"""
Human-Turn Extraction

Separates what a PERSON typed from what the agent emitted on their behalf.

Claude Code writes a lot of text into the conversation that looks exactly like a
user turn but isn't: web-search templates it generates for its own WebSearch
tool, statusline summarisation prompts, task notifications, system reminders,
and tool results. In the ENG2-1393 corpus these were **46% of everything that
parsed as a user message** — so any "what did this engineer ask Claude?" query
that skips this filter is roughly half wrong, in a way that looks plausible.

Two named offenders, both of which read as perfectly human at a glance:

  * ``Perform a web search for the query: <q>`` — emitted by Claude Code when
    the AGENT decides to search. Nobody typed it.
  * ``Describe your most recent action in 3-5 words...`` — the statusline
    summariser prompting itself.

The filter is deliberately conservative in one direction: it is far worse to
admit agent noise (which silently inflates and distorts an engineer's prompt
history) than to drop a borderline human turn. When in doubt, drop.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Agent-emitted text that masquerades as a human turn. Matched against the
# START of the turn (after stripping), except where noted.
_AGENT_PREFIXES: tuple[str, ...] = (
    # Claude Code emits this when the AGENT fires WebSearch — not a human turn.
    "perform a web search for the query",
    # Statusline / summariser prompting itself.
    "describe your most recent action",
    # Idle-return recap prompt.
    "the user stepped away",
    "the user has been idle",
    # Suggestion engine.
    "[suggestion mode",
    # Session-boot noise.
    "the date has changed",
    "caveat: the messages below were generated",
)

# Structural markers: if a turn contains any of these it is machine plumbing
# (tool results, harness injections), regardless of position.
_MACHINE_MARKERS: tuple[str, ...] = (
    "tool_use_id",
    "<system-reminder>",
    "<task-notification>",
    "<command-name>",
    "<local-command-stdout>",
    "[request interrupted by user]",
)

# A real human turn is rarely shorter than this once whitespace is collapsed.
# Below it we get "ok", "y", "continue" — real, but carrying no analysable
# signal, and easily confused with fragments of machine output.
MIN_HUMAN_TURN_CHARS = 12


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_human_turn(text: str | None, *, min_chars: int = MIN_HUMAN_TURN_CHARS) -> bool:
    """True if `text` looks like something a person actually typed.

    Conservative by design: prefers dropping a borderline human turn over
    admitting agent-emitted text. See module docstring for why.
    """
    if not text:
        return False

    normalized = _normalize(text)
    if len(normalized) < min_chars:
        return False

    lowered = normalized.lower()

    for marker in _MACHINE_MARKERS:
        if marker in lowered:
            return False

    for prefix in _AGENT_PREFIXES:
        if lowered.startswith(prefix):
            return False

    return True


def extract_human_turns(
    texts: list[str | None],
    *,
    min_chars: int = MIN_HUMAN_TURN_CHARS,
) -> list[str]:
    """Filter a list of candidate turns down to the ones a human typed.

    Logs what it dropped and why — the drop rate is itself a data-quality
    signal, and a silently-filtered corpus is how you end up reporting a
    confidently wrong number.
    """
    kept: list[str] = []
    dropped = 0

    for text in texts:
        if is_human_turn(text, min_chars=min_chars):
            kept.append(_normalize(str(text)))
        else:
            dropped += 1

    total = len(texts)
    if total:
        logger.info(
            "[prompts] kept %d/%d human turns (dropped %d agent-emitted/noise, %.0f%%)",
            len(kept),
            total,
            dropped,
            100.0 * dropped / total,
        )
        if dropped / total > 0.4:
            # Not an error — the ENG2-1393 corpus legitimately sits near 50% —
            # but a drop rate this high means the caller's upstream extraction
            # is pulling in a lot of machine text, which is worth knowing.
            logger.warning(
                "[prompts] %.0f%% of candidate turns were agent-emitted — "
                "check the upstream extraction if this is unexpected",
                100.0 * dropped / total,
            )

    return kept
