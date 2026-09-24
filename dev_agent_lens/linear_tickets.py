"""The tracker's tickets as a table in the store, so a span's ``ticket`` joins to a kind.

The typed layout stamps ``ticket`` on every span (ENG2-1540); this is the other half of
the cost-per-outcome question: what kind of work was that ticket. Linear holds the
answer as labels (Bug, Feature, Improvement, Research), so ``dal linear-sync`` reads every
issue the key can see and writes one Parquet file under ``tickets/`` in the store. The
typed layout exposes it as the ``tickets`` view; the join is ``spans.ticket =
tickets.ticket``.

One call per 250 issues through the GraphQL API; a workspace of a few thousand issues is a
handful of calls, so the sync loop refreshes it every pass when ``LINEAR_API_KEY`` is
set. The key never lands in the store or on a span.

``kind`` is derived, not stored by Linear: the first of Bug, Feature, Improvement,
Research among the labels, else the ``Bug:`` title prefix, else NULL. Area labels (Lens,
Forge, AIT, infra, ...) stay in ``labels`` and never decide the kind.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

log = logging.getLogger(__name__)

API = "https://api.linear.app/graphql"
KINDS = ("Bug", "Feature", "Improvement", "Research")
PAGE = 250

QUERY = (
    """
query Tickets($after: String) {
  issues(first: %d, after: $after, orderBy: updatedAt, includeArchived: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier title priority estimate
      createdAt startedAt completedAt canceledAt updatedAt archivedAt
      team { key }
      project { name }
      state { name type }
      labels { nodes { name } }
      assignee { name }
      parent { identifier }
    }
  }
}
"""
    % PAGE
)

COLUMNS = (
    "ticket",
    "title",
    "kind",
    "labels",
    "team",
    "project",
    "state",
    "state_type",
    "priority",
    "estimate",
    "assignee",
    "parent",
    "created_at",
    "started_at",
    "completed_at",
    "canceled_at",
    "updated_at",
    "archived_at",
)


def kind_of(labels: list[str], title: str | None) -> str | None:
    have = {lbl.lower() for lbl in labels}
    for k in KINDS:
        if k.lower() in have:
            return k
    t = (title or "").lstrip().lower()
    if t.startswith("bug:") or t.startswith("bug "):
        return "Bug"
    return None


def _graphql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(4):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read())
                log.debug("[linear] page in %.0fms", (time.perf_counter() - t0) * 1000)
                break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                wait = 3.0 * (attempt + 1)
                log.warning("[linear] %d from the API; retry in %.0fs", e.code, wait)
                time.sleep(wait)
                continue
            raise
    if out.get("errors"):
        raise RuntimeError(f"Linear API: {out['errors'][0].get('message', out['errors'])}")
    return out["data"]


def fetch_issues(api_key: str) -> Iterator[dict[str, Any]]:
    """Every issue the key can see, as flat rows in COLUMNS order (a dict each)."""
    after: str | None = None
    pages = 0
    while True:
        data = _graphql(api_key, QUERY, {"after": after})
        page = data["issues"]
        pages += 1
        for n in page["nodes"]:
            labels = [
                lb["name"] for lb in (n.get("labels") or {}).get("nodes", []) if lb.get("name")
            ]
            yield {
                "ticket": n["identifier"],
                "title": n.get("title"),
                "kind": kind_of(labels, n.get("title")),
                "labels": labels,
                "team": (n.get("team") or {}).get("key"),
                "project": (n.get("project") or {}).get("name"),
                "state": (n.get("state") or {}).get("name"),
                "state_type": (n.get("state") or {}).get("type"),
                "priority": n.get("priority"),
                "estimate": n.get("estimate"),
                "assignee": (n.get("assignee") or {}).get("name"),
                "parent": (n.get("parent") or {}).get("identifier"),
                "created_at": n.get("createdAt"),
                "started_at": n.get("startedAt"),
                "completed_at": n.get("completedAt"),
                "canceled_at": n.get("canceledAt"),
                "updated_at": n.get("updatedAt"),
                "archived_at": n.get("archivedAt"),
            }
        if not page["pageInfo"]["hasNextPage"]:
            log.info("[linear] read %d page(s) of up to %d issues", pages, PAGE)
            return
        after = page["pageInfo"]["endCursor"]


def write_tickets(store: Any, con: Any, rows: list[dict[str, Any]]) -> int:
    """Replace the store's ``tickets`` dataset with these rows. Returns the row count."""
    import pandas as pd

    from dev_agent_lens.storage.spanstore.base import quote_literal

    if not rows:
        log.warning("[linear] no issues read; the tickets dataset is left as it was")
        return 0
    df = pd.DataFrame(rows, columns=list(COLUMNS))
    for c in (
        "created_at",
        "started_at",
        "completed_at",
        "canceled_at",
        "updated_at",
        "archived_at",
    ):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    store.attach_duckdb(con)
    store.clear("tickets")
    store.prepare_write("tickets")
    target = quote_literal(store.write_target("tickets").rstrip("/") + "/data_0.parquet")
    con.register("_dal_tickets", df)
    try:
        con.execute(
            f"COPY (SELECT * FROM _dal_tickets) TO {target} "
            "(FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 9)"
        )
    finally:
        con.unregister("_dal_tickets")
    kinds = df["kind"].value_counts(dropna=False).to_dict()
    log.info("[linear] wrote %d tickets to %s; kinds=%s", len(df), store.uri, kinds)
    return len(df)


def attach_tickets(con: Any, store: Any) -> None:
    """A ``tickets`` view on the store's dataset, or an empty one with the same columns."""
    from dev_agent_lens.storage.spanstore.base import quote_literal

    con.execute("DROP VIEW IF EXISTS tickets")
    if store.size_bytes("tickets") == 0:
        cols = ", ".join(f"NULL::{_sql_type(c)} AS {c}" for c in COLUMNS)
        con.execute(f"CREATE VIEW tickets AS SELECT {cols} WHERE false")
        return
    glob = quote_literal(store.read_glob("tickets"))
    con.execute(f"CREATE VIEW tickets AS SELECT * FROM read_parquet({glob})")


def _sql_type(column: str) -> str:
    if column.endswith("_at"):
        return "TIMESTAMPTZ"
    if column == "labels":
        return "VARCHAR[]"
    if column in ("priority", "estimate"):
        return "INTEGER"
    return "VARCHAR"
