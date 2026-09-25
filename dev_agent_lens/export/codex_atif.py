"""
ATIF export for Codex sessions (ENG2-402).

Converts a Codex CLI session file (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``)
into the same ATIF-v1.7 trajectory shape :mod:`dev_agent_lens.export.atif` builds for
Claude Code, so both go through one ``harbor-atif2otel`` path into the store. The agent
name on the trajectory (``codex``) becomes the span service name, which is what lets a
query split work by harness.

The mapping follows Harbor's Codex converter (``harbor/agents/installed/codex.py``),
reimplemented against plain dicts and extended to the older file format. Two formats
exist on disk, both measured on 50 real sessions (2026-09-23):

  * **Envelope** (current): ``{"timestamp", "type", "payload"}``. Conversation lives in
    ``response_item`` records; ``event_msg`` records are the UI's echo of the same
    turns and are dropped so nothing is counted twice.
  * **Flat** (older CLIs): the first line is ``{"id", "timestamp", "instructions"}``
    and every later line is a bare item (``{"type": "message", ...}``) with no
    timestamp of its own.

One model response becomes one agent step, the same unit as a Claude Code assistant
line: its reasoning summary, assistant text and every tool call it issued. Tool outputs
attach to that step as ATIF observations keyed by ``call_id``. Token usage arrives in a
``token_count`` event after the response and attaches to the step it measured.

Fidelity limits, measured:

  * Reasoning text is only the **summary**. The full chain is ``encrypted_content`` and
    is not recoverable.
  * Flat-format steps carry the session start time, not their own: the format recorded
    none per line. Their order is exact, their clock time is not.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from dev_agent_lens.export.atif import ATIF_SCHEMA_VERSION, MAX_OBSERVATION_CHARS

logger = logging.getLogger(__name__)

AGENT_NAME = "codex"

#: Items the model produced. Consecutive ones form a single agent step.
AGENT_ITEM_TYPES = frozenset(
    {"reasoning", "function_call", "custom_tool_call", "web_search_call", "local_shell_call"}
)
OUTPUT_ITEM_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})

#: Codex injects context as user-role messages. They are not human turns: recording them
#: as such invents turns nobody typed (the same trap the Claude converter documents).
INJECTED_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md",
    "<INSTRUCTIONS>",
)

_CWD_IN_CONTEXT = re.compile(r"<cwd>(.*?)</cwd>", re.S)


def _item(record: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
    """``(record_type, item, timestamp)`` for either on-disk format."""
    if "payload" in record and isinstance(record["payload"], dict):
        return record.get("type"), record["payload"], record.get("timestamp")
    return "response_item" if record.get("type") else None, record, record.get("timestamp")


def _text(content: Any) -> str:
    """Join the text parts of a message's ``content``."""
    if isinstance(content, str):
        return content
    out = [
        part.get("text") or ""
        for part in content or []
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text")
    ]
    return "\n".join(x for x in out if x)


def _output_text(raw: Any) -> str:
    """A tool output as text. Older CLIs wrapped it as a JSON ``{"output": ...}`` string."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(parsed, dict) and "output" in parsed:
            inner = parsed["output"]
            return inner if isinstance(inner, str) else json.dumps(inner)
        return raw
    if isinstance(raw, list):
        joined = _text(raw)
        return joined or json.dumps(raw)
    return json.dumps(raw)


def _arguments(raw: Any) -> Any:
    """Tool arguments as an object when they are JSON, else the raw string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw if raw is not None else {}


