#!/usr/bin/env python3
"""Build BOTH span-store layouts at matched settings and report the honest difference.

This exists because ENG2-1589's published 9.6x storage win for the typed layout was an
artefact of the two sides being built by different scripts at different compression levels.
Nobody could re-run them together, so nobody caught it for a day. Now it is one command.

    uv run python scripts/compare_layouts.py \
        --from-parquet '/path/to/spans/**/*.parquet' --levels 1,3,9

Reports, per layout per level: bytes hot, bytes cold, build time, and the recipe suite.
Sizes are written single-threaded so they are byte-reproducible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb  # noqa: E402

from dev_agent_lens.storage.layouts import get_layout  # noqa: E402
from dev_agent_lens.storage.spanstore import open_store  # noqa: E402

log = logging.getLogger("compare")

# Questions we actually ask of DAL. Written to run against BOTH layouts unchanged, which
# is only possible because the typed layout keeps a verbatim overflow column.
RECIPES = {
    "roster": "SELECT day, count(*) FROM spans GROUP BY 1 ORDER BY 1",
    "by_model": ("SELECT {model}, count(*) c FROM spans GROUP BY 1 ORDER BY c DESC, 1 LIMIT 20"),
    "tokens": (
        "SELECT day, sum(COALESCE({tp},0)) p, sum(COALESCE({tc},0)) c "
        "FROM spans GROUP BY 1 ORDER BY 1"
    ),
    "thinking": "SELECT {think} t, count(*) c FROM spans GROUP BY 1 ORDER BY c DESC",
    "errors": (
        "SELECT trace_id, span_id FROM spans WHERE status_code='ERROR' ORDER BY 1,2 LIMIT 50"
    ),
    "trace": (
        "SELECT trace_id, span_id, COALESCE(parent_span_id,'') FROM spans "
        "WHERE trace_id IN (SELECT trace_id FROM spans GROUP BY 1 "
        "ORDER BY count(*) DESC, 1 LIMIT 25) ORDER BY 1,2"
    ),
}

# The same question costs a JSON parse on raw and a column read on typed. That difference
# is the entire point of typing, so the SQL differs while the ANSWER must not.
BIND = {
    "raw": {
        "model": "json_extract_string(attributes,'$.llm.model_name')",
        "tp": "llm_token_count_prompt",
        "tc": "llm_token_count_completion",
        "think": (
            "json_extract_string(json_extract_string(attributes,"
            "'$.llm.invocation_parameters'),'$.thinking.type')"
        ),
        "trace_id": "trace_rowid",
        "parent_span_id": "parent_id",
    },
    "typed": {
        "model": "model_name",
        "tp": "tokens_prompt",
        "tc": "tokens_completion",
        "think": "thinking_type",
        "trace_id": "trace_id",
        "parent_span_id": "parent_span_id",
    },
}


def sql_for(layout: str, rid: str) -> str:
    b = BIND[layout]
    s = RECIPES[rid].format(**{k: v for k, v in b.items()})
    if layout == "raw":
        s = s.replace("trace_id", "trace_rowid").replace("parent_span_id", "parent_id")
    return s


def run(layout_name: str, store_uri: str, source: str, level: int, runs: int) -> dict:
    layout = get_layout(layout_name)
    store = open_store(store_uri)
    store.ensure()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    built = layout.build(con, source, store, zstd_level=level, deterministic=True)
    layout.attach(con, store)
    out = {
        "layout": layout_name,
        "level": level,
        "rows": built.rows,
        "hot_mb": round(built.bytes_hot / 1e6, 1),
        "cold_mb": round(built.bytes_cold / 1e6, 1),
        "total_mb": round(built.bytes_total / 1e6, 1),
        "build_s": round(built.elapsed_ms / 1000, 1),
        "detail": built.detail,
        "recipes": {},
    }
    for rid in RECIPES:
        s = sql_for(layout_name, rid)
        times, rows = [], None
        try:
            for _ in range(runs):
                t0 = time.perf_counter()
                rows = con.execute(s).fetchall()
                times.append((time.perf_counter() - t0) * 1000)
            h = hashlib.sha256()
            for r in rows:
                h.update(("\x1f".join("" if v is None else str(v) for v in r) + "\x1e").encode())
            out["recipes"][rid] = {
                "ms": round(min(times), 1),
                "rows": len(rows),
                "digest": h.hexdigest()[:16],
            }
        except Exception as e:  # noqa: BLE001
            out["recipes"][rid] = {"error": f"{type(e).__name__}: {str(e)[:90]}"}
            log.warning("[compare] %s %s FAILED: %s", layout_name, rid, e)
        log.info("[compare] %-6s L%-2d %-9s %s", layout_name, level, rid, out["recipes"][rid])
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-parquet", dest="source", required=True)
    ap.add_argument("--store", default=f"file://{tempfile.mkdtemp(prefix='dal-compare-')}")
    ap.add_argument("--levels", default="3,9")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    levels = [int(x) for x in a.levels.split(",")]

    results = []
    for lvl in levels:
        for lay in ("raw", "typed"):
            results.append(run(lay, f"{a.store}/L{lvl}/{lay}", a.source, lvl, a.runs))

    print("\n" + "=" * 96)
    print(
        f"{'level':<7}{'layout':<8}{'rows':>9}{'hot MB':>9}{'cold MB':>9}{'total MB':>10}"
        f"{'build s':>9}   suite ms"
    )
    for r in results:
        suite = sum(v.get("ms", 0) for v in r["recipes"].values())
        print(
            f"{r['level']:<7}{r['layout']:<8}{r['rows']:>9,}{r['hot_mb']:>9.1f}"
            f"{r['cold_mb']:>9.1f}{r['total_mb']:>10.1f}{r['build_s']:>9.1f}   {suite:.1f}"
        )
    print("\n--- the answers must not differ, only the cost ---")
    mismatch = 0
    for lvl in levels:
        raw = next(r for r in results if r["level"] == lvl and r["layout"] == "raw")
        typ = next(r for r in results if r["level"] == lvl and r["layout"] == "typed")
        for rid in RECIPES:
            # not `a, b`: that shadowed the parsed args and crashed the final print
            d_raw, d_typ = raw["recipes"][rid].get("digest"), typ["recipes"][rid].get("digest")
            same = d_raw is not None and d_raw == d_typ
            mismatch += 0 if same else 1
            print(f"  level {lvl:<3} {rid:<10} {'IDENTICAL' if same else 'MISMATCH'}")
    print("\n--- typed vs raw, at matched level ---")
    for lvl in levels:
        raw = next(r for r in results if r["level"] == lvl and r["layout"] == "raw")
        typ = next(r for r in results if r["level"] == lvl and r["layout"] == "typed")
        sr = sum(v.get("ms", 0) for v in raw["recipes"].values())
        st = sum(v.get("ms", 0) for v in typ["recipes"].values())
        print(
            f"  level {lvl:<3} size hot {raw['hot_mb'] / typ['hot_mb']:5.2f}x   "
            f"size total {raw['total_mb'] / typ['total_mb']:5.2f}x   "
            f"suite {sr / max(st, 0.01):6.1f}x faster"
        )
    print("\n--- per recipe (ms) ---")
    hdr = f"{'recipe':<10}" + "".join(
        f"{'L' + str(lvl) + ' ' + w:>13}" for lvl in levels for w in ("raw", "typed")
    )
    print(hdr)
    for rid in RECIPES:
        row = f"{rid:<10}"
        for lvl in levels:
            for lay in ("raw", "typed"):
                r = next(x for x in results if x["level"] == lvl and x["layout"] == lay)
                v = r["recipes"][rid]
                row += f"{v.get('ms', 'ERR'):>13}"
        print(row)
    if a.json_out:
        json.dump(results, open(a.json_out, "w"), indent=1)
        print(f"\n[compare] -> {a.json_out}")
    print(
        "PASS: layouts agree on every answer"
        if not mismatch
        else f"FAIL: {mismatch} answer(s) differ"
    )
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
