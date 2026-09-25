"""Partition updates published as one immutable typed snapshot."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import time
import uuid
from pathlib import Path

from dev_agent_lens.storage.layouts.base import BuildResult
from dev_agent_lens.storage.snapshots import SnapshotIO
from dev_agent_lens.storage.spanstore.base import quote_literal

log = logging.getLogger(__name__)


def parquet_sql(files):
    paths = "[" + ",".join(quote_literal(p) for p in files) + "]"
    return f"read_parquet({paths}, hive_partitioning=true, union_by_name=true)"


def day_of(path):
    days = [part[4:] for part in path.split("/") if part.startswith("day=")]
    if len(days) != 1:
        raise ValueError(f"incremental raw files must have exactly one day partition: {path}")
    return dt.date.fromisoformat(days[0]).isoformat()


def fingerprint(level):
    from . import typed

    identity = Path(typed.identity_path())
    return hashlib.sha256(
        Path(typed.__file__).read_bytes()
        + Path(__file__).read_bytes()
        + (identity.read_bytes() if identity.exists() else b"")
        + str(level).encode()
    ).hexdigest()


def update(layout, con, store, *, zstd_level, full=False):
    from .typed import _bound_memory

    started = time.perf_counter()
    con.execute("SET TimeZone='UTC'")
    _bound_memory(con)
    # The dependency scan parses every changed row's attributes before the build sets
    # its own thread count. DuckDB's default is one thread per core, and each thread
    # holds a 2,048-row vector of parsed JSON, so large attribute strings exhaust a
    # small updater's memory cap. Use the configured build thread count here too.
    threads = os.environ.get("DAL_BUILD_THREADS", "").strip()
    if threads:
        con.execute(f"SET threads={int(threads)}")
        log.info("[layout:incremental] duckdb threads=%s for the dependency scan", threads)
    io = SnapshotIO(store)
    previous, token = io.read_current()
    raw = io.files("spans_raw")
    for path, item in raw.items():
        item["day"] = day_of(path)
    old = previous["raw"] if previous else {}
    version = fingerprint(zstd_level)
    force = full or not previous or previous["build_version"] != version
    changed = {
        item["day"]
        for path, item in {**old, **raw}.items()
        if force or old.get(path) != raw.get(path)
    }
    if previous and not changed and not force:
        return result(previous, [], started, zstd_level)
    if not raw and not previous:
        raise ValueError("no raw day partitions to build")
    generation_id = uuid.uuid4().hex
    generation = io.generation(generation_id)
    store.attach_duckdb(con)
    dependencies, affected = dependency_days(con, raw, previous, changed, generation, force)
    changed = affected
    selected = [p for p, item in raw.items() if item["day"] in changed]
    partitions = {} if force else dict(previous["partitions"])
    for day in changed:
        partitions.pop(day, None)
    if selected:
        layout.build(
            con,
            selected,
            generation,
            zstd_level=zstd_level,
            identity_files=[p for files in dependencies.values() for p in files],
        )
        generated = SnapshotIO(generation)
        hot = generated.files("spans_typed")
        cold = generated.files("blobs_typed")
        for day in sorted(changed):
            files = [p for p in hot if day_of(p) == day]
            if files:
                partitions[day] = {
                    "hot": files,
                    "cold": list(cold),
                    "hot_bytes": sum(hot[p]["size"] for p in files),
                    "cold_sizes": {p: item["size"] for p, item in cold.items()},
                }
    hot_paths = [p for part in partitions.values() for p in part["hot"]]
    store.attach_duckdb(con)
    count = (
        con.execute(f"SELECT count(*) FROM {parquet_sql(hot_paths)}").fetchone()[0]
        if hot_paths
        else 0
    )
    expected = (
        con.execute(f"SELECT count(*) FROM {parquet_sql(list(raw))}").fetchone()[0] if raw else 0
    )
    if count != expected:
        raise RuntimeError(f"typed snapshot count mismatch: {count} != {expected}")
    current = io.files("spans_raw")
    if any(
        current.get(p) != {k: v for k, v in item.items() if k != "day"} for p, item in raw.items()
    ):
        raise RuntimeError("raw input changed during build; previous snapshot retained")
    manifest = dict(
        format=1,
        snapshot=generation_id,
        build_version=version,
        raw=raw,
        partitions=partitions,
        dependencies=dependencies,
        rows=count,
        schema_file=hot_paths[0] if hot_paths else previous["schema_file"],
    )
    io.publish(manifest, token)
    return result(manifest, sorted(changed), started, zstd_level)


def dependency_days(con, raw, previous, changed, generation, force):
    """Take the closure of days sharing any candidate trace/session identity.

    Old links matter for deletions; new links matter for late parents. Unchanged raw
    payloads are never parsed here: their small dependency Parquet files are reused.
    """
    from .typed import _prepare_raw

    old = previous.get("dependencies", {}) if previous else {}
    dependencies = {} if force else dict(old)
    for day in changed:
        dependencies.pop(day, None)
    selected = [path for path, item in raw.items() if item["day"] in changed]
    if selected:
        _prepare_raw(con, selected)
        try:
            sql = """SELECT day,trace_key,euid,session_alt,account_alt,account_rx,device_rx,
                session_rx,ticket_rx,agent_rx,end_user_email FROM _raw"""
            con.execute(
                f"COPY ({sql}) TO {quote_literal(generation.write_target('dependencies'))} "
                "(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY(day))"
            )
            for path in SnapshotIO(generation).files("dependencies"):
                dependencies.setdefault(day_of(path), []).append(path)
        finally:
            con.execute("DROP VIEW IF EXISTS _raw")
    links = sorted(
        {path for parts in (old, dependencies) for paths in parts.values() for path in paths}
    )
    if not links or force:
        return dependencies, changed
    try:
        # Follow trace/session relationships, not day adjacency. A rebuilt day can
        # contain unrelated traces; global cached identity facts give those traces
        # their context without recursively rebuilding their other days.
        con.execute(f"CREATE OR REPLACE TEMP VIEW _facts AS SELECT * FROM {parquet_sql(links)}")
        con.execute("""CREATE OR REPLACE TEMP TABLE _trace_sessions AS
            SELECT DISTINCT trace_key, session FROM (
                SELECT trace_key, unnest([session_alt,session_rx,
                    json_extract_string(TRY_CAST(euid AS JSON),'$.session_id')]) AS session
                FROM _facts) WHERE session IS NOT NULL""")
        con.execute("CREATE OR REPLACE TEMP TABLE _changed_days(day VARCHAR PRIMARY KEY)")
        con.executemany("INSERT INTO _changed_days VALUES (?)", [(day,) for day in sorted(changed)])
        con.execute("""CREATE OR REPLACE TEMP TABLE _affected_traces AS
            SELECT DISTINCT trace_key FROM _facts
            WHERE CAST(day AS VARCHAR) IN (SELECT day FROM _changed_days)
            AND trace_key IS NOT NULL""")
        while True:
            new = con.execute("""INSERT INTO _affected_traces
                SELECT DISTINCT b.trace_key FROM _trace_sessions a
                JOIN _trace_sessions b USING(session)
                WHERE a.trace_key IN (SELECT trace_key FROM _affected_traces)
                  AND b.trace_key NOT IN (SELECT trace_key FROM _affected_traces)
                RETURNING trace_key""").fetchall()
            if not new:
                break
        affected = {
            str(row[0])
            for row in con.execute("""SELECT DISTINCT day FROM _facts
            WHERE trace_key IN (SELECT trace_key FROM _affected_traces)""").fetchall()
        }
        return dependencies, changed | affected
    finally:
        for table in ("_changed_days", "_affected_traces", "_trace_sessions"):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DROP VIEW IF EXISTS _facts")


def result(manifest, days, started, level):
    cold = {
        p: size
        for part in manifest["partitions"].values()
        for p, size in part.get("cold_sizes", {}).items()
    }
    return BuildResult(
        layout="typed",
        rows=manifest["rows"],
        bytes_hot=sum(part.get("hot_bytes", 0) for part in manifest["partitions"].values()),
        bytes_cold=sum(cold.values()),
        elapsed_ms=(time.perf_counter() - started) * 1000,
        zstd_level=level,
        detail={"snapshot": manifest["snapshot"], "rebuilt_days": days},
    )


def attach(con, manifest):
    hot = [p for part in manifest["partitions"].values() for p in part["hot"]]
    cold = sorted({p for part in manifest["partitions"].values() for p in part["cold"]})
    sql = parquet_sql(hot or [manifest["schema_file"]])
    con.execute(f"CREATE VIEW spans AS SELECT * FROM {sql}" + ("" if hot else " WHERE false"))
    if cold:
        con.execute(f"""CREATE VIEW blobs AS
            SELECT ref, any_value(kind) AS kind, any_value(body) AS body
            FROM {parquet_sql(cold)} WHERE ref IN (
                SELECT unnest([payload_messages_ref,payload_tools_ref,payload_system_ref])
                FROM spans)
            GROUP BY ref""")
    else:
        con.execute(
            "CREATE VIEW blobs AS SELECT NULL::VARCHAR AS ref, NULL::VARCHAR AS kind, "
            "NULL::VARCHAR AS body WHERE false"
        )
