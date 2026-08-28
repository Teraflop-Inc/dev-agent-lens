"""
ATIF export for Claude Code sessions.

Converts a Claude Code session JSONL into an ATIF-v1.7 trajectory
(https://github.com/Teraflop-Inc/harbor rfcs/0001-trajectory-format), which
`harbor-atif2otel` then converts to OpenTelemetry spans for Phoenix or any
OTLP backend.

Why this exists: `harbor-atif2otel` accepts ATIF only, so eval trajectories
could already be backfilled but raw session JSONL could not. This is the
missing mapping layer.

Verified 2026-08-21 against a real 8.5 MB session: 1029 steps, valid ATIF,
1359 spans in Phoenix with span kinds intact and original timestamps preserved
(Aug 4-5, not ingestion date).

Three fidelity limits, measured, that callers should know about:

  * Reasoning text is NOT recoverable. Claude Code writes `thinking` blocks with
    an empty `thinking` field and a signature only (1939 blocks across 12
    sessions, zero with text). Nothing here can recover it.
  * Assistant text is sparse. Most agent turns are pure tool_use with no prose,
    so a minority of LLM spans carry an output value.
  * Non-conversation line types (mode, queue-operation, file-history-*,
    attachment, ai-title, ...) are session bookkeeping and are dropped.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

ATIF_SCHEMA_VERSION = "ATIF-v1.7"
AGENT_NAME = "claude-code"

#: Line types that carry conversation. Everything else is session bookkeeping.
CONVERSATION_TYPES = frozenset({"user", "assistant", "system"})

#: Cap on a single tool result stored in an observation, in characters.
MAX_OBSERVATION_CHARS = 8000


def _content_parts(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize `message.content` to a list of part dicts."""
    content = (message or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [p for p in (content or []) if isinstance(p, dict)]


def _joined_text(parts: Iterable[dict[str, Any]], kind: str = "text") -> str:
    """
    Concatenate the text of all parts of `kind`.

    NB: a `thinking` part stores its text under the ``thinking`` key, not
    ``text``. Reading only ``text`` silently drops reasoning content.
    """
    out: list[str] = []
    for part in parts:
        if part.get("type") != kind:
            continue
        value = part.get(kind) if kind != "text" else None
        out.append(value or part.get("text") or "")
    return "\n".join(x for x in out if x)


def session_to_atif(
    lines: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], Counter[str]]:
    """
    Convert parsed Claude Code session JSONL lines into an ATIF trajectory.

    Args:
        lines: Parsed JSONL objects, in file order (see
            :meth:`ClaudeClient.read_session`).

    Returns:
        ``(trajectory, stats)`` where trajectory is a valid ATIF-v1.7 document
        and stats counts what was mapped and what was dropped.
    """
    stats: Counter[str] = Counter()
    steps: list[dict[str, Any]] = []
    session_id: str | None = None
    version: str | None = None
    model: str | None = None

    # Tool results arrive on later `user` lines; hold them until we can attach
    # them to the agent step that issued the call.
    pending_results: list[dict[str, Any]] = []
    call_to_step: dict[str, int] = {}

    for line in lines:
        line_type = line.get("type")
        stats[f"line:{line_type}"] += 1
        session_id = session_id or line.get("sessionId") or line.get("session_id")
        version = version or line.get("version")

        if line_type not in CONVERSATION_TYPES:
            stats["dropped_non_conversation"] += 1
            continue

        message = line.get("message") or {}
        parts = _content_parts(message)
        model = message.get("model") or model

        tool_results = [p for p in parts if p.get("type") == "tool_result"]
        if line_type == "user" and tool_results and not _joined_text(parts):
            # Claude Code records tool RESULTS on user lines. These are not
            # human turns; treating them as such invents turns that never
            # happened (in one measured session, 112 of 154 user lines).
            pending_results.extend(tool_results)
            stats["tool_result_deferred"] += len(tool_results)
            continue

        _attach_results(steps, call_to_step, pending_results, stats)
        pending_results = []

        source = {"user": "user", "assistant": "agent", "system": "system"}[line_type]
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "source": source,
            "message": _joined_text(parts),
        }
        if line.get("timestamp"):
            step["timestamp"] = line["timestamp"]

        if source == "agent":
            _enrich_agent_step(step, message, parts, call_to_step, len(steps), stats)

        steps.append(step)
        stats[f"step:{source}"] += 1

    _attach_results(steps, call_to_step, pending_results, stats)

    trajectory: dict[str, Any] = {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": session_id,
        "trajectory_id": session_id,
        "agent": {
            "name": AGENT_NAME,
            "version": version or "unknown",
            **({"model_name": model} if model else {}),
        },
        "steps": steps,
    }
    logger.info(
        "[atif] session=%s steps=%d tool_calls=%d dropped=%d",
        session_id,
        len(steps),
        stats["tool_calls"],
        stats["dropped_non_conversation"],
    )
    return trajectory, stats


