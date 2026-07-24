"""
Live session reconstruction from Supabase-Phoenix spans (ENG2-1435).

Reconstructs a single Claude Code session **directly from live Postgres** (no
`dal sync` / parquet step) into the same JSONL event records that
`export/markdown_renderer.py` consumes, so the markdown output is at parity with
`dal reconstruct` / `dal chain-export --format markdown`.

Why this exists separately from `analysis/chains.export_chain_to_jsonl`:
the chain pipeline reconstructs user/assistant turns only from
`Claude_Code_Internal_Prompt_*` / `raw_gen_ai_request` spans. Live Supabase-
Phoenix instead stores plain `litellm_request` spans, where each span is a
single LLM exchange with the turn's user message in `attributes.llm.input_messages`
and the assistant reply in `attributes.llm.output_messages` (content is a
stringified block list). Walking those spans in `start_time` order reconstructs
the conversation, pairing each assistant `tool_use` with the `tool_result` that
arrives on the next call.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from dev_agent_lens.analysis.threads import (
    COMPACTION_CONTINUATION_MARKER,
    COMPACTION_SUMMARY_MARKER,
    COMPACTION_TASK_MARKER,
)
from dev_agent_lens.export.markdown_litellm import (
    _extract_input_messages_array,
    _extract_input_value,
    _extract_model,
    _extract_output_messages_array,
    _parse_message_content,
    _parse_timestamp,
)

logger = logging.getLogger(__name__)


# User-message text blocks that are internal Claude Code plumbing, not human
# turns. Mirrors the filters used by the parquet reconstruction pipeline so the
# output is at parity.
def _is_human_turn(text: str) -> bool:
    """Return True if a user text block is a genuine human turn."""
    stripped = text.strip()
    if len(stripped) <= 10:
        return False
    if stripped.startswith("<system"):
        return False
    if stripped.startswith("Command:") and "\nOutput:" in stripped:
        return False
    if stripped.startswith("{") and stripped.endswith("}"):
        return False
    if stripped.startswith("Files modified by"):
        return False
    if stripped.startswith("Caveat: The messages below"):
        return False
    if stripped.startswith("Base directory for this skill"):
        return False
    return True


def _stringify_tool_result(content: Any) -> str:
    """Flatten a tool_result block's content into a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "content" in block:
                    parts.append(str(block.get("content", "")))
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content) if content else ""


def _extract_compaction_summary(text: str) -> str:
    """Pull the summary body out of a post-compaction continuation message."""
    marker = COMPACTION_SUMMARY_MARKER
    idx = text.find(marker)
    if idx != -1:
        return text[idx:]
    return text


