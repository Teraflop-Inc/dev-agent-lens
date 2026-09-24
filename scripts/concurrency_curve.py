#!/usr/bin/env python3
# ruff: noqa: E501 -- SQL fixtures retain one complete statement per line.
"""Read concurrency under subagent fan-out (ENG2-1598): N readers, one store, the curve.

An agent that fans subagents across the corpus is N independent processes each opening its
own DuckDB and reading the same Parquet through the same S3 endpoint. That is the shape
measured here: separate processes, not threads, because that is what subagents are. Each
process runs the recipe suite once per round; we report per-recipe wall-clock at p50 and
p95, aggregate throughput, and how far the curve bends as N grows.

    uv run python scripts/concurrency_curve.py --layout typed --n 1,4,16,64 --rounds 2 \
        --json /tmp/curve.json

Tier 0 makes no concurrency promise. Tier 1 needs a number, and this is where it comes from.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

RECIPES = {
    "roster": "SELECT day, count(*) FROM spans GROUP BY 1 ORDER BY 1",
    "by_model": "SELECT {model}, count(*) c FROM spans GROUP BY 1 ORDER BY c DESC, 1 LIMIT 20",
    "tokens": "SELECT day, sum(coalesce({tp},0)) p, sum(coalesce({tc},0)) c FROM spans GROUP BY 1 ORDER BY 1",
    "errors": "SELECT trace_id, span_id FROM spans WHERE status_code='ERROR' ORDER BY 1,2 LIMIT 50",
    "trace": (
        "SELECT trace_id, span_id FROM spans WHERE trace_id IN "
        "(SELECT trace_id FROM spans GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 25) ORDER BY 1,2"
    ),
    "one_session": "SELECT count(*), sum(coalesce({tp},0)) FROM spans WHERE {session} = (SELECT {session} FROM spans WHERE {session} IS NOT NULL LIMIT 1)",
}
BIND = {
    "raw": {
        "model": "json_extract_string(attributes,'$.llm.model_name')",
        "tp": "llm_token_count_prompt",
        "tc": "llm_token_count_completion",
        # the end-user field is sometimes a legacy non-JSON string; hand the parser NULL for those
        "session": "json_extract_string(CASE WHEN json_extract_string(attributes,'$.metadata.user_api_key_end_user_id') LIKE '{%' THEN json_extract_string(attributes,'$.metadata.user_api_key_end_user_id') END,'$.session_id')",
    },
    "typed": {
        "model": "model_name",
        "tp": "tokens_prompt",
        "tc": "tokens_completion",
        "session": "session_id",
    },
}


def worker(args):
    layout_name, rounds, store_uri, idx = args
    import duckdb

    from dev_agent_lens.storage.layouts import get_layout
    from dev_agent_lens.storage.spanstore import open_store

    t_open = time.perf_counter()
    con = duckdb.connect()
    store = open_store(store_uri)
    get_layout(layout_name).attach(con, store)
    open_ms = (time.perf_counter() - t_open) * 1000
    out = {"open_ms": open_ms, "recipes": {k: [] for k in RECIPES}, "errors": 0}
    for _ in range(rounds):
        for name, sql in RECIPES.items():
            t = time.perf_counter()
            try:
                con.execute(sql.format(**BIND[layout_name])).fetchall()
                out["recipes"][name].append((time.perf_counter() - t) * 1000)
            except Exception as e:  # noqa: BLE001
                out["errors"] += 1
                out.setdefault("first_error", f"{type(e).__name__}: {str(e)[:160]}")
    return out


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--layout", default="typed")
    ap.add_argument("--n", default="1,4,16,64")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--store", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    from dev_agent_lens.storage.spanstore import open_store

    store_uri = open_store(a.store).uri
    ns = [int(x) for x in a.n.split(",")]
    results = []
    print(f"store {store_uri}  layout {a.layout}  rounds {a.rounds}")
    print(
        f"{'N':>4} {'wall s':>7} {'open p50':>9} {'errors':>7}  "
        + "  ".join(f"{k[:10]:>10}" for k in RECIPES)
        + "   (p50 ms / p95 ms)"
    )
    for n in ns:
        t0 = time.perf_counter()
        with mp.get_context("spawn").Pool(n) as pool:
            outs = pool.map(worker, [(a.layout, a.rounds, store_uri, i) for i in range(n)])
        wall = time.perf_counter() - t0
        per = {k: [ms for o in outs for ms in o["recipes"][k]] for k in RECIPES}
        errors = sum(o["errors"] for o in outs)
        row = {
            "n": n,
            "wall_s": round(wall, 2),
            "open_p50_ms": round(pct([o["open_ms"] for o in outs], 0.5)),
            "errors": errors,
            "first_error": next((o.get("first_error") for o in outs if o.get("first_error")), None),
            "recipes": {
                k: {"p50": round(pct(v, 0.5) or 0), "p95": round(pct(v, 0.95) or 0)}
                for k, v in per.items()
            },
            "queries_per_s": round(sum(len(v) for v in per.values()) / wall, 1),
        }
        results.append(row)
        print(
            f"{n:>4} {wall:>7.1f} {row['open_p50_ms']:>9} {errors:>7}  "
            + "  ".join(
                f"{row['recipes'][k]['p50']:>4}/{row['recipes'][k]['p95']:<5}" for k in RECIPES
            )
            + f"   {row['queries_per_s']} q/s"
        )
        if row["first_error"]:
            print(f"     first error: {row['first_error']}")
    base = results[0]
    print("\nbend: p95 of the slowest recipe at each N relative to N=1")
    for r in results:
        worst = max(r["recipes"], key=lambda k: r["recipes"][k]["p95"])
        b = base["recipes"][worst]["p95"] or 1
        print(
            f"  N={r['n']:<3} {worst:<12} {r['recipes'][worst]['p95']:>6} ms  {r['recipes'][worst]['p95'] / b:>5.1f}x"
        )
    if a.json:
        json.dump(
            {
                "store": store_uri,
                "layout": a.layout,
                "rounds": a.rounds,
                "results": results,
                "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            open(a.json, "w"),
            indent=2,
        )
    return 1 if any(r["errors"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
