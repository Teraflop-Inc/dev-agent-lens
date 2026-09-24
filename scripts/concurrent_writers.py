#!/usr/bin/env python3
# ruff: noqa: E501 -- SQL fixtures retain one complete statement per line.
"""Two (or N) concurrent writers against one span store: works, corrupts, or refuses?

ENG2-1598's last open acceptance criterion. The read concurrency curve is measured;
the write side was recorded as untested. `append_frame` is read-then-write
(`_drop_present` reads the day partitions, then `COPY` writes a uniquely-named file)
with no lock between the two, so the question is what a second writer does in that gap.

Two scenarios, because they fail differently:

  overlap   every writer appends the SAME span_ids. Tests the dedupe read-write race.
            Correct = `rows == batch`. More than that means both writers passed the
            "not present" check before either wrote.

  disjoint  every writer appends its own span_ids. Tests file-level safety. Correct =
            `rows == batch * writers`. Fewer means a lost write; a read error means
            a torn or half-written Parquet file.

Usage:
    uv run python scripts/concurrent_writers.py --writers 2 --rows 500
    uv run python scripts/concurrent_writers.py --writers 8 --rows 500 --store s3://bucket/prefix
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DAY = "2026-09-21"


def _frame(rows: int, *, writer: int | None):
    """A minimal raw-shaped batch. `writer=None` gives every writer identical ids."""
    import pandas as pd

    tag = "shared" if writer is None else f"w{writer}"
    return pd.DataFrame(
        {
            "span_id": [f"{tag}-span-{i:06d}" for i in range(rows)],
            "trace_id": [f"{tag}-trace-{i:06d}" for i in range(rows)],
            "start_time": pd.to_datetime([f"{DAY}T12:00:00Z"] * rows, utc=True),
            "name": [f"op-{i}" for i in range(rows)],
        }
    )


def _write(args) -> dict:
    """One writer process. Its own DuckDB connection, as a separate process would have."""
    writer, uri, rows, overlap, dedupe = args
    try:
        import duckdb

        from dev_agent_lens.storage.spanstore import open_store

        store = open_store(uri)
        con = duckdb.connect()
        df = _frame(rows, writer=None if overlap else writer)
        written = store.append_frame(con, df, "spans_raw", source="concurrency-test", dedupe=dedupe)
        con.close()
        return {"writer": writer, "ok": True, "written": int(written)}
    except Exception as exc:  # noqa: BLE001 - the failure mode IS the result
        return {
            "writer": writer,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-800:],
        }


def _count(uri: str) -> tuple[int, int, str | None]:
    """(total rows, distinct span_ids, read error). A read error is the corruption signal."""
    import duckdb

    from dev_agent_lens.storage.spanstore import open_store

    store = open_store(uri)
    con = duckdb.connect()
    try:
        store.attach_duckdb(con)
        glob = store.read_glob("spans_raw")
        total, distinct = con.execute(
            f"SELECT count(*), count(DISTINCT span_id) FROM read_parquet('{glob}')"
        ).fetchone()
        return int(total), int(distinct), None
    except Exception as exc:  # noqa: BLE001
        return -1, -1, f"{type(exc).__name__}: {exc}"
    finally:
        con.close()


def run(scenario: str, writers: int, rows: int, base_uri: str | None, dedupe: bool = True) -> dict:
    overlap = scenario == "overlap"
    tmp = None
    if base_uri:
        uri = f"{base_uri.rstrip('/')}/{scenario}"
    else:
        tmp = tempfile.mkdtemp(prefix=f"dal-concurrent-{scenario}-")
        uri = tmp

    try:
        with mp.get_context("spawn").Pool(writers) as pool:
            results = pool.map(_write, [(i, uri, rows, overlap, dedupe) for i in range(writers)])

        total, distinct, read_error = _count(uri)
        failed = [r for r in results if not r["ok"]]
        expected = (rows if overlap else rows * writers) if dedupe else rows * writers

        if read_error:
            verdict = "CORRUPTS"
            detail = f"store unreadable after concurrent writes: {read_error}"
        elif failed and len(failed) == writers:
            verdict = "REFUSES"
            detail = "every writer raised; nothing landed"
        elif failed:
            verdict = "REFUSES (partial)"
            detail = f"{len(failed)} of {writers} writers raised"
        elif total == expected:
            verdict = "WORKS"
            detail = f"rows == expected ({expected})"
        elif total > expected:
            verdict = "DUPLICATES"
            detail = f"{total} rows, expected {expected}; +{total - expected} extra"
        else:
            verdict = "LOSES WRITES"
            detail = f"{total} rows, expected {expected}; {expected - total} missing"

        return {
            "scenario": scenario,
            "dedupe": dedupe,
            "writers": writers,
            "rows_per_writer": rows,
            "expected_rows": expected,
            "actual_rows": total,
            "distinct_span_ids": distinct,
            "writers_failed": len(failed),
            "errors": [f["error"] for f in failed][:3],
            "read_error": read_error,
            "verdict": verdict,
            "detail": detail,
        }
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--writers", type=int, default=2)
    ap.add_argument("--rows", type=int, default=500)
    ap.add_argument("--store", default=None, help="base URI; default is a temp local dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-dedupe", action="store_true", help="append_frame(dedupe=False)")
    ap.add_argument("--only", choices=["overlap", "disjoint"], default=None)
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    os.environ.setdefault("DAL_SPAN_STORE", args.store or "")

    scenarios = [args.only] if args.only else ["overlap", "disjoint"]
    out = [
        run(s, args.writers, args.rows, args.store, not args.no_dedupe)
        for _ in range(args.repeat)
        for s in scenarios
    ]

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for r in out:
            print(
                f"\n=== {r['scenario']}: {r['writers']} writers x {r['rows_per_writer']} rows ==="
            )
            print(f"  expected rows : {r['expected_rows']}")
            print(
                f"  actual rows   : {r['actual_rows']}  (distinct span_ids {r['distinct_span_ids']})"
            )
            print(f"  writers failed: {r['writers_failed']}")
            for e in r["errors"]:
                print(f"    - {e}")
            print(f"  VERDICT       : {r['verdict']} - {r['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