def _enrich_agent_step(
    step: dict[str, Any],
    message: dict[str, Any],
    parts: list[dict[str, Any]],
    call_to_step: dict[str, int],
    step_index: int,
    stats: Counter[str],
) -> None:
    """Attach reasoning, model, tool calls and token metrics to an agent step."""
    reasoning = _joined_text(parts, "thinking")
    if reasoning:
        step["reasoning_content"] = reasoning
        stats["reasoning_preserved"] += 1

    if message.get("model"):
        step["model_name"] = message["model"]

    calls: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") != "tool_use":
            continue
        call_id = part.get("id") or f"call_{len(calls)}"
        calls.append(
            {
                "tool_call_id": call_id,
                "function_name": part.get("name") or "unknown",
                "arguments": part.get("input") or {},
            }
        )
        call_to_step[call_id] = step_index
        stats["tool_calls"] += 1
    if calls:
        step["tool_calls"] = calls

    usage = message.get("usage") or {}
    if usage:
        step["metrics"] = {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
            "cached_tokens": usage.get("cache_read_input_tokens"),
        }
        stats["metrics_preserved"] += 1


def _attach_results(
    steps: list[dict[str, Any]],
    call_to_step: dict[str, int],
    results: list[dict[str, Any]],
    stats: Counter[str],
) -> None:
    """
    Attach deferred tool results as ATIF observations.

    ATIF ObservationSchema is ``{"results": [{source_call_id, content}]}`` -- a
    keyed array, NOT a flat ``{"content": ...}``. harbor-atif2otel builds its
    observation map from ``results[].source_call_id``, so the flat shape is
    silently ignored: the document still validates, the push still returns 200,
    and every TOOL span lands with an empty output.
    """
    for result in results:
        call_id = result.get("tool_use_id")
        index = call_to_step.get(call_id)
        target = steps[index] if index is not None else (steps[-1] if steps else None)
        if target is None:
            stats["orphan_tool_result"] += 1
            continue
        body = result.get("content")
        if not isinstance(body, str):
            body = json.dumps(body)
        observation = target.setdefault("observation", {"results": []})
        observation["results"].append(
            {
                **({"source_call_id": call_id} if call_id else {}),
                "content": body[:MAX_OBSERVATION_CHARS],
            }
        )
        stats["tool_result_attached"] += 1


def session_file_to_atif(path: str | Path) -> tuple[dict[str, Any], Counter[str]]:
    """Convert a session JSONL file on disk to an ATIF trajectory."""
    path = Path(path)

    def _lines() -> Iterable[dict[str, Any]]:
        with path.open(errors="replace") as handle:
            for raw in handle:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("[atif] skipping unparseable line in %s", path)

    return session_to_atif(_lines())


#: Prefix Claude Code gives a subagent's own transcript file.
SUBAGENT_FILE_PREFIX = "agent-"


