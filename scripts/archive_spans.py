"""ENG2-1461: archive phoenix.* to parquet + verify content equivalence.

Step 1 of the de-bloat plan: archive EVERYTHING, prove the archive is
faithful, and only then (a separate, explicit step — not this script) delete
from the hot table. No DELETE lives in this file by design.

Phases:
  export  — dump phoenix.spans (keyset-paginated, timeout-resilient) plus the
            small sibling tables (traces, projects, span_costs,
            span_cost_details) to parquet under --archive-dir. Resumable: a
            checkpoint file records the last exported span id.
  verify  — prove equivalence: total/row-range counts, per-week × span_kind
            counts DB vs archive, and exact row-content comparison on a random
            sample re-fetched from the DB.

Usage:
  uv run --with 'psycopg[binary],pyarrow,duckdb' python scripts/archive_spans.py \
      --archive-dir ~/dal-archive/phoenix-2026-08-03 --phase all

DSN comes from $PHOENIX_SQL_DATABASE_URL (fallback: ../.env). Read-only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger("archive_spans")

SPAN_COLS = [
    "id", "trace_rowid", "span_id", "parent_id", "name", "span_kind",
    "start_time", "end_time", "attributes", "events", "status_code",
    "status_message", "cumulative_error_count",
    "cumulative_llm_token_count_prompt", "cumulative_llm_token_count_completion",
    "llm_token_count_prompt", "llm_token_count_completion",
]
JSONB_COLS = {"attributes", "events"}
TS_COLS = {"start_time", "end_time"}
SMALL_TABLES = ["traces", "projects", "span_costs", "span_cost_details"]

SPANS_SCHEMA = pa.schema(
    [
        (c, pa.timestamp("us", tz="UTC")) if c in TS_COLS
        else (c, pa.int64()) if c == "id" or c.endswith("_rowid") or "count" in c
        else (c, pa.string())
        for c in SPAN_COLS
    ]
)


def dsn_from_env() -> str:
    dsn = os.environ.get("PHOENIX_SQL_DATABASE_URL")
    if dsn:
        return dsn
    env_path = Path(__file__).resolve().parent.parent / ".env"
    m = re.search(
        r'^\s*PHOENIX_SQL_DATABASE_URL=("?)(.+?)\1\s*$', env_path.read_text(), re.M
    )
    if not m:
        sys.exit("[archive] PHOENIX_SQL_DATABASE_URL not in env or ../.env")
    return m.group(2)


def connect(dsn: str, timeout_s: int) -> psycopg.Connection:
    conn = psycopg.connect(dsn, connect_timeout=15)
    conn.execute(f"SET statement_timeout = '{timeout_s}s'")
    conn.execute("SET default_transaction_read_only = on")
    # Bucket comparisons (date_trunc) must agree with DuckDB, which truncates
    # parquet timestamps in UTC — a non-UTC session TZ shuffles boundary rows
    # between week buckets and fails verify with zero actual drift.
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def row_to_record(row: tuple) -> dict:
    rec = {}
    for col, val in zip(SPAN_COLS, row):
        if col in JSONB_COLS:
            rec[col] = None if val is None else json.dumps(val, sort_keys=True)
        else:
            rec[col] = val
    return rec


def export_small_tables(conn: psycopg.Connection, out_dir: Path) -> None:
    import pyarrow.lib  # noqa: F401  (ensure pa loaded before inference)

    for table in SMALL_TABLES:
        t0 = time.perf_counter()
        cur = conn.execute(f"SELECT * FROM phoenix.{table} ORDER BY id")
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
        arrays = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
        table_pa = pa.table(arrays)
        path = out_dir / f"{table}.parquet"
        pq.write_table(table_pa, path)
        log.info(
            "[export] %s: %d rows -> %s in %.1fs",
            table, len(rows), path, time.perf_counter() - t0,
        )


def export_spans(
    conn: psycopg.Connection,
    out_dir: Path,
    chunk_rows: int,
    rows_per_file: int,
    checkpoint: Path,
    max_spans: int | None = None,
) -> None:
    last_id = 0
    if checkpoint.exists():
        last_id = json.loads(checkpoint.read_text())["last_id"]
        log.info("[export] resuming after span id %d", last_id)

    max_id = conn.execute("SELECT max(id) FROM phoenix.spans").fetchone()[0]
    total = conn.execute(
        "SELECT count(*) FROM phoenix.spans WHERE id > %s", (last_id,)
    ).fetchone()[0]
    log.info("[export] %d spans to export (id %d..%d)", total, last_id + 1, max_id)

    sql = (
        f"SELECT {', '.join(SPAN_COLS)} FROM phoenix.spans "
        "WHERE id > %s ORDER BY id LIMIT %s"
    )
    done = 0
    t_start = time.perf_counter()
    buf: list[dict] = []
    buf_first_id: int | None = None
    cur_chunk = chunk_rows

    def flush() -> None:
        nonlocal buf, buf_first_id
        if not buf:
            return
        path = out_dir / f"spans_{buf_first_id:08d}_{buf[-1]['id']:08d}.parquet"
        cols = {c: [r[c] for r in buf] for c in SPAN_COLS}
        pq.write_table(pa.table(cols, schema=SPANS_SCHEMA), path)
        log.info("[export] wrote %s (%d rows)", path.name, len(buf))
        buf, buf_first_id = [], None

    while last_id < max_id:
        if max_spans is not None and done >= max_spans:
            log.info("[export] --max-spans %d reached (smoke mode) — stopping", max_spans)
            break
        t0 = time.perf_counter()
        try:
            rows = conn.execute(sql, (last_id, cur_chunk)).fetchall()
        except psycopg.errors.QueryCanceled:
            conn.rollback()
            cur_chunk = max(25, cur_chunk // 2)
            log.warning(
                "[export] chunk timed out after id %d — halving chunk to %d",
                last_id, cur_chunk,
            )
            continue
        if not rows:
            break
        if buf_first_id is None:
            buf_first_id = rows[0][0]
        buf.extend(row_to_record(r) for r in rows)
        last_id = rows[-1][0]
        done += len(rows)
        if len(buf) >= rows_per_file:
            flush()
            checkpoint.write_text(json.dumps({"last_id": last_id}))
        if cur_chunk < chunk_rows:
            cur_chunk = min(chunk_rows, cur_chunk * 2)  # recover after timeouts
        if done % (chunk_rows * 20) < chunk_rows:
            rate = done / max(time.perf_counter() - t_start, 1e-6)
            log.info(
                "[export] %d/%d spans (%.0f rows/s, ~%.0f min left, chunk %.1fs)",
                done, total, rate, (total - done) / max(rate, 1) / 60,
                time.perf_counter() - t0,
            )
    flush()
    checkpoint.write_text(json.dumps({"last_id": last_id, "complete": True}))
    log.info("[export] DONE: %d spans in %.1f min", done, (time.perf_counter() - t_start) / 60)


def verify(conn: psycopg.Connection, out_dir: Path, sample_n: int) -> dict:
    import duckdb

    duck = duckdb.connect()
    # Same UTC pinning as the PG session (see connect()): DuckDB's date_trunc
    # on timestamptz uses its TimeZone setting, which defaults to the SYSTEM
    # timezone — bucket comparisons need both engines truncating in UTC.
    duck.execute("SET TimeZone='UTC'")
    glob = str(out_dir / "spans_*.parquet")
    report: dict = {"ok": True, "checks": []}

    def check(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        report["ok"] = report["ok"] and ok
        log.log(logging.INFO if ok else logging.ERROR,
                "[verify] %s %s — %s", "PASS" if ok else "FAIL", name, detail)

    # 1. totals + id range + duplicate ids
    t0 = time.perf_counter()
    a_cnt, a_min, a_max, a_dist = duck.execute(
        f"SELECT count(*), min(id), max(id), count(DISTINCT id) FROM '{glob}'"
    ).fetchone()
    # Export walks ids ascending, so the archived set == every span with
    # id <= a_max. Bounding the DB side by a_max makes verify correct for both
    # full archives and --max-spans smoke runs.
    d_cnt, d_min, d_max = conn.execute(
        "SELECT count(*), min(id), max(id) FROM phoenix.spans WHERE id <= %s",
        (a_max,),
    ).fetchone()
    check("row_count", a_cnt == d_cnt, f"archive={a_cnt} db={d_cnt}")
    check("id_range", (a_min, a_max) == (d_min, d_max),
          f"archive=({a_min},{a_max}) db=({d_min},{d_max})")
    check("no_dup_ids", a_dist == a_cnt, f"distinct={a_dist} rows={a_cnt}")

    # 2. per-week × span_kind counts
    a_weeks = dict(
        ((str(w), k or ""), c)
        for w, k, c in duck.execute(
            f"SELECT date_trunc('week', start_time)::date, span_kind, count(*) "
            f"FROM '{glob}' GROUP BY 1, 2"
        ).fetchall()
    )
    d_weeks = dict(
        ((str(w), k or ""), c)
        for w, k, c in conn.execute(
            "SELECT date_trunc('week', start_time)::date, span_kind, count(*) "
            "FROM phoenix.spans WHERE id <= %s GROUP BY 1, 2",
            (a_max,),
        ).fetchall()
    )
    diffs = {k: (a_weeks.get(k), d_weeks.get(k))
             for k in set(a_weeks) | set(d_weeks)
             if a_weeks.get(k) != d_weeks.get(k)}
    check("week_kind_counts", not diffs, f"{len(diffs)} mismatched buckets: "
          f"{dict(list(diffs.items())[:3]) if diffs else 'none'}")

    # 3. exact content on a random sample re-fetched from the DB
    all_ids = [r[0] for r in duck.execute(f"SELECT id FROM '{glob}'").fetchall()]
    sample = sorted(random.sample(all_ids, min(sample_n, len(all_ids))))
    mismatches = []
    for i in range(0, len(sample), 50):
        ids = sample[i : i + 50]
        db_rows = {
            r[0]: row_to_record(r)
            for r in conn.execute(
                f"SELECT {', '.join(SPAN_COLS)} FROM phoenix.spans "
                "WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        }
        arch = duck.execute(
            f"SELECT * FROM '{glob}' WHERE id IN ({','.join(map(str, ids))})"
        ).fetch_arrow_table().to_pylist()
        for a in arch:
            d = db_rows.get(a["id"])
            if d is None:
                mismatches.append((a["id"], "missing in db refetch"))
                continue
            for col in SPAN_COLS:
                av, dv = a[col], d[col]
                if col in JSONB_COLS:
                    av = json.loads(av) if av else None
                    dv = json.loads(dv) if dv else None
                if col in TS_COLS and av is not None and dv is not None:
                    # Compare as epoch-µs: parquet returns tz-aware datetimes
                    # whose str() differs from psycopg's, but the instant and
                    # precision (µs) are identical when the data is faithful.
                    av = round(av.timestamp() * 1e6)
                    dv = round(dv.timestamp() * 1e6)
                if av != dv:
                    mismatches.append((a["id"], col))
                    break
    check("sampled_content_equal", not mismatches,
          f"{len(sample)} sampled, {len(mismatches)} mismatches: {mismatches[:5]}")
    log.info("[verify] content sample took %.1fs", time.perf_counter() - t0)

    # 4. small tables
    for table in SMALL_TABLES:
        a = duck.execute(
            f"SELECT count(*) FROM '{out_dir / (table + '.parquet')}'"
        ).fetchone()[0]
        d = conn.execute(f"SELECT count(*) FROM phoenix.{table}").fetchone()[0]
        check(f"{table}_count", a == d, f"archive={a} db={d}")

    (out_dir / "verify_report.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive-dir", required=True)
    ap.add_argument("--phase", choices=["export", "verify", "all"], default="all")
    ap.add_argument("--chunk-rows", type=int, default=400)
    ap.add_argument("--rows-per-file", type=int, default=20000)
    ap.add_argument("--sample-n", type=int, default=300)
    ap.add_argument("--statement-timeout-s", type=int, default=120)
    ap.add_argument(
        "--max-spans", type=int, default=None,
        help="Smoke mode: stop exporting after ~N spans. Verify then only "
        "checks counts/content over what was exported (id-bounded).",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    out_dir = Path(args.archive_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(dsn_from_env(), args.statement_timeout_s)
    log.info("[archive] connected; dir=%s phase=%s", out_dir, args.phase)

    if args.phase in ("export", "all"):
        export_small_tables(conn, out_dir)
        export_spans(
            conn, out_dir, args.chunk_rows, args.rows_per_file,
            out_dir / "checkpoint.json",
            max_spans=args.max_spans,
        )
    if args.phase in ("verify", "all"):
        report = verify(conn, out_dir, args.sample_n)
        sys.exit(0 if report["ok"] else 2)


if __name__ == "__main__":
    main()
