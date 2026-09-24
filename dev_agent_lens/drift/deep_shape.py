"""Second-level fingerprint: the shape INSIDE string-valued typed columns.

The structural sweep answers "did this path change jsonb type" and finds zero changes in
four months. That answer is blind by construction to double-encoded fields, where the
jsonb type is `string` forever while the JSON *inside* the string changes underneath.

This corpus has at least one such field on a typed column, and it drifted:
`llm.invocation_parameters` went from ~90% unparseable to ~96% parseable JSON between
July and mid-August, swapping `temperature` for `thinking` in its key set. Every day of
that transition, jsonb_typeof said `string`.

So a drift detector that only fingerprints structure would have missed the one event that
actually mattered. This module is the fix: for each configured deep path, parse the string
and fingerprint the shape (parseable? object or scalar? which keys?).
"""

from __future__ import annotations

import collections
import json
import logging

log = logging.getLogger(__name__)

# Typed columns whose stored value is a STRING that we then parse. Each is a place a
# structural check is blind and a deep check is required.
DEEP_PATHS = {
    "llm.invocation_parameters": "thinking_type, thinking_budget_tokens, max_tokens",
    "metadata.user_api_key_end_user_id": "account_uuid, session_id (identity)",
}


def shape(v: str | None, key_limit: int = 3) -> str:
    """Canonical shape of a possibly-double-encoded value.

    Deliberately coarse: we want to detect that the shape moved, not to enumerate every
    key. Key names are included because a rename is the failure mode that goes silently
    NULL under a typed schema.
    """
    if v is None:
        return "absent"
    s = v.strip()
    if not (s.startswith("{") or s.startswith("[")):
        return "bare-scalar"
    try:
        o = json.loads(s)
    except Exception:
        return "unparseable"  # the double-encoding trap; a JSON reader gets nothing
    if isinstance(o, dict):
        return "json-object{" + ",".join(sorted(o.keys())[:key_limit]) + "}"
    return f"json-{type(o).__name__}"


def fingerprint(values: list[str | None]) -> dict[str, float]:
    """Shape distribution for one window. Fractions, so windows of different size compare."""
    if not values:
        return {}
    c = collections.Counter(shape(v) for v in values)
    n = len(values)
    return {k: round(v / n, 4) for k, v in c.most_common()}


def compare(
    baseline: dict[str, float], observed: dict[str, float], move_threshold: float = 0.25
) -> list[tuple[str, str, str]]:
    """Findings when a shape distribution moves.

    A NEW dominant shape is the loud case: it means the producer changed what it writes
    and a typed column that parses the old shape now silently yields NULL.
    """
    out = []
    base_dom = max(baseline, key=baseline.get) if baseline else None
    obs_dom = max(observed, key=observed.get) if observed else None
    if base_dom and obs_dom and base_dom != obs_dom:
        out.append(
            (
                "ERROR",
                "SHAPE_SHIFT",
                f"dominant shape {base_dom} ({baseline[base_dom]:.0%}) -> "
                f"{obs_dom} ({observed[obs_dom]:.0%})",
            )
        )
    for k, v in observed.items():
        b = baseline.get(k, 0.0)
        if abs(v - b) >= move_threshold:
            out.append(("WARN", "SHAPE_DRIFT", f"{k}: {b:.0%} -> {v:.0%}"))
    if observed.get("unparseable", 0) - baseline.get("unparseable", 0) >= move_threshold:
        out.append(
            ("ERROR", "BECAME_UNPARSEABLE", "a JSON reader now gets nothing from this field")
        )
    return out