def _task_call_map(path: Path) -> dict[str, str]:
    """
    Map ``agentId`` -> the Task ``tool_use_id`` that spawned it.

    A subagent's transcript lands in a sibling ``agent-<agentId>.jsonl``. The
    only thing tying it back to the parent is the ``toolUseResult.agentId`` on
    the parent's Task result line, whose ``tool_result`` block carries the
    originating call id.
    """
    calls: dict[str, str] = {}
    with path.open(errors="replace") as handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            result = line.get("toolUseResult")
            if not isinstance(result, dict) or not result.get("agentId"):
                continue
            content = (line.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    calls[result["agentId"]] = part.get("tool_use_id")
                    break
    return calls


def _attach_subagent_ref(trajectory: dict[str, Any], call_id: str, trajectory_id: str) -> bool:
    """Point the observation for ``call_id`` at an embedded subagent trajectory."""
    for step in trajectory.get("steps", []):
        for result in (step.get("observation") or {}).get("results", []):
            if result.get("source_call_id") == call_id:
                result.setdefault("subagent_trajectory_ref", []).append(
                    {"trajectory_id": trajectory_id}
                )
                return True
    return False


def session_tree_to_atif(
    directory: str | Path,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """
    Convert a directory of Claude Code session JSONL files to ATIF trajectories.

    One trajectory per real session, with each ``agent-<agentId>.jsonl``
    sidechain embedded in ``subagent_trajectories`` and referenced from the Task
    observation that spawned it, so harbor-atif2otel nests the subagent spans
    under their Task span.

    Converting each file standalone instead is a trap worth naming: every
    sidechain records the PARENT's ``sessionId``, and harbor-atif2otel seeds
    trace ids from ``session_id`` and span ids from
    ``{trace_id}:{trajectory_id}``. A directory of 57 files would collapse onto
    2 trace ids with identical span seeds, and the collisions would silently
    drop most of the spans. Giving every subagent a distinct ``trajectory_id``
    is what keeps them apart.

    Returns:
        ``(trajectories, stats)``.
    """
    directory = Path(directory)
    stats: Counter[str] = Counter()

    files = sorted(directory.rglob("*.jsonl"))
    subagents = {
        f.name[len(SUBAGENT_FILE_PREFIX) : -len(".jsonl")]: f
        for f in files
        if f.name.startswith(SUBAGENT_FILE_PREFIX)
    }
    mains = [f for f in files if not f.name.startswith(SUBAGENT_FILE_PREFIX)]
    logger.info(
        "[atif] tree=%s sessions=%d subagent_files=%d",
        directory, len(mains), len(subagents),
    )

    trajectories: list[dict[str, Any]] = []
    for main in mains:
        trajectory, session_stats = session_file_to_atif(main)
        stats.update(session_stats)

        embedded: list[dict[str, Any]] = []
        for agent_id, call_id in _task_call_map(main).items():
            sub_file = subagents.get(agent_id)
            if sub_file is None:
                # Task ran outside the exported window; its transcript is absent.
                stats["subagent_transcript_missing"] += 1
                continue
            sub, _ = session_file_to_atif(sub_file)
            trajectory_id = f"{SUBAGENT_FILE_PREFIX}{agent_id}"
            # session_id stays the parent's (ATIF v1.7 allows sharing it);
            # trajectory_id is what must be unique.
            sub["trajectory_id"] = trajectory_id
            embedded.append(sub)
            if _attach_subagent_ref(trajectory, call_id, trajectory_id):
                stats["subagent_linked"] += 1
            else:
                stats["subagent_unlinked"] += 1
                logger.warning(
                    "[atif] no observation for call_id=%s (agent=%s)", call_id, agent_id
                )
        if embedded:
            trajectory["subagent_trajectories"] = embedded

        logger.info(
            "[atif] session=%s steps=%d subagents=%d",
            trajectory.get("session_id"),
            len(trajectory["steps"]),
            len(embedded),
        )
        trajectories.append(trajectory)
        stats["sessions"] += 1

    return trajectories, stats
