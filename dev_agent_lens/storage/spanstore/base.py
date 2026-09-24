"""Span-store backends: where DAL's Parquet trace store physically lives.

ENG2-1591: the open-source product must not force S3, and it has to deploy into highly
regulated, on-prem environments, so the object-storage backend has to be pluggable.

The point of this module is that the answer is a connection string rather than an
architecture. Every backend below hands the query engine the same thing: a glob of
hive-partitioned Parquet. DuckDB and DataFusion do not know or care which one they got,
which is what makes the claim testable instead of aspirational -- see
`scripts/verify_spanstore.py`, which runs the identical recipes against every configured
store and fails if any two disagree by a single row.

Backends are addressed by URI:

    file:///var/lib/dal/spans                       local directory (tier 0)
    s3://bucket/prefix                              AWS S3
    s3://bucket/prefix?endpoint=minio:9000&tls=0    any S3-compatible endpoint
                                                    (MinIO, Garage, Ceph RGW, SeaweedFS,
                                                     an on-prem gateway)
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

log = logging.getLogger(__name__)


def quote_literal(value: str) -> str:
    """Render a string for interpolation into DuckDB SQL, or refuse.

    Refuses rather than escapes: a store URI, glob or path that contains a quote is not
    data we want anywhere near an engine that can read and write files. A review injected
    through a `'` in a file:// root and read the S3 secret back via current_setting().
    """
    if "'" in value or "\0" in value:
        raise ValueError("value may not contain a quote character")
    return "'" + value + "'"


# Pinned because the write settings dominate the format choice, by a lot.
#
# Measured 2026-09-04 on one day of the ENG2-1589 fixture (5,116 spans, identical rows,
# identical ZSTD codec, only the write settings varying):
#
#     duckdb   level 1                        110.17 MB
#     pyarrow  level 1, one row group         102.85 MB
#     pyarrow  level 1, 2000-row groups        55.47 MB   <- how our fixture was written
#     duckdb   level 3                         15.82 MB
#     pyarrow  level 3, one row group          14.93 MB
#     pyarrow  level 3, 2000-row groups        10.76 MB
#     pyarrow  level 9                          6.34 MB
#
# A 17x spread with nothing changed but writer settings. pyarrow's default zstd level is
# 1, which is why the fixture looked 3x larger than the DuckLake (429 MB) and Iceberg
# (574 MB) candidates it was compared against: those numbers were never measuring the
# format. Both of those land in the same family as duckdb level 3 (443.7 MB), so the whole
# apparent gap was our own export's compression level. Pin the level, or a size comparison
# measures the writer's defaults instead of the thing under test.
#
# Level choice, whole fixture (101,655 spans, threads=1), with a 4-recipe suite where one
# recipe scans the large payload column:
#
#     level   size        write     suite
#     1       2112.6 MB   15.6 s    1877 ms
#     3        443.7 MB   11.1 s    1247 ms
#     9        204.8 MB   11.5 s    1072 ms      <- default
#     15       190.7 MB  352.4 s    1060 ms
#
# Level 9 is smaller AND faster to query than level 3, because reading 2.2x fewer bytes
# more than pays for the extra decompression, and it costs 4% more write time. Level 15
# buys 7% more size for 32x the write time and no read gain.
#
# An earlier version of this pinned level 3 on the strength of the single-day numbers
# above, where level 9 looked 33% slower to write. The whole-fixture measurement says that
# was noise on a small input.
DEFAULT_ZSTD_LEVEL = 9


def dataset_of(parts: Sequence[str]) -> str:
    """The dataset a Parquet object belongs to, from its path below the store root.

    A dataset is every path segment before the first hive `key=value` segment, or
    before the file name when there is none:

        spans_raw/day=2026-09-03/sync_ab12.parquet   -> spans_raw
        export/sf/spans/week=2026-W36/data_0.parquet -> export/sf/spans
        blobs_typed/data_0.parquet                   -> blobs_typed

    One rule for every backend, so `dal store status` lists the same datasets whether
    the objects sit in a directory or a bucket.
    """
    dirs = list(parts[:-1])
    for i, seg in enumerate(dirs):
        if "=" in seg:
            dirs = dirs[:i]
            break
    return "/".join(dirs)


# The raw dataset's column types, as every producer path lands them (Phoenix Postgres
# and SQLite direct clients, the REST client, `dal ingest-sessions --to-store`). Only
# consulted when a batch carries a column with no non-null value, so DuckDB cannot infer
# the type from the data; anything not listed is text. Token counts are DOUBLE because
# Phoenix stores them as float and the existing store was written that way; changing
# that is a rebuild, not an edit here.
RAW_COLUMN_TYPES: dict[str, str] = {
    "span_id": "VARCHAR",
    "trace_id": "VARCHAR",
    "parent_id": "VARCHAR",
    "name": "VARCHAR",
    "span_kind": "VARCHAR",
    "start_time": "TIMESTAMP WITH TIME ZONE",
    "end_time": "TIMESTAMP WITH TIME ZONE",
    "status_code": "VARCHAR",
    "status_message": "VARCHAR",
    "attributes": "VARCHAR",
    "events": "VARCHAR",
    "cumulative_error_count": "BIGINT",
    "cumulative_llm_token_count_prompt": "BIGINT",
    "cumulative_llm_token_count_completion": "BIGINT",
    "llm_token_count_prompt": "DOUBLE",
    "llm_token_count_completion": "DOUBLE",
    "source": "VARCHAR",
    "day": "DATE",
}


@dataclass
class StoreHealth:
    """Result of probing a store. `ok` is the only field callers must branch on."""

    ok: bool
    uri: str
    detail: str = ""
    readable: bool = False
    writable: bool = False
    elapsed_ms: float = 0.0


@dataclass
class WriteResult:
    rows: int  # rows read back from the store after the write
    partitions: int
    bytes_written: int
    elapsed_ms: float
    uri: str
    source_rows: int = 0  # rows in the source; a write is only good if rows == source_rows


@dataclass
class StoreCapabilities:
    """What a deployment gets by choosing this backend.

    These are deployment facts, not performance numbers: they are the things that decide
    whether a backend is usable at a regulated customer, which is the question the review
    actually asked.
    """

    name: str
    needs_network: bool
    needs_duckdb_extension: str | None  # None = core DuckDB, nothing to vendor
    airgap_ready: bool  # works with no outbound network at all
    notes: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


class SpanStore(abc.ABC):
    """A physical location holding hive-partitioned Parquet span data.

    Implementations must be interchangeable: given the same input, `read_glob()` must
    yield byte-identical query results across every backend. The verification harness
    enforces this rather than trusting it.
    """

    scheme: str = ""

    def __init__(self, uri: str) -> None:
        self.uri = uri

    # -- what the engine needs ------------------------------------------------
    @abc.abstractmethod
    def read_glob(self, dataset: str = "spans") -> str:
        """The glob an engine reads. Must expand to hive-partitioned Parquet."""

    @abc.abstractmethod
    def write_target(self, dataset: str = "spans") -> str:
        """The destination a `COPY ... TO` writes to."""

    def attach_duckdb(self, con: Any) -> None:
        """Configure a DuckDB connection so `read_glob()` resolves. Default: nothing."""
        log.debug("[spanstore:%s] attach_duckdb no-op", self.scheme)

    # -- lifecycle ------------------------------------------------------------
    @abc.abstractmethod
    def ensure(self) -> None:
        """Create the container (directory / bucket) if it does not exist."""

    @abc.abstractmethod
    def clear(self, dataset: str = "spans") -> int:
        """Remove everything under `dataset`. Returns the count removed.

        Called before every write. DuckDB's OVERWRITE_OR_IGNORE leaves partitions from a
        previous write in place, so re-pointing at a used directory inflated every
        row/partition/byte figure that was read back from it.
        """

    def prepare_write(self, dataset: str) -> None:
        """Make `write_target(dataset)` writable. Default: nothing.

        DuckDB's partitioned COPY creates the partition directories but not the dataset's
        parents, so a nested dataset name (`export/<source>/spans`) fails on a directory
        store with "Failed to create directory". Object stores have no directories and
        need nothing.
        """
        log.debug("[spanstore:%s] prepare_write no-op dataset=%s", self.scheme, dataset)

    @abc.abstractmethod
    def probe(self) -> StoreHealth:
        """Cheap reachability + permission check. Never raises; returns ok=False."""

    @abc.abstractmethod
    def capabilities(self) -> StoreCapabilities:
        """Deployment facts, for the on-prem / air-gap decision."""

    # -- shared helpers -------------------------------------------------------
    def copy_from_glob(
        self,
        con: Any,
        source_glob: str,
        dataset: str = "spans",
        partition_by: str = "day",
        compression: str = "ZSTD",
        compression_level: int = DEFAULT_ZSTD_LEVEL,
        deterministic: bool = True,
    ) -> WriteResult:
        """Load Parquet from `source_glob` into this store, partitioned and compressed.

        Shared by every backend because the SQL is identical; only the destination URI
        changes. That is the whole thesis of this module, so it is written once.

        `compression_level` is pinned rather than left to the writer. See
        DEFAULT_ZSTD_LEVEL for why that matters more than any format choice.

        `deterministic` defaults to True, and pins the writer to one thread so output
        is byte-reproducible. It defaults on for the same reason `compression_level` is
        pinned: a silent default is how a published size figure turned out to be measuring
        the writer. Section 10 of ADR 0003 makes the manifest seam a file naming the exact
        object digests a revision spans, and digests are only checkable if a rewrite of the
        same rows reproduces the same bytes. Pass False deliberately when bulk-loading and
        no size or digest will be quoted.

        Measured 2026-09-04: three multi-threaded writes of identical
        data to the same directory produced 450.800 / 451.999 / 450.973 MB with different
        per-file checksums, while single-threaded writes were byte-identical across runs
        and 1.6% smaller. Use it whenever a size figure will be compared or quoted.
        """
        t0 = time.perf_counter()
        src = quote_literal(source_glob)
        target = quote_literal(self.write_target(dataset))
        if not partition_by.isidentifier():
            raise ValueError("partition_by must be a plain column name")
        self.attach_duckdb(con)
        source_sql = f"read_parquet({src}, hive_partitioning=true, union_by_name=true)"
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {source_sql}").fetchall()}
        if partition_by not in cols:
            if partition_by == "day" and "start_time" in cols:
                # the phoenix.spans dump has no day column; derive it rather than die with
                # a raw BinderException about a column the operator never heard of
                select = f"SELECT *, CAST(start_time AS DATE) AS day FROM {source_sql}"
            else:
                raise ValueError(
                    f"source has no {partition_by!r} column to partition by "
                    f"(columns: {', '.join(sorted(cols))})"
                )
        else:
            select = f"SELECT * FROM {source_sql}"
        source_rows = con.execute(f"SELECT count(*) FROM {source_sql}").fetchone()[0]
        removed = self.clear(dataset)
        self.prepare_write(dataset)
        log.info(
            "[spanstore:%s] write start target=%s partition_by=%s codec=%s level=%d "
            "deterministic=%s source_rows=%d cleared=%d",
            self.scheme,
            target,
            partition_by,
            compression,
            compression_level,
            deterministic,
            source_rows,
            removed,
        )
        prior_threads = None
        try:
            if deterministic:
                prior_threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
                con.execute("SET threads=1")
            con.execute(
                f"""COPY ({select}) TO {target}
                    (FORMAT PARQUET, COMPRESSION {compression},
                     COMPRESSION_LEVEL {int(compression_level)},
                     PARTITION_BY ({partition_by}), OVERWRITE_OR_IGNORE)"""
            )
        finally:
            if prior_threads is not None:  # never leave the caller's connection at 1 thread
                con.execute(f"SET threads={int(prior_threads)}")
        elapsed = (time.perf_counter() - t0) * 1000
        glob = quote_literal(self.read_glob(dataset))
        rows = con.execute(
            f"SELECT count(*) FROM read_parquet({glob}, hive_partitioning=true, union_by_name=true)"
        ).fetchone()[0]
        parts = con.execute(
            f"SELECT count(DISTINCT {partition_by}) FROM read_parquet({glob}, "
            f"hive_partitioning=true, union_by_name=true)"
        ).fetchone()[0]
        size = self.size_bytes(dataset)
        if rows != source_rows:
            raise RuntimeError(
                f"[spanstore:{self.scheme}] wrote {source_rows} rows but read "
                f"back {rows} from {target}"
            )
        log.info(
            "[spanstore:%s] write done rows=%d partitions=%d bytes=%d in %.1fms",
            self.scheme,
            rows,
            parts,
            size,
            elapsed,
        )
        return WriteResult(
            rows=rows,
            partitions=parts,
            bytes_written=size,
            elapsed_ms=elapsed,
            uri=self.write_target(dataset),
            source_rows=source_rows,
        )

    def append_frame(
        self,
        con: Any,
        df: Any,
        dataset: str = "spans_raw",
        *,
        partition_by: str = "day",
        compression: str = "ZSTD",
        compression_level: int = DEFAULT_ZSTD_LEVEL,
        source: str | None = None,
        dedupe: bool = True,
    ) -> int:
        """Append one fetched batch as new partition files. Never clears.

        `source` is the configured trace source the batch came from. It is stamped into a
        `source` column (when the frame has none) so a store holding several sources can
        be exported or queried per source; nothing in the native span carries it.

        Idempotent per (span_id, source) when `dedupe` is on: rows already present in the
        batch's day partitions are dropped before the write, so a sync loop that re-pulls
        an overlapping window (deploy/compose.yml does, every SYNC_INTERVAL) lands each
        span once. Before this, a second loop iteration doubled the store: 154 spans in,
        308 rows out, measured 2026-09-11. The check reads only `span_id` and `source`
        from the days the batch touches, so it costs a column scan of those partitions.

        This is what `dal sync` calls per batch. The frame is brought to the store's raw
        shape first: the direct Phoenix clients name the ids `context.span_id` /
        `context.trace_id`, the REST client flattens `attributes` into `attributes.*`
        columns, and neither carries a `day`. Every file gets a unique name so two batches
        landing in the same day partition never overwrite each other.
        """
        import json
        import math
        import uuid

        if df is None or len(df) == 0:
            return 0
        df = df.rename(columns={"context.span_id": "span_id", "context.trace_id": "trace_id"})
        if source is not None and "source" not in df.columns:
            df = df.assign(source=source)
        if "attributes" not in df.columns:
            flat = [c for c in df.columns if c.startswith("attributes.")]
            if flat:
                # REST sources: re-nest the flattened columns into one JSON object per row.
                # Best effort: it is only as faithful as the client's flattening was.
                def nest(row):
                    out: dict = {}
                    for c in flat:
                        v = row[c]
                        if v is None or (isinstance(v, float) and math.isnan(v)):
                            continue
                        node = out
                        *parents, leaf = c[len("attributes.") :].split(".")
                        for k in parents:
                            node = node.setdefault(k, {})
                        node[leaf] = v
                    return json.dumps(out, default=str)

                df = df.assign(attributes=df.apply(nest, axis=1)).drop(columns=flat)
                log.info(
                    "[spanstore:%s] re-nested %d flattened attribute columns",
                    self.scheme,
                    len(flat),
                )
        for col in ("attributes", "events"):
            if col not in df.columns:
                continue  # a typed or fixture frame may carry neither; that is fine
            is_container = df[col].map(lambda v: isinstance(v, (dict, list)))
            if is_container.any():
                df = df.assign(
                    **{
                        col: df[col].map(
                            lambda v: json.dumps(v, default=str)
                            if isinstance(v, (dict, list))
                            else v
                        )
                    }
                )
        missing = {"span_id", "start_time"} - set(df.columns)
        if missing:
            raise ValueError(
                f"batch lacks required column(s) {sorted(missing)}; "
                f"columns: {', '.join(df.columns)}"
            )

        self.attach_duckdb(con)
        self.prepare_write(dataset)
        if dedupe:
            df, skipped = self._drop_present(con, df, dataset, partition_by, source)
            if skipped:
                log.info(
                    "[spanstore:%s] skipped %d span(s) already in %s (idempotent re-sync)",
                    self.scheme,
                    skipped,
                    dataset,
                )
            if len(df) == 0:
                log.info("[spanstore:%s] nothing new in this batch for %s", self.scheme, dataset)
                return 0
        con.register("_dal_batch", df)
        try:
            # A column that is null in every row of this batch has no type of its own:
            # pandas hands DuckDB an object column, DuckDB registers it as INTEGER, and the
            # Parquet file lands with parent_id (or status_message, or a token count) as
            # INT32. The next batch writes the same column as VARCHAR, and every reader
            # of the union fails with "Could not convert string '' to INT32" the first
            # time it touches the column. Pin all-null columns to the raw schema's type.
            # Seen live on a two-day container sync of a project whose small batches
            # held only root spans (ENG2-1612 smoke test, 2026-09-11).
            projection = []
            pinned = []
            for col in df.columns:
                q = '"' + str(col).replace('"', '""') + '"'
                if df[col].isna().all():
                    typ = RAW_COLUMN_TYPES.get(str(col), "VARCHAR")
                    projection.append(f"CAST(NULL AS {typ}) AS {q}")
                    pinned.append(f"{col}:{typ}")
                else:
                    projection.append(q)
            if pinned:
                log.debug(
                    "[spanstore:%s] pinned %d all-null column(s) to their raw types: %s",
                    self.scheme,
                    len(pinned),
                    ", ".join(pinned),
                )
            day_expr = (
                ""
                if partition_by in df.columns
                else f", CAST(start_time AS DATE) AS {partition_by}"
            )
            target = quote_literal(self.write_target(dataset))
            pattern = quote_literal(f"sync_{uuid.uuid4().hex[:12]}")
            con.execute(
                f"""COPY (SELECT {", ".join(projection)}{day_expr} FROM _dal_batch) TO {target}
                    (FORMAT PARQUET, COMPRESSION {compression},
                     COMPRESSION_LEVEL {int(compression_level)},
                     PARTITION_BY ({partition_by}), OVERWRITE_OR_IGNORE,
                     FILENAME_PATTERN {pattern})"""
            )
        finally:
            con.unregister("_dal_batch")
        log.info("[spanstore:%s] appended %d rows to %s", self.scheme, len(df), dataset)
        return len(df)

    def _drop_present(
        self, con: Any, df: Any, dataset: str, partition_by: str, source: str | None
    ) -> tuple[Any, int]:
        """Return (frame without rows the store already holds, how many were dropped).

        Keyed on (span_id, source) with NULL matching NULL, within the day partitions the
        batch touches. Files written before the `source` stamp existed read as NULL. An
        empty store (no Parquet under the dataset yet) drops nothing.
        """
        import time

        t0 = time.perf_counter()
        glob = quote_literal(self.read_glob(dataset))
        scan = f"read_parquet({glob}, hive_partitioning=true, union_by_name=true)"
        try:
            cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {scan}").fetchall()}
        except Exception as e:  # noqa: BLE001 - only the empty-store case is expected
            if "no files found" in str(e).lower():
                return df, 0
            raise
        if "span_id" not in cols:
            return df, 0
        src_col = "p.source" if "source" in cols else "NULL::VARCHAR"
        con.register("_dal_probe", df)
        try:
            day_expr = partition_by if partition_by in df.columns else "CAST(start_time AS DATE)"
            days = [
                r[0]
                for r in con.execute(f"SELECT DISTINCT {day_expr} FROM _dal_probe").fetchall()
                if r[0] is not None
            ]
            if not days:
                return df, 0
            day_list = ", ".join(f"DATE '{d}'" for d in days)
            present = con.execute(
                f"""SELECT DISTINCT p.span_id, {src_col} FROM {scan} p
                    WHERE p.{partition_by} IN ({day_list})
                      AND p.span_id IN (SELECT span_id FROM _dal_probe)"""
            ).fetchall()
        finally:
            con.unregister("_dal_probe")
        if not present:
            return df, 0
        pairs = {(sid, src) for sid, src in present}
        batch_src = (
            df["source"].map(lambda v: None if v is None or v != v else v)
            if "source" in df.columns
            else [None] * len(df)
        )
        mask = [(sid, src) in pairs for sid, src in zip(df["span_id"].tolist(), list(batch_src))]
        keep = df[[not m for m in mask]]
        log.debug(
            "[spanstore:%s] dedupe probe days=%d present=%d in %.1fms",
            self.scheme,
            len(days),
            len(pairs),
            (time.perf_counter() - t0) * 1000,
        )
        return keep, len(df) - len(keep)

    @abc.abstractmethod
    def size_bytes(self, dataset: str = "spans") -> int:
        """Total bytes stored for `dataset`. The number a size claim must come from."""

    @abc.abstractmethod
    def list_datasets(self) -> list[str]:
        """Every dataset holding at least one Parquet object, sorted. See `dataset_of`."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.uri}>"
