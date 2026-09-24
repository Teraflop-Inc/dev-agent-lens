"""The tracker's tickets as a store table, joinable on spans.ticket (ENG2-1540)."""

from __future__ import annotations

import json

import duckdb

from dev_agent_lens.linear_tickets import (
    COLUMNS,
    attach_tickets,
    fetch_issues,
    kind_of,
    write_tickets,
)
from dev_agent_lens.storage.layouts import get_layout
from dev_agent_lens.storage.spanstore import open_store


def test_kind_is_the_first_type_label_else_the_bug_prefix_else_nothing():
    assert kind_of(["Lens", "Bug", "Feature"], "x") == "Bug"
    assert kind_of(["AIT", "feature"], "x") == "Feature"
    assert kind_of(["Research", "Improvement"], "x") == "Improvement"
    assert kind_of([], "Bug: search returns nothing") == "Bug"
    assert kind_of(["Lens"], "DAL can't answer cost questions") is None


def _issue(ident, title, labels, kind_label=None, **kw):
    return {
        "identifier": ident,
        "title": title,
        "priority": 2,
        "estimate": None,
        "createdAt": "2026-08-25T20:15:42.734Z",
        "startedAt": None,
        "completedAt": kw.get("completedAt"),
        "canceledAt": None,
        "updatedAt": "2026-09-15T21:44:32.768Z",
        "archivedAt": None,
        "team": {"key": ident.split("-")[0]},
        "project": {"name": "Dev-Agent-Lens"},
        "state": {"name": "Done" if kw.get("completedAt") else "Todo", "type": "completed"},
        "labels": {"nodes": [{"name": lb} for lb in labels]},
        "assignee": {"name": "Ada"},
        "parent": {"identifier": "ENG2-1621"},
    }


def test_fetch_walks_every_page_and_flattens(monkeypatch):
    pages = [
        {
            "issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [_issue("ENG2-1540", "cost per outcome", ["Lens"])],
            }
        },
        {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    _issue(
                        "ENG2-1572",
                        "Three small capture debts",
                        ["Bug"],
                        completedAt="2026-09-17T16:01:48.325Z",
                    )
                ],
            }
        },
    ]
    seen = []

    def fake(api_key, query, variables):
        seen.append(variables["after"])
        return pages[len(seen) - 1]

    monkeypatch.setattr("dev_agent_lens.linear_tickets._graphql", fake)
    rows = list(fetch_issues("k"))
    assert seen == [None, "c1"]
    assert [r["ticket"] for r in rows] == ["ENG2-1540", "ENG2-1572"]
    assert rows[0]["kind"] is None and rows[0]["labels"] == ["Lens"]
    assert rows[1]["kind"] == "Bug" and rows[1]["completed_at"] == "2026-09-17T16:01:48.325Z"
    assert rows[1]["parent"] == "ENG2-1621" and rows[1]["team"] == "ENG2"
    assert set(rows[0]) == set(COLUMNS)


def test_tickets_land_as_one_table_and_join_to_the_typed_layouts_ticket(tmp_path):
    import pandas as pd

    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    # nobody synced yet: the view exists, empty, with every column typed
    attach_tickets(con, store)
    assert con.execute("SELECT count(*) FROM tickets").fetchone() == (0,)
    assert [r[0] for r in con.execute("DESCRIBE tickets").fetchall()] == list(COLUMNS)

    rows = [
        {
            **{c: None for c in COLUMNS},
            "ticket": "ENG2-1540",
            "title": "cost",
            "kind": None,
            "labels": ["Lens"],
            "team": "ENG2",
            "project": "Dev-Agent-Lens",
            "priority": 3,
            "created_at": "2026-08-25T20:15:42.734Z",
        },
        {
            **{c: None for c in COLUMNS},
            "ticket": "ENG2-1572",
            "title": "debts",
            "kind": "Bug",
            "labels": ["Bug"],
            "team": "ENG2",
            "project": None,
            "priority": 3,
            "created_at": "2026-09-01T00:00:00Z",
            "completed_at": "2026-09-17T16:01:48.325Z",
        },
    ]
    assert write_tickets(store, con, rows) == 2
    # a second sync replaces, never appends
    assert write_tickets(store, con, rows[:1]) == 1
    attach_tickets(con, store)
    assert con.execute("SELECT count(*) FROM tickets").fetchone() == (1,)
    assert write_tickets(store, con, rows) == 2

    # spans with a ticket, built through the typed layout, join on it
    n = 3
    batch = pd.DataFrame(
        {
            "context.span_id": [f"s{i}" for i in range(n)],
            "context.trace_id": ["t1", "t1", "t2"],
            "parent_id": [None] * n,
            "name": ["litellm_request"] * n,
            "span_kind": ["LLM"] * n,
            "start_time": pd.to_datetime(["2026-09-15T10:00:00Z"] * n, utc=True),
            "end_time": pd.to_datetime(["2026-09-15T10:00:01Z"] * n, utc=True),
            "status_code": ["OK"] * n,
            "status_message": [""] * n,
            "attributes": [
                json.dumps(
                    {
                        "input": {"value": "/ticket ENG2-1572"},
                        "llm": {"token_count": {"prompt": 100, "completion": 10}},
                    }
                ),
                json.dumps(
                    {
                        "input": {"value": "more on ENG2-1572"},
                        "llm": {"token_count": {"prompt": 200, "completion": 20}},
                    }
                ),
                json.dumps(
                    {
                        "input": {"value": "/ticket ENG2-1540"},
                        "llm": {"token_count": {"prompt": 50, "completion": 5}},
                    }
                ),
            ],
            "events": ["[]"] * n,
            "cumulative_error_count": [0] * n,
            "cumulative_llm_token_count_prompt": [None] * n,
            "cumulative_llm_token_count_completion": [None] * n,
            "llm_token_count_prompt": [100.0, 200.0, 50.0],
            "llm_token_count_completion": [10.0, 20.0, 5.0],
        }
    )
    store.append_frame(con, batch, "spans_raw", source="t")
    lay = get_layout("typed")
    lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
    lay.attach(con, store)  # the typed layout's attach brings `tickets` along
    got = con.execute(
        """SELECT coalesce(t.kind, 'untyped') AS kind, sum(s.tokens_prompt)::BIGINT AS prompt
           FROM spans s LEFT JOIN tickets t USING (ticket) GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    assert got == [("Bug", 300), ("untyped", 50)]
