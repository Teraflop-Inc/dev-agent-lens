#!/usr/bin/env python3
"""Capture parity: does the new store reach a person's sessions at least as often as
Phoenix does? (ENG2-1611, strand 3, criterion 12.)

The unit is a local Claude Code session with at least one assistant turn, from
``~/.claude/projects/<project>/<session>.jsonl``, scoped by ``--include`` the way
``dal ingest-sessions`` scopes it. Each session is then looked up in:

- a Phoenix-synced store (what the LiteLLM proxy captured; ENG2-1581's "reach"), and
- a store that ``dal ingest-sessions --to-store`` landed the same folder into.

Both are the new store. The first is the proxy channel, the second the JSONL channel,
and parity holds when the union is at least the Phoenix number. 2026-09-11, Adam's
laptop, ``--include '*teraflop*'``: 114 sessions, proxy 103, JSONL 114, union 114.

    uv run python scripts/capture_parity.py --include '*teraflop*' \
        --phoenix-store 's3://dal-migration/rehearsal?endpoint=127.0.0.1:9100&tls=0' \
        --jsonl-store file:///tmp/parity-store

The JSONL store is whatever ``dal ingest-sessions --include '<same glob>' --to-store``
was pointed at (``DAL_SPAN_STORE``). Omit it to measure the proxy channel alone.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import logging
import os
import sys
import time

log = logging.getLogger("capture_parity")

_RAW_SESSION = (
    "coalesce(json_extract_string(attributes,'$.claude.session_id'), "
    "json_extract_string(attributes,'$.session.id'), "
    "json_extract_string(attributes,'$.metadata.session_id'))"
)


def local_sessions(sessions_dir: str, include: str) -> dict[str, str]:
    """session_id -> path, for top-level sessions with an assistant turn under matching projects."""
    t0 = time.perf_counter()
    out: dict[str, str] = {}
    for f in glob.glob(os.path.join(sessions_dir, "*", "*.jsonl")):
        sid = os.path.basename(f)[:-6]
        if sid.startswith("agent-"):
            continue
        if not fnmatch.fnmatch(os.path.basename(os.path.dirname(f)), include):
            continue
        try:
            with open(f, errors="ignore") as fh:
                has = any('"type":"assistant"' in line for line in fh)
        except OSError:
            has = False
        if has:
            out[sid] = f
    log.info(
        "[parity] %d local sessions match %r in %.0fms",
        len(out),
        include,
        (time.perf_counter() - t0) * 1000,
    )
    return out


def store_sessions(uri: str) -> set[str]:
    """Distinct session ids in a store: typed `session_id` if built, else the raw paths."""
    import duckdb

    from dev_agent_lens.storage.spanstore import open_store

    t0 = time.perf_counter()
    s = open_store(uri)
    con = duckdb.connect()
    s.attach_duckdb(con)
    datasets = s.list_datasets()
    if "spans_typed" in datasets:
        col, dataset = "session_id", "spans_typed"
    else:
        col, dataset = _RAW_SESSION, "spans_raw"
    rows = con.execute(
        f"SELECT DISTINCT {col} FROM read_parquet(?, hive_partitioning=true, union_by_name=true) "
        f"WHERE {col} IS NOT NULL",
        [s.read_glob(dataset)],
    ).fetchall()
    ids = {r[0] for r in rows}
    log.info(
        "[parity] %s: %d sessions from %s in %.0fms",
        s.uri,
        len(ids),
        dataset,
        (time.perf_counter() - t0) * 1000,
    )
    return ids


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--include", required=True, help="glob on the project folder name, as dal ingest-sessions"
    )
    ap.add_argument(
        "--sessions-dir",
        default=os.environ.get("DAL_CLAUDE_DIR") or os.path.expanduser("~/.claude/projects"),
    )
    ap.add_argument(
        "--phoenix-store", required=True, help="store URI synced from Phoenix (the proxy channel)"
    )
    ap.add_argument(
        "--jsonl-store", default=None, help="store URI the same folder was ingested into"
    )
    a = ap.parse_args(argv)

    local = set(local_sessions(a.sessions_dir, a.include))
    proxy = store_sessions(a.phoenix_store)
    jsonl = store_sessions(a.jsonl_store) if a.jsonl_store else set()
    n = len(local)
    rows = [
        ("local sessions (assistant turn)", n),
        ("reached via proxy (Phoenix-synced store)", len(local & proxy)),
        ("reached via JSONL ingest", len(local & jsonl) if a.jsonl_store else None),
        ("reached via either", len(local & (proxy | jsonl))),
    ]
    print(f"capture parity, include={a.include!r}")
    for label, v in rows:
        if v is None:
            print(f"  {label:<44} -")
        else:
            print(f"  {label:<44} {v:>5} of {n}  ({100.0 * v / n if n else 0:.1f}%)")
    parity = len(local & (proxy | jsonl)) >= len(local & proxy)
    print("parity:", "holds (store reaches at least what the proxy reaches)" if parity else "FAILS")
    return 0 if parity else 1


if __name__ == "__main__":
    raise SystemExit(main())