def read_codex_cwd(path: Path, max_lines: int = 200) -> str | None:
    """The working directory a Codex session recorded, or None.

    ``session_meta.cwd`` and ``turn_context.cwd`` in the envelope format; the flat format
    only states it inside the injected ``<environment_context>`` message.
    """
    try:
        with path.open(errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index >= max_lines:
                    break
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                _, item, _ = _item(record)
                cwd = item.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
                if item.get("type") == "message":
                    match = _CWD_IN_CONTEXT.search(_text(item.get("content")))
                    if match:
                        return match.group(1).strip()
    except OSError as exc:
        logger.warning("[codex-atif] cannot read %s: %s", path, exc)
    return None


def _usage_metrics(usage: dict[str, Any]) -> dict[str, Any]:
    """One model call's `last_token_usage` as step metrics. OpenAI's input_tokens already
    includes the cached part, the convention the proxy's LiteLLM spans use."""
    return {
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "cached_tokens": usage.get("cached_input_tokens"),
        "extra": {"reasoning_output_tokens": usage.get("reasoning_output_tokens")},
    }


def _add_usage(step: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    """Add one call's metrics to a step's, field by field; returns the step."""
    held = step.get("metrics")
    if not held:
        step["metrics"] = {**call, "extra": dict(call.get("extra") or {})}
        return step
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
        if call.get(key) is not None:
            held[key] = (held.get(key) or 0) + call[key]
    extra = held.setdefault("extra", {})
    for key, value in (call.get("extra") or {}).items():
        if value is not None:
            extra[key] = (extra.get(key) or 0) + value
    return step


def codex_session_to_atif(
    records: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], Counter[str]]:
    """
    Convert parsed Codex session records into an ATIF trajectory.

    Returns:
        ``(trajectory, stats)``: a valid ATIF-v1.7 document, and counts of what was
        mapped and what was dropped.
    """
    stats: Counter[str] = Counter()
    steps: list[dict[str, Any]] = []
    session_id: str | None = None
    version: str | None = None
    git_branch: str | None = None
    model: str | None = None
    clock: str | None = None
    last_total: dict[str, Any] | None = None

    current: dict[str, Any] | None = None  # the agent step being assembled
    call_to_step: dict[str, int] = {}
    # A model call whose count arrives before any agent step (the first call of a
    # session, a reasoning-only call) waits here for the next agent step.
    pending: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            current["step_id"] = len(steps) + 1
            if clock and "timestamp" not in current:
                current["timestamp"] = clock
            steps.append(current)
            stats["step:agent"] += 1
            current = None

    def agent_step(timestamp: str | None) -> dict[str, Any]:
        nonlocal current, pending
        if current is None:
            current = {"source": "agent", "message": ""}
            if timestamp or clock:
                current["timestamp"] = timestamp or clock
            if model:
                current["model_name"] = model
            if pending:
                _add_usage(current, pending["metrics"])
                pending = None
        return current

    for record in records:
        if not isinstance(record, dict):
            continue
        record_type, item, timestamp = _item(record)
        clock = timestamp or clock
        kind = item.get("type")
        stats[f"record:{record_type}/{kind}"] += 1

        # Flat-format header: {"id", "timestamp", "instructions"}.
        if record_type is None and "id" in item and "instructions" in item:
            session_id = session_id or item.get("id")
            clock = clock or item.get("timestamp")
            continue

        if record_type == "session_meta":
            session_id = session_id or item.get("id")
            version = version or item.get("cli_version")
            branch = (item.get("git") or {}).get("branch")
            if branch and branch != "HEAD":
                git_branch = git_branch or branch
            continue
        if record_type == "turn_context":
            model = item.get("model") or model
            continue
        if record_type == "event_msg":
            if kind == "token_count":
                info = item.get("info") or {}
                total = info.get("total_token_usage")
                usage = info.get("last_token_usage")
                # Codex re-emits an unchanged count (rate-limit refreshes); only a change
                # in the running total is a new model call.
                if usage and total and total != last_total:
                    last_total = total
                    target = current or (
                        steps[-1] if steps and steps[-1]["source"] == "agent" else None
                    )
                    call = _usage_metrics(usage)
                    # Two calls can land on one step (a reasoning-only call, then the
                    # answer). Replacing lost the first: output tokens summed ~6% under
                    # Codex's own total on a real session. Add them instead.
                    if target is not None:
                        _add_usage(target, call)
                    else:
                        pending = _add_usage(pending or {}, call)
                        stats["metrics_pending"] += 1
                    stats["metrics_preserved"] += 1
            else:
                stats["dropped_event_msg"] += 1
            continue
        if record_type != "response_item":
            stats["dropped_other"] += 1
            continue

        if kind == "message":
            role = item.get("role")
            text = _text(item.get("content"))
            if role == "assistant":
                step = agent_step(timestamp)
                step["message"] = "\n".join(x for x in (step["message"], text) if x)
                continue
            flush()
            source = (
                "user"
                if role == "user" and not text.lstrip().startswith(INJECTED_PREFIXES)
                else "system"
            )
            step = {"step_id": len(steps) + 1, "source": source, "message": text}
            if timestamp or clock:
                step["timestamp"] = timestamp or clock
            steps.append(step)
            stats[f"step:{source}"] += 1
            continue

        if kind in AGENT_ITEM_TYPES:
            step = agent_step(timestamp)
            if kind == "reasoning":
                summary = "\n".join(
                    s.get("text") or "" for s in item.get("summary") or [] if isinstance(s, dict)
                )
                if summary:
                    step["reasoning_content"] = "\n".join(
                        x for x in (step.get("reasoning_content"), summary) if x
                    )
                    stats["reasoning_preserved"] += 1
                continue
            call_id = item.get("call_id") or item.get("id") or f"call_{stats['tool_calls']}"
            if kind == "web_search_call":
                name, arguments = "web_search", item.get("action") or {}
            elif kind == "custom_tool_call":
                name, arguments = item.get("name") or "unknown", item.get("input") or ""
            else:
                name, arguments = (
                    item.get("name") or kind,
                    _arguments(item.get("arguments", item.get("action"))),
                )
            step.setdefault("tool_calls", []).append(
                {"tool_call_id": call_id, "function_name": name, "arguments": arguments}
            )
            call_to_step[call_id] = len(steps)  # index it will take when flushed
            stats["tool_calls"] += 1
            continue

        if kind in OUTPUT_ITEM_TYPES:
            flush()
            call_id = item.get("call_id")
            index = call_to_step.get(call_id)
            target = steps[index] if index is not None and index < len(steps) else None
            if target is None:
                stats["orphan_tool_result"] += 1
                continue
            target.setdefault("observation", {"results": []})["results"].append(
                {
                    **({"source_call_id": call_id} if call_id else {}),
                    "content": _output_text(item.get("output"))[:MAX_OBSERVATION_CHARS],
                }
            )
            stats["tool_result_attached"] += 1
            continue

        stats["dropped_item"] += 1

    flush()
    if pending:
        last_agent = next((st for st in reversed(steps) if st["source"] == "agent"), None)
        if last_agent is not None:
            _add_usage(last_agent, pending["metrics"])
        else:
            stats["metrics_dropped_no_agent_step"] += 1

    trajectory: dict[str, Any] = {
        "schema_version": ATIF_SCHEMA_VERSION,
        "session_id": session_id,
        "trajectory_id": session_id,
        **({"git_branch": git_branch} if git_branch else {}),
        "agent": {
            "name": AGENT_NAME,
            "version": version or "unknown",
            **({"model_name": model} if model else {}),
        },
        "steps": steps,
    }
    if last_total:
        trajectory["final_metrics"] = {
            "total_prompt_tokens": last_total.get("input_tokens"),
            "total_completion_tokens": last_total.get("output_tokens"),
            "total_cached_tokens": last_total.get("cached_input_tokens"),
        }
    logger.info(
        "[codex-atif] session=%s steps=%d tool_calls=%d attached=%d orphans=%d metrics=%d",
        session_id,
        len(steps),
        stats["tool_calls"],
        stats["tool_result_attached"],
        stats["orphan_tool_result"],
        stats["metrics_preserved"],
    )
    return trajectory, stats


def codex_session_file_to_atif(path: str | Path) -> tuple[dict[str, Any], Counter[str]]:
    """Convert a Codex session file on disk to an ATIF trajectory."""
    path = Path(path)

    def _records() -> Iterable[dict[str, Any]]:
        with path.open(errors="replace") as handle:
            for number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    yield json.loads(raw)
                except ValueError:
                    logger.warning("[codex-atif] %s:%d is not JSON; skipped", path.name, number)

    trajectory, stats = codex_session_to_atif(_records())
    # The flat format's header id is the session; the envelope's session_meta usually
    # matches the file name's trailing uuid. Fall back to it when neither was recorded.
    if not trajectory["session_id"]:
        match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.name
        )
        trajectory["session_id"] = trajectory["trajectory_id"] = (
            match.group(1) if match else path.stem
        )
    return trajectory, stats