def build_session_records(
    session_id: str,
    spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct a live session's spans into markdown_renderer JSONL records.

    Args:
        session_id: The Claude session UUID (used as chain/session id in output).
        spans: Live `litellm_request` span dicts. Each must carry
            ``raw_attributes_json`` (``{"attributes": <phoenix attributes>}``),
            ``start_time``, and optional token-count fields.

    Returns:
        A list of records (one header, then event records) suitable for
        ``dev_agent_lens.export.markdown_renderer.render_jsonl_to_markdown``.
    """
    # Sort by start_time; fall back to datetime.min (not "") so a span with a
    # missing/unparseable start_time can't trigger a str-vs-datetime TypeError.
    ordered = sorted(
        spans, key=lambda s: _parse_timestamp(s.get("start_time")) or datetime.min
    )

    events: list[dict[str, Any]] = []
    # tool_use_id -> the emitted tool event, so a later tool_result can fill it in
    pending_tools: dict[str, dict[str, Any]] = {}

    total_tokens = 0
    models_used: dict[str, int] = {}
    compaction_count = 0

    timestamps: list[Any] = []
    for span in ordered:
        ts = _parse_timestamp(span.get("start_time"))
        if ts:
            timestamps.append(ts)
        ts_end = _parse_timestamp(span.get("end_time"))
        if ts_end:
            timestamps.append(ts_end)

        # Claude Code routes ancillary background work (quota checks, topic
        # detection, conversation-title generation, bash-safety checks) to
        # Haiku. Those are not part of the main conversation, so skip them for
        # parity with `dal reconstruct`'s main-thread-only default.
        model = _extract_model(span)
        if "haiku" in model.lower():
            continue

        if model:
            models_used[model] = models_used.get(model, 0) + 1

        total_tokens += int(span.get("llm_token_count_prompt") or 0)
        total_tokens += int(span.get("llm_token_count_completion") or 0)

        # The continuation/task markers can live in either message shape: the
        # structured `llm.input_messages` OR the serialized `input.value`
        # request string (which is what the parquet `export_chain_to_jsonl`
        # pipeline checks). Scan input.value once so compaction is detected
        # regardless of which shape carries the marker.
        span_input_value = _extract_input_value(span)

        # A compaction-summary *call* (input = the summarization prompt, output
        # = the summary) is not conversation — skip the whole span so its giant
        # prompt/summary isn't dumped as user/assistant turns.
        if COMPACTION_TASK_MARKER in span_input_value:
            continue

        span_emitted_compaction = False

        # ---- User side: input_messages (one user message per exchange) --------
        for msg in _extract_input_messages_array(span):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            content_str = content if isinstance(content, str) else str(content)

            # Compaction: the post-compaction continuation carries the summary
            if COMPACTION_CONTINUATION_MARKER in content_str:
                compaction_count += 1
                span_emitted_compaction = True
                events.append({
                    "record_type": "event",
                    "event_type": "compaction",
                    "number": compaction_count,
                    "summary": _extract_compaction_summary(content_str),
                })
                continue
            # The summarization request itself is not a human turn — skip its
            # (huge) prompt rather than dumping it as user text.
            if COMPACTION_TASK_MARKER in content_str:
                continue

            for block in _parse_message_content(content_str):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if _is_human_turn(text):
                        events.append({
                            "record_type": "event",
                            "event_type": "user",
                            "text": text.strip(),
                        })
                elif btype == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    result_str = _stringify_tool_result(block.get("content", ""))
                    tool_event = pending_tools.pop(tool_use_id, None)
                    if tool_event is not None:
                        # Fill the result into the matching tool_use call.
                        if not tool_event.get("result"):
                            tool_event["result"] = result_str
                    else:
                        # Live litellm spans don't record the assistant's
                        # tool_use blocks (only text), so the call itself is
                        # unknown — but the result is real conversation content.
                        # Emit it standalone so tool results still appear.
                        name = "tool_result (error)" if block.get("is_error") else "tool_result"
                        events.append({
                            "record_type": "event",
                            "event_type": "tool",
                            "name": name,
                            "input": {},
                            "result": result_str,
                        })

        # Fallback: the continuation marker may live only in the serialized
        # `input.value` request (not the structured messages). Emit a compaction
        # event from there so it's caught regardless of message shape.
        if not span_emitted_compaction and (
            COMPACTION_CONTINUATION_MARKER in span_input_value
        ):
            compaction_count += 1
            events.append({
                "record_type": "event",
                "event_type": "compaction",
                "number": compaction_count,
                "summary": _extract_compaction_summary(span_input_value),
            })

        # ---- Assistant side: output_messages (this call's reply) --------------
        for msg in _extract_output_messages_array(span):
            content = msg.get("content", "")
            content_str = content if isinstance(content, str) else str(content)
            for block in _parse_message_content(content_str):
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        events.append({
                            "record_type": "event",
                            "event_type": "assistant",
                            "text": text,
                        })
                elif btype == "tool_use":
                    tool_event = {
                        "record_type": "event",
                        "event_type": "tool",
                        "name": block.get("name", "Unknown"),
                        "input": block.get("input", {}) or {},
                        "result": "",
                    }
                    events.append(tool_event)
                    tool_use_id = block.get("id", "")
                    if tool_use_id:
                        pending_tools[tool_use_id] = tool_event

    start_time = min(timestamps).isoformat() if timestamps else None
    end_time = max(timestamps).isoformat() if timestamps else None

    header = {
        "record_type": "header",
        "chain_id": session_id,
        "claude_session_id": session_id,
        "start_time": start_time,
        "end_time": end_time,
        "total_tokens": total_tokens,
        "metrics": {"models_used": models_used},
        "compaction_count": compaction_count,
    }

    return [header, *events]
