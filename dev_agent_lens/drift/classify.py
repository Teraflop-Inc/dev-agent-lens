"""Classify structural change in a fingerprint series into blast-radius categories.

  APPEAR     new path                       -> benign: lands in the verbatim overflow column
  VANISH     stops being produced           -> SILENT NULL if the path feeds a typed column
  GAP        absent, then returns           -> SILENT NULL for the gap
  POLYMORPH  >1 non-null type the same day  -> a modelling problem, present from day one
  TYPEFLIP   type changes between days      -> SILENT WRONG: TRY_CAST returns NULL, no error
  NULLED     a typed path's real types go non-empty -> empty and stay empty
                                            -> SILENT NULL with the key still present, the
                                               case presence-based checks cannot see

Coverage classes separate the three causes of absence, only one of which is drift:
  STABLE   present on all but <=2 live days
  SPARSE   max coverage under 5%: feature-gated (web search, cache); absence is data
  GAPPY    absent on many days: needs a dated cause, or it is drift
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass

from dev_agent_lens.drift.fingerprints import Day, live_days, real_types

log = logging.getLogger(__name__)

# Attribute paths that feed the ENG2-1589 typed columns. An event on one of these is the
# blast radius; an event anywhere else lands in `attributes_rest` and costs nothing.
TYPED_PATHS: dict[str, str] = {
    "llm.model_name": "model_name",
    "llm.provider": "provider",
    "llm.system": "llm_system",
    "llm.is_streaming": "is_streaming",
    "llm.token_count.prompt": "tokens_prompt",
    "llm.token_count.completion": "tokens_completion",
    "llm.invocation_parameters": "thinking_type / thinking_budget_tokens / max_tokens",
    "input.value": "input_value",
    "output.value": "output_value",
    "metadata.user_api_key_end_user_id": "account_uuid + session_id",
    "metadata.usage_object": "tokens_cache_read / tokens_cache_write",
    "claude.session_id": "claude_session_id",
    "claude.surface": "claude_surface",
    "claude.cwd": "claude_cwd",
    "claude_code_tool_name": "tool_name",
    "llm.anthropic": "payload_*_ref (content-addressed blob)",
}


def typed_column(path: str) -> str | None:
    if path in TYPED_PATHS:
        return TYPED_PATHS[path]
    for k, v in TYPED_PATHS.items():
        if path.startswith(k + "."):
            return v
    return None


@dataclass
class Event:
    kind: str
    day: str
    path: str
    note: str = ""

    @property
    def typed(self) -> str | None:
        return typed_column(self.path)


def classify(days: dict[str, Day]) -> list[Event]:
    live = live_days(days)
    if not live:
        return []
    by_path: dict[str, dict[str, set[str]]] = collections.defaultdict(dict)
    for d in live:
        for path, pd_ in days[d].paths.items():
            by_path[path][d] = pd_.types
    events: list[Event] = []
    for path, byday in sorted(by_path.items()):
        present = [d for d in live if d in byday]
        first, last = present[0], present[-1]
        if first != live[0]:
            events.append(Event("APPEAR", first, path))
        if last != live[-1]:
            events.append(Event("VANISH", last, path, f"last seen {last}"))
        span = [d for d in live if first <= d <= last]
        missing = [d for d in span if d not in byday]
        if missing:
            events.append(Event("GAP", missing[0], path, f"{len(missing)}/{len(span)} days absent"))
        poly = [d for d in present if len(real_types(byday[d])) > 1]
        seq: list[tuple[str, tuple[str, ...]]] = []
        for d in present:
            r = real_types(byday[d])
            if not r:
                continue
            key = tuple(sorted(r))
            if not seq or seq[-1][1] != key:
                seq.append((d, key))
        if poly:
            types = sorted({t for d in poly for t in real_types(byday[d])})
            events.append(
                Event("POLYMORPH", poly[0], path, f"{'|'.join(types)} on {len(poly)} days")
            )
        if len(seq) > 1:
            events.append(
                Event(
                    "TYPEFLIP", seq[1][0], path, " -> ".join(f"{d}:{'|'.join(k)}" for d, k in seq)
                )
            )
        # NULLED: real types were non-empty, then empty for every remaining live day the
        # path is present on. One null-only day on a sparse path is noise; a tail of them on
        # a typed column is the silent-NULL failure the whole package exists to surface.
        if typed_column(path):
            had = [d for d in present if real_types(byday[d])]
            if had:
                after = [d for d in present if d > had[-1]]
                if len(after) >= 2 and all(not real_types(byday[d]) for d in after):
                    events.append(
                        Event(
                            "NULLED",
                            after[0],
                            path,
                            f"no non-null value on {len(after)} days since {had[-1]}",
                        )
                    )
    log.info(
        "[drift:classify] %d live days, %d paths, %s",
        len(live),
        len(by_path),
        dict(collections.Counter(e.kind for e in events)),
    )
    return events


def coverage_classes(days: dict[str, Day]) -> dict[str, tuple[str, int, float, float]]:
    """path -> (class, days_present, median_cov, max_cov), typed paths only."""
    live = live_days(days)
    out = {}
    for path in sorted({p for d in live for p in days[d].paths}):
        if not typed_column(path):
            continue
        covs = sorted(days[d].paths[path].cov_nonnull for d in live if path in days[d].paths)
        absent = len(live) - len(covs)
        if covs[-1] < 0.05:
            klass = "SPARSE"
        elif absent <= 2:
            klass = "STABLE"
        else:
            klass = "GAPPY"
        out[path] = (klass, len(covs), covs[len(covs) // 2], covs[-1])
    return out
