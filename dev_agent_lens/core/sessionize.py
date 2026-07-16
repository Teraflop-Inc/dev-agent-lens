"""
Working-Session Segmentation

Turns a stream of spans into the thing a human means by "a working session".

**`session_id` is not a working session.** It is a Claude Code *conversation
thread*, and it survives `--continue` / `--resume` indefinitely. In the
ENG2-1393 corpus one engineer's thread ran for 28 days: 1,990 of his 2,109
spans carried a single `session_id`, totalling 498M tokens. Grouping by
`session_id` to answer "show me their working sessions" returns one meaningless
month-long blob plus a scatter of 6-span noise — and it does so silently, which
is the dangerous part.

What an engineering manager means by a session is a *contiguous block of work*:
spans separated by less than some idle gap. Segmenting the same corpus on a
30-minute gap recovered ~46 real sessions with plausible shapes (median ~30
min), which is what made the learning-vs-building breakdown legible at all.

The gap is a policy choice, not a law. 30 minutes is the default because it
matches the coffee/lunch/meeting rhythm of a working day without splitting a
single train of thought. Tune it per question; just don't use `session_id`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_IDLE_GAP = timedelta(minutes=30)

# Blocks smaller than this are almost always background chatter (statusline
# summarisers, suggestion prompts) rather than a person sitting down to work.
DEFAULT_MIN_SPANS = 4


@dataclass(frozen=True)
class WorkingSession:
    """A contiguous block of work, recovered from span timestamps."""

    block: int
    start: pd.Timestamp
    end: pd.Timestamp
    spans: int
    tokens: int

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def segment(
    spans: pd.DataFrame,
    *,
    time_col: str = "start_time",
    idle_gap: timedelta = DEFAULT_IDLE_GAP,
) -> pd.DataFrame:
    """Add a `work_block` column: a contiguous-work id derived from idle gaps.

    A new block starts whenever the silence since the previous span exceeds
    `idle_gap`. Returns a copy sorted by time. Empty input returns an empty
    frame with the column present, so callers can group unconditionally.
    """
    if spans.empty:
        out = spans.copy()
        out["work_block"] = pd.Series(dtype="int64")
        return out

    if time_col not in spans.columns:
        raise KeyError(
            f"sessionize.segment: no {time_col!r} column "
            f"(have: {list(spans.columns)})"
        )

    out = spans.sort_values(time_col).copy()
    times = pd.to_datetime(out[time_col])

    gap = times.diff()
    # First span always opens a block (its gap is NaT).
    starts_block = gap.isna() | (gap > idle_gap)
    out["work_block"] = starts_block.cumsum().astype("int64")

    n_blocks = int(out["work_block"].nunique())
    logger.info(
        "[sessionize] %d spans -> %d working sessions (idle gap %.0f min)",
        len(out),
        n_blocks,
        idle_gap.total_seconds() / 60.0,
    )

    # Loud, because this is the trap the module exists to prevent: if the caller
    # ALSO has a session_id column, show how badly it misrepresents the work.
    if "session_id" in out.columns:
        n_sessions = int(out["session_id"].nunique())
        if n_sessions and n_blocks > n_sessions * 2:
            logger.warning(
                "[sessionize] %d session_id(s) collapse into %d working "
                "sessions — session_id is a resumed conversation thread, not a "
                "working session; do not group by it",
                n_sessions,
                n_blocks,
            )

    return out


def summarize(
    spans: pd.DataFrame,
    *,
    time_col: str = "start_time",
    token_col: str = "tokens",
    idle_gap: timedelta = DEFAULT_IDLE_GAP,
    min_spans: int = DEFAULT_MIN_SPANS,
) -> list[WorkingSession]:
    """Segment `spans` and return one WorkingSession per block.

    Blocks with fewer than `min_spans` spans are dropped as background chatter.
    The count of dropped blocks is logged rather than silently swallowed — a
    caller comparing totals deserves to know the difference is real.
    """
    segmented = segment(spans, time_col=time_col, idle_gap=idle_gap)
    if segmented.empty:
        return []

    times = pd.to_datetime(segmented[time_col])
    tokens = (
        segmented[token_col]
        if token_col in segmented.columns
        else pd.Series(0, index=segmented.index)
    )

    sessions: list[WorkingSession] = []
    dropped = 0

    for block, idx in segmented.groupby("work_block").groups.items():
        if len(idx) < min_spans:
            dropped += 1
            continue
        sessions.append(
            WorkingSession(
                block=int(block),
                start=times.loc[idx].min(),
                end=times.loc[idx].max(),
                spans=len(idx),
                tokens=int(tokens.loc[idx].fillna(0).sum()),
            )
        )

    if dropped:
        logger.info(
            "[sessionize] dropped %d block(s) under %d spans (background chatter)",
            dropped,
            min_spans,
        )

    return sessions
