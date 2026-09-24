#!/usr/bin/env python3
# ruff: noqa: E501 -- SQL fixtures retain one complete statement per line.
"""A producer the schema has never met: the arbitrary stress test for ENG2-1610.

Appends N spans to the configured store under one `source`, with a vocabulary nothing in
`typed.py` knows: nested objects, arrays, a field whose JSON type changes row to row, a
double-encoded string, a 200 KB attribute, unicode keys, rows with no events, and a
handful that reuse LiteLLM's `llm.model_name` so overlap is exercised too. Then the typed
build must land every row, scope by `source`, and leave the unknown vocabulary whole in
`attributes_rest`. Alex, 2026-09-08: "the fake case that is an arbitrary stress test plus
also the real work load will provide us some clarity."

    DAL_SPAN_STORE=file:///tmp/proof uv run python scripts/fake_producer.py --rows 2000 --seed 7
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_rows(n: int, seed: int, day0: datetime):
    rnd = random.Random(seed)
    rows = []
    trace = None
    for i in range(n):
        if i % 7 == 0:
            trace = uuid.uuid4().hex
        t = day0 + timedelta(minutes=i * 3.7)
        poly = [i, str(i), float(i) / 3, None, {"deep": i}, [i, i + 1]][i % 6]
        attrs = {
            "widget": {
                "kind": rnd.choice(["gizmo", "sprocket", "flange"]),
                "score": rnd.random(),
                "tags": [rnd.choice(["a", "b", "c"]) for _ in range(rnd.randint(0, 4))],
                "nested": {"level": {"deeper": {"value": i}}},
                "poly": poly,
                "wrapped": json.dumps({"inner": {"n": i, "ok": i % 2 == 0}}),
                "sesión_id": f"s-{i // 50}",
                "huge": ("x" * 200_000) if i % 500 == 1 else None,
            },
            "openinference": {"span": {"kind": "UNKNOWN"}},
        }
        if i % 13 == 0:
            attrs["llm"] = {
                "model_name": "widget-llm-1",
                "token_count": {"prompt": i, "completion": 2 * i},
            }
        rows.append(
            {
                "context.span_id": uuid.uuid4().hex[:16],
                "context.trace_id": trace,
                "parent_id": None if i % 7 == 0 else "p" + uuid.uuid4().hex[:15],
                "name": rnd.choice(["widget.build", "widget.test", "widget.ship"]),
                "span_kind": "UNKNOWN",
                "start_time": t,
                "end_time": t + timedelta(milliseconds=rnd.randint(1, 5000)),
                "status_code": rnd.choice(["OK", "OK", "OK", "ERROR"]),
                "status_message": "",
                "attributes": json.dumps(attrs, ensure_ascii=False),
                "events": None if i % 9 == 0 else json.dumps([{"name": "widget.event", "n": i}]),
                "cumulative_error_count": 0,
                "cumulative_llm_token_count_prompt": 0,
                "cumulative_llm_token_count_completion": 0,
                "llm_token_count_prompt": i if i % 13 == 0 else None,
                "llm_token_count_completion": 2 * i if i % 13 == 0 else None,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--rows", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--source", default="fake-widget")
    ap.add_argument("--day", default="2026-09-01")
    a = ap.parse_args()
    import duckdb
    import pandas as pd

    from dev_agent_lens.storage.spanstore import open_store

    day0 = datetime.fromisoformat(a.day).replace(tzinfo=timezone.utc)
    df = pd.DataFrame(make_rows(a.rows, a.seed, day0))
    store = open_store()
    store.ensure()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    n = store.append_frame(con, df, "spans_raw", source=a.source)
    print(
        f"[fake-producer] appended {n} rows as source={a.source} to {store.uri}; "
        f"{df['context.trace_id'].nunique()} traces, days {df['start_time'].min().date()}..{df['start_time'].max().date()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
