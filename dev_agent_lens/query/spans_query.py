"""Span listing and statistics behind `dal query-spans`, over either physical home.

Two places hold spans today:

  * the per-source export files `dal export-parquet` writes under `~/.dal/data/parquet/`
    (one flattened row per span, `session_id` and `llm_model_name` already columns), and
  * the span store, where `dal sync` lands the native shape and `session_id` / model live
    inside the `attributes` JSON (raw layout) or as typed columns (typed layout).

`SpansRelation` names the FROM clause and the column expressions for one of those, so the
filters and aggregates below are written once. User-supplied values always travel as bound
parameters; only our own expressions are spliced into the SQL.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dev_agent_lens.storage.spanstore.base import quote_literal

log = logging.getLogger(__name__)


@dataclass
class SpansRelation:
    """A queryable set of spans: the FROM clause plus how to reach each filterable column."""

    from_sql: str
    label: str  # what to print: a path, or a store URI + layout
    session_id: str = "session_id"
    model: str = "llm_model_name"
    name: str = "name"
    status_code: str = "status_code"
    start_time: str = "start_time"
    span_id: str = "span_id"
    size_bytes: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def open_export(con: Any, spans_path: str | Path) -> SpansRelation:
    """The export-parquet shape: a partitioned directory (`spans/source=<name>/`) or a
    legacy single `<name>_spans.parquet` file."""
    p = Path(spans_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"spans not found: {p}")
    if p.is_dir():
        glob, size = f"{p}/**/*.parquet", sum(f.stat().st_size for f in p.rglob("*.parquet"))
    else:
        glob, size = str(p), p.stat().st_size
    rel = SpansRelation(
        from_sql=f"read_parquet({quote_literal(glob)}, hive_partitioning=true, union_by_name=true)",
        label=str(p),
        size_bytes=size,
    )
    log.debug("[query-spans] export relation %s (%.1f MB)", p, size / 1e6)
    return rel


# per-span session id in the raw layout, propagated across the trace exactly as the typed
# layout's identity table does: only some spans in a trace carry the end-user metadata.
# A trace with no identity is its own session, keyed by trace_id, which is how the export
# files count sessions too; the two homes must agree on "Sessions".
_RAW_EUID = "json_extract_string(attributes,'$.metadata.user_api_key_end_user_id')"
# Producer paths tried in order, matching the typed layout's _IDENT: LiteLLM's end-user
# JSON string, then the Claude Code hooks producer's claude.session_id, then OpenInference's
# session.id. A producer this list has not met is its own session by trace_id.
_RAW_SESSION = (
    f"coalesce(max(CASE WHEN {_RAW_EUID} LIKE '{{%' "
    f"THEN json_extract_string({_RAW_EUID},'$.session_id') END) OVER (PARTITION BY trace_id), "
    "max(coalesce(json_extract_string(attributes,'$.claude.session_id'), "
    "json_extract_string(attributes,'$.session.id'))) OVER (PARTITION BY trace_id), "
    "trace_id)"
)


def open_store_spans(con: Any, store: Any, layout: str) -> SpansRelation:
    """The span store through the given layout. Attaches the layout's views to `con`."""
    from dev_agent_lens.storage.layouts import get_layout

    primary = {"raw": "spans_raw", "typed": "spans_typed"}[layout]
    size = store.size_bytes(primary)
    if size == 0:
        raise FileNotFoundError(f"the store at {store.uri} holds no {primary} data yet")
    get_layout(layout).attach(con, store)
    if layout == "typed":
        rel = SpansRelation(
            from_sql="(SELECT *, coalesce(session_id, trace_id) AS _session_id FROM spans)",
            label=f"{store.uri} [typed]",
            session_id="_session_id",
            model="model_name",
            size_bytes=size,
        )
    else:
        # a subquery so the window-derived session id is a plain column to the filters
        rel = SpansRelation(
            from_sql=f"(SELECT *, {_RAW_SESSION} AS _session_id FROM spans)",
            label=f"{store.uri} [raw]",
            session_id="_session_id",
            model="json_extract_string(attributes,'$.llm.model_name')",
            size_bytes=size,
        )
    log.debug("[query-spans] store relation %s layout=%s (%.1f MB)", store.uri, layout, size / 1e6)
    return rel


def _where(
    rel: SpansRelation,
    *,
    session_id: str | None = None,
    status_code: str | None = None,
    model_name: str | None = None,
    name_pattern: str | None = None,
) -> tuple[str, list[Any], dict[str, str]]:
    conds, params, shown = [], [], {}
    if session_id:
        conds.append(f"{rel.session_id} = ?")
        params.append(session_id)
        shown["session_id"] = session_id
    if status_code:
        conds.append(f"{rel.status_code} = ?")
        params.append(status_code)
        shown["status_code"] = status_code
    if model_name:
        conds.append(f"lower({rel.model}) LIKE ?")
        params.append(f"%{model_name.lower()}%")
        shown["model"] = model_name
    if name_pattern:
        conds.append(f"{rel.name} ILIKE ?")
        params.append(f"%{name_pattern}%")
        shown["name"] = name_pattern
    return (" WHERE " + " AND ".join(conds)) if conds else "", params, shown


def spans_stats(con: Any, rel: SpansRelation, **filters: str | None) -> dict[str, Any]:
    """Counts by status, name and model, plus totals, under the filters."""
    t0 = time.perf_counter()
    where, params, shown = _where(rel, **filters)
    base = f"FROM {rel.from_sql}{where}"
    total, sessions = con.execute(
        f"SELECT count(*), count(DISTINCT {rel.session_id}) {base}", params
    ).fetchone()

    def counts(expr: str, limit: int) -> dict[str, int]:
        rows = con.execute(
            f"SELECT coalesce({expr}, '(none)') AS k, count(*) AS n {base} "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(limit)}",
            params,
        ).fetchall()
        return {k: n for k, n in rows}

    out = {
        "filters": shown,
        "label": rel.label,
        "size_bytes": rel.size_bytes,
        "total_spans": total,
        "session_count": sessions,
        "status_code_counts": counts(rel.status_code, 10),
        "span_name_counts": counts(rel.name, 15),
        "top_models": counts(rel.model, 10),
    }
    log.info(
        "[query-spans] stats total=%d sessions=%d filters=%s in %.1fms",
        total,
        sessions,
        shown,
        (time.perf_counter() - t0) * 1000,
    )
    return out


def spans_list(
    con: Any, rel: SpansRelation, *, limit: int = 50, **filters: str | None
) -> list[dict[str, Any]]:
    """Newest-first span rows under the filters, as dicts with a stable column set."""
    t0 = time.perf_counter()
    where, params, shown = _where(rel, **filters)
    cols = [
        f"{rel.session_id} AS session_id",
        f"{rel.span_id} AS span_id",
        f"{rel.name} AS name",
        f"{rel.status_code} AS status_code",
        f"{rel.start_time} AS start_time",
        f"{rel.model} AS model",
    ]
    rows = con.execute(
        f"SELECT {', '.join(cols)} FROM {rel.from_sql}{where} "
        f"ORDER BY {rel.start_time} DESC NULLS LAST LIMIT {int(limit)}",
        params,
    ).fetchall()
    names = ["session_id", "span_id", "name", "status_code", "start_time", "model"]
    out = [dict(zip(names, r)) for r in rows]
    log.info(
        "[query-spans] list rows=%d filters=%s limit=%d in %.1fms",
        len(out),
        shown,
        limit,
        (time.perf_counter() - t0) * 1000,
    )
    return out
