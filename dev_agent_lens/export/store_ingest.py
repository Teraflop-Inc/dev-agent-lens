"""Land Claude Code sessions straight in the span store, no Phoenix in the loop.

`dal ingest-sessions` converts a session JSONL to ATIF and, until now, could only push
the resulting OTLP spans to a Phoenix endpoint. Alex's five-minute story (ENG2-1612):
a new DAL user uploads their Claude folder and gets an answer back. That has to work on a
laptop with nothing but the store, so the same spans now also land in `spans_raw`
directly, in the raw shape `dal sync` writes, stamped with a `source`.

The conversion is the OTLP protobuf the push path already builds; only the sink differs,
so a session lands identically whichever way it goes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# OTLP span kind enum -> OpenInference-style label the store already uses. Phoenix keys
# span_kind off the openinference.span.kind attribute when present; the OTLP kind is the
# fallback.
_OTLP_KIND = {0: "UNKNOWN", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}


def _any_value(v: Any) -> Any:
    """Decode an OTLP AnyValue into a plain Python value."""
    kind = v.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_any_value(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _any_value(kv.value) for kv in v.kvlist_value.values}
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, kind)


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """`llm.token_count.prompt` -> {"llm": {"token_count": {"prompt": ...}}}, which is how
    Phoenix stores OpenInference attributes and what every store recipe expects."""
    out: dict[str, Any] = {}
    for key, value in flat.items():
        node = out
        *parents, leaf = key.split(".")
        for p in parents:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                node[p] = nxt
            node = nxt
        node[leaf] = value
    return out


def _ts(nanos: int) -> datetime:
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc)


def spans_to_rows(spans: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    """OTLP protobuf spans -> rows in the store's raw shape (the direct-client shape
    `append_frame` accepts: `context.span_id`, `context.trace_id`, JSON text attributes)."""
    rows: list[dict[str, Any]] = []
    for _scope, span in spans:
        flat = {kv.key: _any_value(kv.value) for kv in span.attributes}
        nested = _nest(flat)
        events = [
            {
                "name": e.name,
                "timestamp": _ts(e.time_unix_nano).isoformat(),
                "attributes": _nest({kv.key: _any_value(kv.value) for kv in e.attributes}),
            }
            for e in span.events
        ]
        tc = (
            nested.get("llm", {}).get("token_count", {})
            if isinstance(nested.get("llm"), dict)
            else {}
        )
        rows.append(
            {
                "context.span_id": span.span_id.hex(),
                "context.trace_id": span.trace_id.hex(),
                "parent_id": span.parent_span_id.hex() or None,
                "name": span.name,
                "span_kind": flat.get("openinference.span.kind")
                or _OTLP_KIND.get(span.kind, "UNKNOWN"),
                "start_time": _ts(span.start_time_unix_nano),
                "end_time": _ts(span.end_time_unix_nano),
                "status_code": _STATUS.get(span.status.code, "UNSET"),
                "status_message": span.status.message or "",
                "attributes": json.dumps(nested, default=str),
                "events": json.dumps(events, default=str),
                "cumulative_error_count": 1 if span.status.code == 2 else 0,
                "cumulative_llm_token_count_prompt": tc.get("prompt")
                if isinstance(tc, dict)
                else None,
                "cumulative_llm_token_count_completion": tc.get("completion")
                if isinstance(tc, dict)
                else None,
                "llm_token_count_prompt": tc.get("prompt") if isinstance(tc, dict) else None,
                "llm_token_count_completion": tc.get("completion")
                if isinstance(tc, dict)
                else None,
            }
        )
    return rows


def land_trajectories(
    trajectories: list[dict[str, Any]],
    project: str,
    user_id: str | None = None,
    store: Any = None,
    con: Any = None,
) -> dict[str, int]:
    """Convert trajectories with the push path's own `build_spans`, then append the rows
    to `spans_raw` under `source=<project>`. Returns counts the ingest report can add."""
    import duckdb
    import pandas as pd

    from dev_agent_lens.export.otlp import _count_duplicate_ids, build_spans
    from dev_agent_lens.storage.spanstore import open_store

    started = time.perf_counter()
    _resource, spans, clamped = build_spans(trajectories, project, user_id)
    if not spans:
        return {"spans_total": 0, "spans_sent": 0, "clamped_spans": 0}
    duplicates = _count_duplicate_ids(spans)
    if duplicates:
        raise ValueError(f"{duplicates} spans share a (trace_id, span_id); refusing to land them")
    store = store or open_store()
    con = con or duckdb.connect()
    store.ensure()
    df = pd.DataFrame(spans_to_rows(spans))
    n = store.append_frame(con, df, "spans_raw", source=project)
    logger.info(
        "[store-ingest] landed %d spans as source=%s in %.0fms",
        n,
        project,
        (time.perf_counter() - started) * 1000,
    )
    return {"spans_total": len(spans), "spans_sent": n, "clamped_spans": clamped}
