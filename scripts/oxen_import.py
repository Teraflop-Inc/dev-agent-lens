#!/usr/bin/env python3
"""One-time import of the Dev-Agent-Lens-2 Oxen repo into the span store (ENG2-1614).

Oxen is leaving; this reads it once. The repo holds ``parquet/spans/source=<name>/
week=<W>/part-*.parquet`` in the flat shape the 2025 DAL wrote: one column per hot
field plus ``raw_attributes_json``, a JSON object whose keys are dotted paths under an
``attributes.`` prefix. This turns each row into the store's raw shape: the dotted keys
re-nested into one ``attributes`` object, the flat hot fields folded in under their
OpenInference paths when the raw object lacks them, ``session_id`` under ``session.id``
so the typed layout's identity coalesce finds it. Nothing is dropped from the raw
object; a Python-repr string stays a Python-repr string, because that is what was
recorded.

Provenance rides on every row: ``source`` is ``oxen/<name>``, ``oxen_commit`` is the
head the files were downloaded at, ``oxen_path`` the file inside the repo,
``imported_at`` when. Rows already present (same span_id, same source) land once;
the report counts them.

    oxen download Teraflop/Dev-Agent-Lens-2 'parquet/spans/source=arize-sightline' -o work/
    uv run python scripts/oxen_import.py --work work --sources arize-sightline \
        --commit a287818683b5a82a64b5632b9b84187d --report work/report.json

``--store`` overrides the configured store. Rows with no ``start_time`` cannot be
partitioned; the 2025 writer's ``week=unknown`` rows recover it from the raw object's
``time`` (and ``end_time`` from ``latency_ms``), marked under ``attributes._import``.
Rows with neither are listed in the report as unparseable, with their span ids.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import os
import sys
import time
from typing import Any

log = logging.getLogger("oxen_import")

PREFIX = "attributes."
# flat column -> attribute path, used only when the raw object lacks the path
FOLD = {
    "input_value": "input.value",
    "output_value": "output.value",
    "input_messages": "llm.input_messages",
    "output_messages": "llm.output_messages",
    "llm_model_name": "llm.model_name",
    "session_id": "session.id",
}


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in flat.items():
        key = k[len(PREFIX) :] if k.startswith(PREFIX) else k
        node = out
        parts = key.split(".")
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[parts[-1]] = v
    return out


def _get(obj: dict[str, Any], path: str) -> Any:
    node: Any = obj
    for p in path.split("."):
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    return node


def _set(obj: dict[str, Any], path: str, value: Any) -> None:
    node = obj
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def row_to_raw(
    r: dict[str, Any], *, source: str, commit: str, path: str, now: str
) -> dict[str, Any] | None:
    """One Oxen row -> one store row, or None when it cannot be placed (no start_time)."""
    raw = r.get("raw_attributes_json")
    try:
        flat = json.loads(raw) if raw else {}
        if not isinstance(flat, dict):
            flat = {"_raw": flat}
    except (TypeError, ValueError):
        flat = {"_raw_unparsed": raw}
    attrs = _nest(flat)
    start, end, note = r.get("start_time"), r.get("end_time"), None
    if start is None:
        # The 2025 writer filed spans with no start_time under week=unknown (2,555 of
        # 2,656 sightline rows) but kept the emitter's own `time` and `latency_ms` in
        # the raw object. Use them, and say so on the row.
        t = flat.get("time")
        try:
            start = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00")) if t else None
        except ValueError:
            start = None
        if start is None:
            return None
        note = {"start_time_from": "raw.time"}
        lat = flat.get("latency_ms")
        if end is None and isinstance(lat, (int, float)):
            end = start + dt.timedelta(milliseconds=float(lat))
            note["end_time_from"] = "raw.time + latency_ms"
    if note:
        attrs["_import"] = note
    for col, apath in FOLD.items():
        v = r.get(col)
        if v not in (None, "") and _get(attrs, apath) is None:
            _set(attrs, apath, v)
    return {
        "span_id": r["span_id"],
        "trace_id": r.get("trace_id"),
        "parent_id": r.get("parent_id") or None,
        "name": r.get("name"),
        "span_kind": r.get("span_kind") or None,
        "start_time": start,
        "end_time": end,
        "status_code": r.get("status_code") or None,
        "status_message": None,
        "attributes": json.dumps(attrs, default=str),
        "events": "[]",
        "cumulative_error_count": None,
        "cumulative_llm_token_count_prompt": None,
        "cumulative_llm_token_count_completion": None,
        "llm_token_count_prompt": r.get("llm_token_count_prompt"),
        "llm_token_count_completion": r.get("llm_token_count_completion"),
        "source": f"oxen/{source}",
        "oxen_commit": commit,
        "oxen_path": path,
        "imported_at": now,
    }


def import_source(
    work: str, source: str, *, store: Any, con: Any, commit: str, batch: int
) -> dict[str, Any]:
    import pandas as pd

    base = os.path.join(work, "parquet", "spans", f"source={source}")
    files = sorted(glob.glob(os.path.join(base, "**", "*.parquet"), recursive=True))
    rep: dict[str, Any] = {
        "source": source,
        "files": len(files),
        "rows_read": 0,
        "rows_landed": 0,
        "rows_already_present": 0,
        "unparseable": [],
        "duplicate_ids_in_oxen": 0,
    }
    if not files:
        log.warning("[oxen:%s] no parquet under %s", source, base)
        return rep
    import duckdb

    seen: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    t0 = time.perf_counter()
    # A separate connection for reading. A DuckDB connection holds one open result;
    # append_frame runs statements on `con`, which would clobber the cursor being
    # paged through and hand back garbage rows (seen: read=2657 of 2654, bad=3).
    read_con = duckdb.connect()
    for f in files:
        rel = os.path.relpath(f, work)
        cur = read_con.execute(f"SELECT * FROM read_parquet('{f}')")
        cols = [d[0] for d in cur.description]
        while True:
            chunk = cur.fetchmany(batch)
            if not chunk:
                break
            rows, out = [dict(zip(cols, c)) for c in chunk], []
            for r in rows:
                rep["rows_read"] += 1
                sid = r.get("span_id")
                if sid in seen:
                    # Oxen holds the same span twice (a re-sync in 2025). Land the first,
                    # count the second; the report reconciles read = landed + dup + bad.
                    rep["duplicate_ids_in_oxen"] += 1
                    continue
                seen.add(sid)
                converted = row_to_raw(r, source=source, commit=commit, path=rel, now=now)
                if converted is None:
                    rep["unparseable"].append({"span_id": sid, "path": rel, "why": "no start_time"})
                    continue
                out.append(converted)
            if out:
                df = pd.DataFrame(out)
                df["start_time"] = pd.to_datetime(df["start_time"], utc=True)
                df["end_time"] = pd.to_datetime(df["end_time"], utc=True, errors="coerce")
                n = store.append_frame(con, df, "spans_raw", source=f"oxen/{source}")
                rep["rows_landed"] += n
                rep["rows_already_present"] += len(out) - n
        log.info(
            "[oxen:%s] %s read=%d landed=%d present=%d bad=%d in %.0fs",
            source,
            rel,
            rep["rows_read"],
            rep["rows_landed"],
            rep["rows_already_present"],
            len(rep["unparseable"]),
            time.perf_counter() - t0,
        )
    rep["elapsed_s"] = round(time.perf_counter() - t0, 1)
    return rep


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--work",
        required=True,
        help="directory `oxen download` wrote into (holds parquet/spans/...)",
    )
    ap.add_argument(
        "--sources", required=True, help="comma-separated source names (the source=<name> dirs)"
    )
    ap.add_argument(
        "--commit", required=True, help="Oxen commit id the files came from; recorded on every row"
    )
    ap.add_argument(
        "--store", default=None, help="store URI; default resolves like `dal store show`"
    )
    ap.add_argument("--batch", type=int, default=20000)
    ap.add_argument(
        "--report", default=None, help="write the per-source reconciliation here as JSON"
    )
    a = ap.parse_args(argv)

    import duckdb

    from dev_agent_lens.storage.spanstore import open_store

    store = open_store(a.store)
    store.ensure()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    reports = [
        import_source(a.work, s.strip(), store=store, con=con, commit=a.commit, batch=a.batch)
        for s in a.sources.split(",")
        if s.strip()
    ]
    for r in reports:
        print(
            f"{r['source']:<24} files={r['files']:<4} read={r['rows_read']:<9} "
            f"landed={r['rows_landed']:<9} present={r['rows_already_present']:<7} "
            f"dup_in_oxen={r['duplicate_ids_in_oxen']:<6} bad={len(r['unparseable'])}"
        )
    if a.report:
        with open(a.report, "w") as fh:
            json.dump({"commit": a.commit, "store": store.uri, "sources": reports}, fh, indent=2)
    return 0 if all(r["files"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
