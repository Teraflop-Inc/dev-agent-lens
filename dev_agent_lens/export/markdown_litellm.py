"""
LiteLLM/Phoenix Trace to Markdown Exporter

Converts Phoenix trace data into markdown format matching AGREED_FORMAT.md specification.
This is the LiteLLM pipeline counterpart to markdown.py (Claude Session pipeline).

Design principles:
- Deterministic output (same input always produces identical output)
- Exact string matching with Claude pipeline (after stripping PIPELINE_SPECIFIC)
- PIPELINE_SPECIFIC sections for LiteLLM-only fields (tokens, models, compaction)
- Subagent files named by type and sequence (subagent_{type}_{n}.md)

Known differences from Claude pipeline:
- LiteLLM CAN detect compaction (Claude cannot) - goes in PIPELINE_SPECIFIC
- LiteLLM has ~80% success rate linking subagent traces (20% summary-only fallback)
- LiteLLM has token counts and model info (Claude does not)
"""

from __future__ import annotations

import ast
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from dev_agent_lens.analysis.threads import (
    COMPACTION_CONTINUATION_MARKER,
    COMPACTION_TASK_MARKER,
    classify_span_thread,
    ThreadType,
)


# =============================================================================
# Constants - Exact thresholds from AGREED_FORMAT.md
# =============================================================================

TOOL_RESULT_INLINE_LIMIT = 500  # Show 497 + '...' if exceeded
TOOL_RESULT_FILE_THRESHOLD = 2000  # Create external file if exceeded
SUBAGENT_PROMPT_PREVIEW_LIMIT = 200  # Show 197 + '...' if exceeded
SUBAGENT_RESPONSE_SUMMARY_LIMIT = 500  # Show 497 + '...' if exceeded
TOOL_INPUT_VALUE_LIMIT = 200  # Per-key value limit, show 197 + '...'
PARALLEL_TOOL_TARGET_LIMIT = 60  # For file paths in parallel tools table


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class LiteLLMMarkdownExport:
    """Result of exporting LiteLLM/Phoenix traces to markdown."""

    main_content: str
    """Main session markdown content."""

    subagent_files: dict[str, str] = field(default_factory=dict)
    """Map of subagent filename (without .md) -> markdown content."""

    tool_result_files: dict[str, str] = field(default_factory=dict)
    """Map of tool result filename -> content for large results."""

    session_id: str = ""
    """Session identifier (Claude session UUID from metadata)."""

    total_tokens: int = 0
    """Total tokens used (LiteLLM-specific)."""

    models_used: list[str] = field(default_factory=list)
    """List of unique models used (LiteLLM-specific)."""

    compactions: list[dict] = field(default_factory=list)
    """Detected compaction events (LiteLLM-specific)."""

    stats: dict[str, Any] = field(default_factory=dict)
    """Export statistics (turns, tools, subagents, etc.)."""


@dataclass
class SubagentInfo:
    """Tracking info for a subagent."""

    subagent_type: str
    normalized_type: str
    sequence: int
    filename: str
    task_description: str
    task_prompt: str
    response_summary: str
    tool_use_id: str
    # Subagent trace linkage (may be None if linkage failed)
    trace_id: str | None = None
    spans: list[dict] = field(default_factory=list)
    # Timing info (LiteLLM-specific)
    start_time: datetime | None = None
    end_time: datetime | None = None
    total_tokens: int = 0
    tool_count: int = 0


# =============================================================================
# Helper Functions
# =============================================================================


def _parse_timestamp(ts: Any) -> datetime | None:
    """Parse a timestamp to datetime, handling various formats."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str):
        return None
    try:
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(ts[:26], fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _safe_str(value: Any) -> str:
    """Convert value to string, handling NaN and None gracefully."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value) if value else ""


def truncate(text: str, limit: int) -> str:
    """Truncate text to limit chars, showing (limit-3) + '...' if exceeded."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def normalize_subagent_type(subagent_type: str) -> str:
    """Normalize subagent type for filename: lowercase, replace - and spaces with _."""
    return subagent_type.lower().replace("-", "_").replace(" ", "_")


def get_language_hint(file_path: str) -> str:
    """Get language hint for code block based on file extension."""
    ext_map = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".json": "json",
        ".md": "markdown",
        ".sh": "bash",
        ".bash": "bash",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".html": "html",
        ".css": "css",
        ".sql": "sql",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".rb": "ruby",
        ".php": "php",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
    }
    ext = Path(file_path).suffix.lower()
    return ext_map.get(ext, "text")


def format_timestamp(dt: datetime | None) -> str:
    """Format datetime as 'YYYY-MM-DD HH:MM:SS UTC'."""
    if dt is None:
        return ""
    # Ensure UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_tool_input(tool_input: dict) -> str:
    """Format tool input as key: value pairs, alphabetically ordered."""
    if not tool_input:
        return ""

    lines = []
    for key in sorted(tool_input.keys()):
        value = tool_input[key]
        # Convert non-string values to string representation
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value)
        else:
            value_str = str(value)
        # Truncate long values
        value_str = truncate(value_str, TOOL_INPUT_VALUE_LIMIT)
        lines.append(f"{key}: {value_str}")

    return "\n".join(lines)


def get_tool_target_brief(tool_name: str, tool_input: dict) -> str:
    """Get brief target description for parallel tools table."""
    if tool_name == "Read":
        path = tool_input.get("file_path", "")
        if len(path) > PARALLEL_TOOL_TARGET_LIMIT:
            return "..." + path[-(PARALLEL_TOOL_TARGET_LIMIT - 3) :]
        return path

    elif tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        if len(path) > PARALLEL_TOOL_TARGET_LIMIT:
            return "..." + path[-(PARALLEL_TOOL_TARGET_LIMIT - 3) :]
        return path

    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return truncate(cmd, 50)

    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", ".")
        return f"`{pattern}` in `{path}`"

    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", ".")
        return f"`{pattern}` in `{path}`"

    elif tool_name == "Task":
        subagent_type = tool_input.get("subagent_type", "")
        desc = tool_input.get("description", "")
        return f"{subagent_type}: {truncate(desc, 30)}"

    else:
        # First value truncated
        for key in sorted(tool_input.keys()):
            value = str(tool_input[key])
            return truncate(value, 50)
        return ""


# =============================================================================
# Phoenix Trace Data Extraction
# =============================================================================


def _extract_input_value(span: dict[str, Any]) -> str:
    """Extract input value from span, checking multiple locations."""
    # First try direct input_value field
    input_val = _safe_str(span.get("input_value"))
    if input_val:
        return input_val

    # Try raw_attributes_json
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if raw_attrs:
        try:
            if isinstance(raw_attrs, str):
                attrs = json.loads(raw_attrs)
            else:
                attrs = raw_attrs
            return _safe_str(attrs.get("attributes", {}).get("input", {}).get("value", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return ""


def _extract_output_value(span: dict[str, Any]) -> str:
    """Extract output value from span."""
    # First try direct output_value field
    output_val = _safe_str(span.get("output_value"))
    if output_val:
        return output_val

    # Try raw_attributes_json
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if raw_attrs:
        try:
            if isinstance(raw_attrs, str):
                attrs = json.loads(raw_attrs)
            else:
                attrs = raw_attrs
            return _safe_str(attrs.get("attributes", {}).get("output", {}).get("value", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return ""


def _extract_input_messages_array(span: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract the llm.input_messages array from a span.

    This array contains the full conversation history with proper ordering.
    Each message has a 'role' (user/assistant/system) and 'content' field.

    The key insight is that this array is the SOURCE OF TRUTH for message ordering,
    as it represents the cumulative conversation history at that point.

    Returns:
        List of message dicts with 'role' and 'content' keys, or empty list.
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if not raw_attrs:
        return []

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Try multiple locations for input_messages:
        # 1. Top-level llm.input_messages (some Phoenix versions)
        # 2. Nested at attributes.llm.input_messages (litellm_request spans)
        input_messages = attrs.get("llm.input_messages")

        if not input_messages:
            # Try nested path: attributes -> llm -> input_messages
            inner_attrs = attrs.get("attributes", {})
            llm_attrs = inner_attrs.get("llm", {})
            input_messages = llm_attrs.get("input_messages")

        if not input_messages:
            return []

        # Parse if it's a string
        if isinstance(input_messages, str):
            try:
                input_messages = json.loads(input_messages)
            except json.JSONDecodeError:
                try:
                    input_messages = ast.literal_eval(input_messages)
                except (ValueError, SyntaxError):
                    return []

        if isinstance(input_messages, list):
            # Handle the nested message format: [{"message": {"role": ..., "content": ...}}]
            result = []
            for item in input_messages:
                if isinstance(item, dict):
                    # Check for nested "message" key
                    if "message" in item:
                        msg = item["message"]
                        if isinstance(msg, dict) and "role" in msg:
                            result.append(msg)
                    # Or direct role/content format
                    elif "role" in item:
                        result.append(item)
            return result

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return []


def _extract_cumulative_messages_from_raw_span(span: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract the cumulative conversation messages from a raw_gen_ai_request span.

    The correct path is: attributes -> llm -> None -> messages

    This is the SOURCE OF TRUTH for message ordering - it contains the full
    conversation history in correct chronological order at the time of the LLM call.

    Args:
        span: A raw_gen_ai_request span dict.

    Returns:
        List of message dicts with 'role' and 'content' keys, or empty list.
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if not raw_attrs:
        return []

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Navigate: attributes -> llm -> None -> messages
        inner = attrs.get("attributes", {})
        llm = inner.get("llm", {})
        # The key is literally the string "None" (not Python None)
        none_key = llm.get("None", llm.get(None, {}))

        messages_str = none_key.get("messages", "")

        if not messages_str:
            return []

        # Parse the messages string (it's a JSON/Python literal string)
        try:
            messages = json.loads(messages_str)
        except json.JSONDecodeError:
            try:
                messages = ast.literal_eval(messages_str)
            except (ValueError, SyntaxError):
                return []

        return messages if isinstance(messages, list) else []

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return []


def _get_message_content_key(msg: dict[str, Any]) -> str:
    """
    Generate a unique key for a message based on role and content.

    Used for deduplication when diffing successive spans.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")

    if isinstance(content, list):
        # Get first text content for hashing
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    content = block.get("text", "")[:200]
                    break
                elif block.get("type") == "tool_result":
                    # Include tool_use_id for uniqueness
                    tool_id = block.get("tool_use_id", "")
                    content = f"tool_result:{tool_id}"
                    break
        else:
            content = str(content)[:200]
    elif isinstance(content, str):
        content = content[:200]
    else:
        content = str(content)[:200]

    return f"{role}:{hash(content)}"


def _get_message_text(msg: dict[str, Any]) -> str:
    """Extract text content from a message dict."""
    content = msg.get("content", "")

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    # Include tool result content
                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        texts.append(f"[tool_result: {result_content[:100]}]")
        return " ".join(texts)
    elif isinstance(content, str):
        return content

    return str(content)


def _extract_ordered_messages_from_spans(
    all_spans: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """
    Extract messages in correct chronological order by diffing successive
    raw_gen_ai_request spans.

    This is the SOURCE OF TRUTH approach - each raw_gen_ai_request span contains
    the cumulative conversation history in correct order. By finding main thread
    spans (those with increasing message counts) and diffing them, we get messages
    in the order they actually occurred.

    Args:
        all_spans: List of all spans for a session.

    Returns:
        List of (role, text) tuples in correct chronological order.
    """
    # Filter to raw_gen_ai_request spans only
    raw_spans = [s for s in all_spans if s.get("name") == "raw_gen_ai_request"]

    if not raw_spans:
        return []

    # Sort by start_time
    raw_spans.sort(key=lambda s: s.get("start_time", ""))

    # Find main thread spans (those with increasing cumulative message counts)
    # Main thread spans:
    # - Have 2+ messages (user/assistant pairs)
    # - Message count increases (cumulative history) OR represents a new turn after compaction
    # - Start with 'user' role
    #
    # IMPORTANT: After a compaction, message count may RESET to a small number.
    # We detect this by looking for spans where count drops significantly but
    # still has 2+ messages starting with 'user'. These represent new conversation
    # turns that need to be included.
    main_thread_spans = []
    prev_count = 0
    prev_time = None

    for span in raw_spans:
        messages = _extract_cumulative_messages_from_raw_span(span)
        count = len(messages)
        span_time = span.get("start_time")

        if count >= 2:
            # Verify starts with user role
            if messages and messages[0].get("role") == "user":
                # Include if:
                # 1. Count is increasing (normal case)
                # 2. OR count dropped significantly (compaction/new turn) but we have valid messages
                #
                # Compaction reset detection:
                # After a real compaction, the context typically includes:
                # - Compaction summary (system/assistant message)
                # - At least one new user turn + assistant response
                # So we require at least 4 messages for a valid reset.
                #
                # Spans with only 2 messages after a drop are likely subagent calls,
                # not main thread compaction resets.
                is_increasing = count > prev_count
                is_compaction_reset = (
                    prev_count > 0 and
                    count < prev_count / 2 and
                    count >= 4  # Require 4+ messages to distinguish from subagent calls
                )

                if is_increasing or is_compaction_reset:
                    main_thread_spans.append((span, messages))
                    prev_count = count
                    prev_time = span_time

    if not main_thread_spans:
        return []

    # Diff successive spans to get ordered messages
    # IMPORTANT: Filter out system/internal messages to match what we export
    # This ensures the ordering index aligns with exported user/assistant messages
    ordered_messages: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for span, messages in main_thread_spans:
        for msg in messages:
            key = _get_message_content_key(msg)
            if key not in seen_keys:
                seen_keys.add(key)
                role = msg.get("role", "unknown")
                text = _get_message_text(msg)
                if not text.strip():
                    continue

                # Skip system/internal messages that we don't export
                # These pollute the ordering index and cause misalignment
                if role == "user":
                    # Skip tool results (internal system messages)
                    if text.startswith("[tool_result:"):
                        continue
                    # Skip system reminders
                    if text.startswith("<system-reminder>") or text.startswith("<system"):
                        continue
                    # Skip subagent prompts (not actual user turns)
                    if text.startswith("Explore the ~/") or text.startswith("Search for and read"):
                        continue
                    # Skip command/JSON outputs
                    if text.startswith("{") and text.endswith("}"):
                        continue
                    if text.startswith("Command:") and "\nOutput:" in text:
                        continue

                ordered_messages.append((role, text))

    return ordered_messages


def _extract_output_messages_array(span: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract the llm.output_messages array from a span.

    This array contains the assistant's response for this turn.

    Returns:
        List of message dicts with 'role' and 'content' keys, or empty list.
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if not raw_attrs:
        return []

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Try multiple locations for output_messages:
        # 1. Top-level llm.output_messages (some Phoenix versions)
        # 2. Nested at attributes.llm.output_messages (litellm_request spans)
        output_messages = attrs.get("llm.output_messages")

        if not output_messages:
            # Try nested path: attributes -> llm -> output_messages
            inner_attrs = attrs.get("attributes", {})
            llm_attrs = inner_attrs.get("llm", {})
            output_messages = llm_attrs.get("output_messages")

        if not output_messages:
            return []

        # Parse if it's a string
        if isinstance(output_messages, str):
            try:
                output_messages = json.loads(output_messages)
            except json.JSONDecodeError:
                try:
                    output_messages = ast.literal_eval(output_messages)
                except (ValueError, SyntaxError):
                    return []

        if isinstance(output_messages, list):
            # Handle the nested message format: [{"message": {"role": ..., "content": ...}}]
            result = []
            for item in output_messages:
                if isinstance(item, dict):
                    # Check for nested "message" key
                    if "message" in item:
                        msg = item["message"]
                        if isinstance(msg, dict) and "role" in msg:
                            result.append(msg)
                    # Or direct role/content format
                    elif "role" in item:
                        result.append(item)
            return result

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return []


def _extract_model(span: dict[str, Any]) -> str:
    """Extract model name from span."""
    model = span.get("llm_model_name")
    if model:
        return _safe_str(model)

    # Try raw_attributes_json
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if raw_attrs:
        try:
            if isinstance(raw_attrs, str):
                attrs = json.loads(raw_attrs)
            else:
                attrs = raw_attrs
            # Try attributes.llm.model_name
            model = attrs.get("attributes", {}).get("llm", {}).get("model_name")
            if model:
                return _safe_str(model)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return ""


def _parse_message_content(content: str) -> list[dict[str, Any]]:
    """
    Parse message content which may be JSON array of message blocks.

    Content may be stored as JSON (double quotes) or Python literals (single quotes).
    Returns list of message dictionaries with 'type' and content keys.
    """
    if not content:
        return []

    parsed = None

    # Try to parse as JSON array first
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        pass

    # If JSON fails, try Python literal (handles single quotes)
    if parsed is None:
        try:
            parsed = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            pass

    # Process the parsed content if successful
    if isinstance(parsed, list):
        messages = []
        for item in parsed:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    messages.append({"type": "text", "text": item.get("text", "")})
                elif item.get("type") == "tool_use":
                    messages.append({
                        "type": "tool_use",
                        "name": item.get("name", "unknown"),
                        "input": item.get("input", {}),
                        "id": item.get("id", ""),
                    })
                elif item.get("type") == "tool_result":
                    messages.append({
                        "type": "tool_result",
                        "tool_use_id": item.get("tool_use_id", ""),
                        "content": item.get("content", ""),
                    })
        return messages

    # Return as plain text if parsing failed
    if content.strip():
        return [{"type": "text", "text": content}]
    return []


def _is_main_thread_span(span: dict[str, Any]) -> bool:
    """Check if span is a main thread conversation span."""
    name = span.get("name", "")
    # Main thread spans start with Claude_Code_Internal_Prompt_
    return name.startswith("Claude_Code_Internal_Prompt_")


def _is_subagent_span(span: dict[str, Any]) -> bool:
    """Check if span belongs to a subagent execution."""
    name = span.get("name", "")
    # Subagent LLM calls have raw_gen_ai_request with subagent-specific system prompts
    if name == "raw_gen_ai_request":
        input_val = _extract_input_value(span)
        # Subagent system prompts contain specific markers
        return "subagent_type" in input_val or "You are a specialized" in input_val
    return False


def has_compaction_marker(span: dict[str, Any]) -> bool:
    """Check if a span has a compaction continuation marker."""
    input_val = _extract_input_value(span)
    return COMPACTION_CONTINUATION_MARKER in input_val


def is_compaction_task(span: dict[str, Any]) -> bool:
    """Check if a span is a compaction task (generating summary)."""
    input_val = _extract_input_value(span)
    return COMPACTION_TASK_MARKER in input_val


def extract_claude_session_id(span: dict[str, Any]) -> str | None:
    """
    Extract Claude session UUID from span metadata.

    Returns 36-char UUID or None if not found.
    """
    import re

    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if not raw_attrs:
        return None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Check multiple locations for metadata
        def extract_from_user_id(user_id: str) -> str | None:
            if not user_id:
                return None
            match = re.search(r"session_([a-f0-9\-]{36})", user_id)
            return match.group(1) if match else None

        # Path 1: Dotted key format (lambda2)
        dotted_metadata = attrs.get("attributes.metadata")
        if dotted_metadata:
            try:
                if isinstance(dotted_metadata, str):
                    metadata = json.loads(dotted_metadata)
                else:
                    metadata = dotted_metadata
                req_meta = metadata.get("requester_metadata", {})
                if isinstance(req_meta, dict):
                    user_id = req_meta.get("user_id", "")
                    result = extract_from_user_id(user_id)
                    if result:
                        return result
            except (json.JSONDecodeError, TypeError):
                pass

        # Path 2: Nested dict format (local-alex)
        attributes = attrs.get("attributes", {})
        if isinstance(attributes, dict):
            metadata = attributes.get("metadata", {})
            if isinstance(metadata, dict):
                req_meta = metadata.get("requester_metadata", {})
                if isinstance(req_meta, dict):
                    user_id = req_meta.get("user_id", "")
                    result = extract_from_user_id(user_id)
                    if result:
                        return result

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return None


# =============================================================================
# Main Export Function
# =============================================================================


def _extract_task_span_info(span: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract task (subagent) info from a Claude_Code_Tool_Task span.

    Returns dict with: tool_use_id, subagent_type, description, prompt, response
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        return None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        attributes = attrs.get("attributes", {})

        # Get input
        input_data = attributes.get("input", {}).get("value", "")
        if isinstance(input_data, str):
            input_data = json.loads(input_data) if input_data else {}

        if not input_data or input_data.get("type") != "tool_use":
            return None

        tool_use_id = input_data.get("id", "")
        tool_input = input_data.get("input", {})

        # Get output
        output_data = attributes.get("output", {}).get("value", "")
        if isinstance(output_data, str):
            # Try to parse as Python literal (Phoenix uses single quotes)
            try:
                import ast
                output_data = ast.literal_eval(output_data)
            except (ValueError, SyntaxError):
                try:
                    output_data = json.loads(output_data)
                except json.JSONDecodeError:
                    output_data = []

        # Extract response text
        response_text = ""
        if isinstance(output_data, list):
            for item in output_data:
                if isinstance(item, dict) and item.get("type") == "text":
                    response_text = item.get("text", "")
                    break
        elif isinstance(output_data, str):
            response_text = output_data

        return {
            "tool_use_id": tool_use_id,
            "subagent_type": tool_input.get("subagent_type", "unknown"),
            "description": tool_input.get("description", ""),
            "prompt": tool_input.get("prompt", ""),
            "response": response_text,
        }
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return None


def _extract_tool_span_info(span: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract tool info from a Claude_Code_Tool_* span (non-Task).

    Returns dict with: tool_use_id, name, input, result
    """
    name = span.get("name", "")
    if not name.startswith("Claude_Code_Tool_") or name == "Claude_Code_Tool_Task":
        return None

    # Extract tool name from span name
    tool_name = name.replace("Claude_Code_Tool_", "")

    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        return None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        attributes = attrs.get("attributes", {})

        # Get input
        input_data = attributes.get("input", {}).get("value", "")
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data) if input_data else {}
            except json.JSONDecodeError:
                input_data = {}

        tool_use_id = ""
        tool_input = {}

        if isinstance(input_data, dict):
            if input_data.get("type") == "tool_use":
                tool_use_id = input_data.get("id", "")
                tool_input = input_data.get("input", {})
            else:
                tool_input = input_data

        # Get output
        output_data = attributes.get("output", {}).get("value", "")
        result = ""
        if isinstance(output_data, str):
            result = output_data
        elif isinstance(output_data, (dict, list)):
            result = str(output_data)

        return {
            "tool_use_id": tool_use_id,
            "name": tool_name,
            "input": tool_input,
            "result": result,
        }
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return None


def export_chain_to_unified_markdown(
    chain: "ConversationChain",
    sessions: list[dict[str, Any]],
) -> LiteLLMMarkdownExport:
    """
    Export a conversation chain to markdown matching AGREED_FORMAT.md.

    This function produces output identical to the Claude Session pipeline
    (after stripping PIPELINE_SPECIFIC sections).

    Args:
        chain: The ConversationChain to export.
        sessions: List of all session dictionaries with spans.

    Returns:
        LiteLLMMarkdownExport with main content, subagent files, and tool result files.
    """
    # Import here to avoid circular dependency
    from dev_agent_lens.analysis.chains import ConversationChain

    session_lookup = {s.get("session_id", ""): s for s in sessions}

    # Extract Claude session UUID (our canonical ID for matching Claude pipeline)
    claude_session_id = chain.claude_session_id
    if not claude_session_id:
        # Try to extract from spans
        for session_id in chain.session_ids:
            session = session_lookup.get(session_id, {})
            for span in session.get("spans", []):
                claude_session_id = extract_claude_session_id(span)
                if claude_session_id:
                    break
            if claude_session_id:
                break

    if not claude_session_id:
        # Fallback to chain ID
        claude_session_id = chain.chain_id

    # Stats tracking
    stats = {
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "subagents": 0,
    }

    # LiteLLM-specific tracking
    total_tokens = 0
    models_used: set[str] = set()
    compactions: list[dict] = []
    seen_compaction_hashes: set[int] = set()  # For deduplicating compactions

    # Subagent tracking
    subagent_type_counts: dict[str, int] = {}
    subagent_infos: list[SubagentInfo] = []

    # Tool result file tracking
    tool_result_files: dict[str, str] = {}
    tool_result_sequence = 0

    # Collect conversation events: tuples of (timestamp, event_type, data)
    # event_type is one of: "user", "assistant", "tool", "subagent"
    conversation_events: list[tuple[datetime, str, dict[str, Any]]] = []

    # Track which tool_use_ids are Task (subagent) calls - deduplicate by tool_use_id
    task_tool_ids: set[str] = set()
    seen_tool_use_ids: set[str] = set()

    # CRITICAL: Identify main thread trace_ids
    # Main thread traces are those that contain Claude_Code_Tool_Task spans.
    # Tool spans from other traces (subagent internal traces) should NOT be included
    # in the main conversation file - they belong in subagent files only.
    main_thread_trace_ids: set[str] = set()

    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            if span.get("name") == "Claude_Code_Tool_Task":
                trace_id = span.get("trace_id")
                if trace_id:
                    main_thread_trace_ids.add(trace_id)

    # Collect Task spans first to identify subagent tool_use_ids (deduplicated)
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            if span.get("name") == "Claude_Code_Tool_Task":
                task_info = _extract_task_span_info(span)
                if task_info:
                    tool_use_id = task_info["tool_use_id"]
                    # Skip if we've already seen this tool_use_id
                    if tool_use_id in seen_tool_use_ids:
                        continue
                    seen_tool_use_ids.add(tool_use_id)
                    task_tool_ids.add(tool_use_id)
                    ts = _parse_timestamp(span.get("start_time"))
                    if ts:
                        conversation_events.append((ts, "subagent", task_info))

    # Collect tool spans (non-Task) - ONLY from main thread traces
    # Subagent internal tool calls are in separate traces and should go in subagent files
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            # CRITICAL: Only include tools from main thread traces
            trace_id = span.get("trace_id")
            if trace_id and trace_id not in main_thread_trace_ids:
                continue

            name = span.get("name", "")
            if name.startswith("Claude_Code_Tool_") and name != "Claude_Code_Tool_Task":
                tool_info = _extract_tool_span_info(span)
                if tool_info:
                    tool_use_id = tool_info.get("tool_use_id", "")
                    # Skip if we've already seen this tool_use_id (if present)
                    if tool_use_id and tool_use_id in seen_tool_use_ids:
                        continue
                    if tool_use_id:
                        seen_tool_use_ids.add(tool_use_id)
                    ts = _parse_timestamp(span.get("start_time"))
                    if ts:
                        conversation_events.append((ts, "tool", tool_info))

    # Get timestamps from ALL spans to capture the broadest session time range
    # Note: Phoenix may not capture the full session duration that Claude JSONL has
    # This is a known limitation documented in PIPELINE_COMPARISON.md
    all_timestamps: list[datetime] = []
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            ts = _parse_timestamp(span.get("start_time"))
            if ts:
                all_timestamps.append(ts)
            ts = _parse_timestamp(span.get("end_time"))
            if ts:
                all_timestamps.append(ts)

    start_time = min(all_timestamps) if all_timestamps else None
    end_time = max(all_timestamps) if all_timestamps else None

    # ==========================================================================
    # Build ordering index from input arrays for correct chronological order
    # ==========================================================================
    # The input arrays in raw_gen_ai_request spans contain cumulative conversation
    # history in correct order. We use this to build an ordering index, then apply
    # it to messages extracted from all span types (which captures more content).
    #
    # Strategy:
    # 1. Extract ordered messages from input arrays to build ordering index
    # 2. Extract ALL messages from individual spans (original approach - more complete)
    # 3. Use ordering index to assign correct order to extracted messages
    # 4. Fall back to timestamp ordering for messages not in the index
    #
    # IMPORTANT: The ordering index maps content_hash -> (order_index, role)
    # We use this to create synthetic timestamps that place ordered messages
    # at the START of the conversation, ensuring they sort before any
    # timestamp-based messages. This is because the first user messages
    # typically appear in the FIRST LLM call's input array, not as "new"
    # messages detected by diffing.

    # Collect all spans from all sessions
    all_spans: list[dict[str, Any]] = []
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        all_spans.extend(session.get("spans", []))

    # Build ordering index from input arrays
    # Map: content_hash -> (order_index, role)
    # We track role to help with debugging/verification
    ordering_index: dict[int, tuple[int, str]] = {}
    ordered_messages = _extract_ordered_messages_from_spans(all_spans)
    max_order_idx = len(ordered_messages)
    for idx, (role, text) in enumerate(ordered_messages):
        # Create hash of content for matching
        text_clean = text.strip()
        if text_clean:
            # Use first 100 chars for matching (same as deduplication)
            content_hash = hash(text_clean[:100])
            if content_hash not in ordering_index:
                ordering_index[content_hash] = (idx, role)

    # Collect main thread conversation spans for user/assistant messages
    # ONLY from main thread traces (fixes Bug 3: user turn count mismatch)
    seen_user_messages: set[str] = set()
    seen_assistant_messages: set[str] = set()

    # Build mapping: tool_use_id -> order_idx of spawning assistant message
    # This allows us to correctly order tools after the assistant message that invoked them
    tool_use_id_to_order_idx: dict[str, int] = {}

    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            # CRITICAL: Only include spans from main thread traces
            trace_id = span.get("trace_id")
            if trace_id and trace_id not in main_thread_trace_ids:
                continue

            if not _is_main_thread_span(span):
                continue

            ts = _parse_timestamp(span.get("start_time"))
            if not ts:
                continue

            input_val = _extract_input_value(span)
            output_val = _extract_output_value(span)

            # Track tokens and models (LiteLLM-specific)
            tokens_prompt = span.get("llm_token_count_prompt") or 0
            tokens_completion = span.get("llm_token_count_completion") or 0
            total_tokens += tokens_prompt + tokens_completion

            model = _extract_model(span)
            if model:
                models_used.add(model)

            # Check for compaction continuation (LiteLLM detects this, Claude cannot)
            # Add compactions as conversation events at their chronological position
            if COMPACTION_CONTINUATION_MARKER in input_val:
                if "The conversation is summarized below:" in input_val:
                    summary_start = input_val.find("The conversation is summarized below:")
                    summary_text = input_val[summary_start:]
                    # Deduplicate compactions by summary hash (first 500 chars)
                    summary_hash = hash(summary_text[:500])
                    if summary_hash not in seen_compaction_hashes:
                        seen_compaction_hashes.add(summary_hash)
                        compaction_number = len(compactions) + 1
                        compactions.append({
                            "number": compaction_number,
                            "summary": summary_text,
                            "timestamp": ts,  # Store timestamp for inline placement
                        })
                        # Add compaction as a conversation event at its chronological position
                        conversation_events.append((ts, "compaction", {
                            "number": compaction_number,
                            "summary": summary_text,
                        }))
                continue

            # Skip compaction task spans
            if COMPACTION_TASK_MARKER in input_val:
                continue

            # Parse messages
            input_messages = _parse_message_content(input_val)
            output_messages = _parse_message_content(output_val)

            # For ordering: use span start_time for input (user) messages,
            # and span end_time for output (assistant) messages.
            # This ensures that within a span, user input comes before assistant output.
            input_ts = ts
            output_ts = _parse_timestamp(span.get("end_time")) or ts

            # Extract user messages from input
            for msg in input_messages:
                if msg["type"] == "text":
                    text = msg.get("text", "").strip()
                    if text and len(text) > 10:
                        # Skip system reminders
                        if text.startswith("<system"):
                            continue
                        # Skip tool result echoes
                        if text.startswith("Command:") and "\nOutput:" in text:
                            continue
                        # Skip JSON tool results
                        if text.startswith("{") and text.endswith("}"):
                            continue
                        # Skip internal Claude Code context messages
                        if text.startswith("Files modified by"):
                            continue
                        if text.startswith("Explore the ~/") or text.startswith("Search for and read"):
                            continue
                        # Skip if we've seen this message
                        text_hash = hash(text[:100])
                        if text_hash in seen_user_messages:
                            continue
                        seen_user_messages.add(text_hash)

                        # Check ordering index for correct position
                        order_info = ordering_index.get(text_hash)
                        if order_info is not None:
                            order_idx, _ = order_info
                            # Use ordering index - store for later sorting
                            conversation_events.append((input_ts, "user", {"text": text, "order_idx": order_idx}))
                        else:
                            # Fall back to span timestamp - no order_idx
                            conversation_events.append((input_ts, "user", {"text": text}))

            # Extract assistant messages from output
            # Also capture tool_use_ids so we can link tools to their spawning assistant
            assistant_order_idx_for_this_span: int | None = None
            for msg in output_messages:
                if msg["type"] == "text":
                    text = msg.get("text", "").strip()
                    if text:
                        # Skip partial/incomplete messages
                        if len(text) < 5:
                            continue
                        # Skip if we've seen this message
                        text_hash = hash(text[:100])
                        if text_hash in seen_assistant_messages:
                            continue
                        seen_assistant_messages.add(text_hash)

                        # Check ordering index for correct position
                        order_info = ordering_index.get(text_hash)
                        if order_info is not None:
                            order_idx, _ = order_info
                            assistant_order_idx_for_this_span = order_idx
                            # Use ordering index - store for later sorting
                            conversation_events.append((output_ts, "assistant", {"text": text, "order_idx": order_idx}))
                        else:
                            # Fall back to span timestamp - no order_idx
                            conversation_events.append((output_ts, "assistant", {"text": text}))

            # Map tool_use_ids from this assistant output to the assistant's order_idx
            # This allows tools to sort correctly after the assistant that spawned them
            if assistant_order_idx_for_this_span is not None:
                for msg in output_messages:
                    if msg["type"] == "tool_use":
                        tool_use_id = msg.get("id", "")
                        if tool_use_id:
                            tool_use_id_to_order_idx[tool_use_id] = assistant_order_idx_for_this_span

    # Sort events to match Claude's export order.
    #
    # Claude JSONL exports follow a specific pattern:
    # User → Assistant → Tool(s) → User → ...
    #
    # The Assistant message contains the decision to call tools, so tools should
    # appear AFTER the assistant message that invoked them.
    #
    # Strategy:
    # 1. Messages with order_idx (from input array) define the backbone order
    # 2. Tools/subagents are linked to their spawning assistant via tool_use_id
    # 3. We use the tool_use_id_to_order_idx mapping to assign order_idx to tools
    #
    # The key insight: tool_use_id explicitly links each tool to the assistant
    # message that spawned it. We captured this mapping when processing assistant
    # output messages above.

    # Assign effective order_idx to tools and subagents using tool_use_id linking
    for i, (ts, event_type, data) in enumerate(conversation_events):
        if event_type in ("tool", "subagent") and "order_idx" not in data:
            tool_use_id = data.get("tool_use_id", "")
            if tool_use_id and tool_use_id in tool_use_id_to_order_idx:
                # Use the order_idx of the spawning assistant message
                # Add 0.5 to sort after the assistant but before the next user
                spawning_assistant_idx = tool_use_id_to_order_idx[tool_use_id]
                data["order_idx"] = spawning_assistant_idx + 0.5
            else:
                # Fallback: Find the last assistant message before this tool's timestamp
                best_idx = 0
                for other_ts, other_type, other_data in conversation_events:
                    if other_type == "assistant" and "order_idx" in other_data:
                        if other_ts and other_ts <= ts:
                            best_idx = max(best_idx, other_data["order_idx"])
                data["order_idx"] = best_idx + 0.5

    # Event type order within the same order_idx:
    # - user: 0 (comes first)
    # - assistant: 1 (after user, before tools)
    # - tool: 2 (after assistant)
    # - subagent: 3 (after regular tools)
    # - compaction: 4 (after everything else in the group)
    EVENT_TYPE_ORDER = {
        "user": 0,
        "assistant": 1,
        "tool": 2,
        "subagent": 3,
        "compaction": 4,
    }

    # Assign order_idx to events that don't have one based on timestamp position
    # relative to events that DO have order_idx
    #
    # Strategy: Events without order_idx get interpolated between events that have them
    # based on timestamp. This ensures they appear at the right chronological position.
    events_with_idx = [(ts, t, d) for ts, t, d in conversation_events if "order_idx" in d]
    events_without_idx = [(ts, t, d) for ts, t, d in conversation_events if "order_idx" not in d]

    if events_with_idx and events_without_idx:
        # Sort events with order_idx to find the timeline
        events_with_idx.sort(key=lambda e: e[2].get("order_idx", 0))

        # For each event without order_idx, find where it belongs based on timestamp
        for ts, event_type, data in events_without_idx:
            if ts is None:
                # No timestamp, put at the end
                data["order_idx"] = max_order_idx + 1
                continue

            ts_value = ts.timestamp()

            # Find the best position based on timestamp
            best_idx = 0
            for other_ts, other_type, other_data in events_with_idx:
                other_idx = other_data.get("order_idx", 0)
                if other_ts and other_ts.timestamp() <= ts_value:
                    best_idx = max(best_idx, other_idx)

            # Place this event after the last event that happened before it
            # Use a fractional index to sort after that event but before the next
            data["order_idx"] = best_idx + 0.1

    def sort_key(event):
        ts, event_type, data = event
        # Primary: order_idx (the logical conversation position)
        # Events without order_idx now have interpolated values
        order_idx = data.get("order_idx", max_order_idx + 1)

        # Secondary: type order within the same order_idx
        type_order = EVENT_TYPE_ORDER.get(event_type, 5)

        # Tertiary: actual timestamp for stable ordering of same-type events
        ts_value = ts.timestamp() if ts else 0

        return (order_idx, type_order, ts_value)

    conversation_events.sort(key=sort_key)

    # Process conversation events into markdown
    conversation_lines: list[str] = []

    for ts, event_type, data in conversation_events:
        if event_type == "user":
            conversation_lines.append("### User")
            conversation_lines.append("")
            conversation_lines.append(data["text"])
            conversation_lines.append("")
            conversation_lines.append("---")
            conversation_lines.append("")
            stats["user_turns"] += 1

        elif event_type == "assistant":
            conversation_lines.append("### Assistant")
            conversation_lines.append("")
            conversation_lines.append(data["text"])
            conversation_lines.append("")
            conversation_lines.append("---")
            conversation_lines.append("")
            stats["assistant_turns"] += 1

        elif event_type == "tool":
            tool_name = data.get("name", "Unknown")
            tool_input = data.get("input", {})
            result = data.get("result", "")

            stats["tool_calls"] += 1

            _output_tool_section(
                tool_name,
                tool_input,
                result,
                conversation_lines,
                tool_result_files,
                tool_result_sequence,
            )
            if result and len(result) > TOOL_RESULT_FILE_THRESHOLD:
                tool_result_sequence += 1

        elif event_type == "subagent":
            subagent_type = data.get("subagent_type", "unknown")
            description = data.get("description", "")
            prompt = data.get("prompt", "")
            response = data.get("response", "")
            tool_use_id = data.get("tool_use_id", "")

            # Track subagent for file generation
            normalized_type = normalize_subagent_type(subagent_type)
            subagent_type_counts[normalized_type] = subagent_type_counts.get(normalized_type, 0) + 1
            sequence = subagent_type_counts[normalized_type]
            filename = f"subagent_{normalized_type}_{sequence}"

            subagent_infos.append(SubagentInfo(
                subagent_type=subagent_type,
                normalized_type=normalized_type,
                sequence=sequence,
                filename=filename,
                task_description=description,
                task_prompt=prompt,
                response_summary=response,
                tool_use_id=tool_use_id,
            ))

            # Format subagent section
            conversation_lines.append(f"### Subagent: {subagent_type}")
            conversation_lines.append("")
            conversation_lines.append(f"**Task**: {description}")

            prompt_preview = truncate(prompt, SUBAGENT_PROMPT_PREVIEW_LIMIT)
            if len(prompt) > SUBAGENT_PROMPT_PREVIEW_LIMIT:
                conversation_lines.append(f"**Prompt** (first {SUBAGENT_PROMPT_PREVIEW_LIMIT} chars):")
            else:
                conversation_lines.append("**Prompt**:")
            conversation_lines.append(f"> {prompt_preview}")
            conversation_lines.append("")

            summary_preview = truncate(response, SUBAGENT_RESPONSE_SUMMARY_LIMIT)
            if len(response) > SUBAGENT_RESPONSE_SUMMARY_LIMIT:
                conversation_lines.append(f"**Result Summary** (first {SUBAGENT_RESPONSE_SUMMARY_LIMIT} chars):")
            else:
                conversation_lines.append("**Result Summary**:")
            conversation_lines.append(f"> {summary_preview}")
            conversation_lines.append("")
            conversation_lines.append(f"→ Full conversation: [{filename}.md](./{filename}.md)")
            conversation_lines.append("")
            conversation_lines.append("---")
            conversation_lines.append("")

            stats["subagents"] += 1
            stats["tool_calls"] += 1

        elif event_type == "compaction":
            # Output compaction inline at its chronological position
            # Compactions: Header outside PIPELINE_SPECIFIC (like Claude), content inside
            # This allows comparison scripts to see the header while filtering the summary
            compaction_number = data.get("number", 0)
            summary_text = data.get("summary", "")

            conversation_lines.append(f"### Compaction #{compaction_number}")
            conversation_lines.append("")
            conversation_lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
            conversation_lines.append("> **Previous Context Summary** (LiteLLM detected):")
            for line in summary_text.split("\n"):
                conversation_lines.append(f"> {line}")
            conversation_lines.append("<!-- END PIPELINE_SPECIFIC -->")
            conversation_lines.append("")
            conversation_lines.append("---")
            conversation_lines.append("")

    # Build main content
    lines: list[str] = []

    # Header
    lines.append(f"# Session: {claude_session_id[:8]}")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Session ID**: `{claude_session_id}`")
    lines.append("")

    # Pipeline-specific section (LiteLLM only fields)
    # NOTE: Timestamps are INSIDE PIPELINE_SPECIFIC because Phoenix/LiteLLM and Claude JSONL
    # have fundamentally different timestamp sources that will never match exactly.
    # See SUPERVISOR_RESPONSE_TO_AGENT_B.md for rationale.
    lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
    if start_time:
        lines.append(f"- **Started**: {format_timestamp(start_time)} (LiteLLM only)")
    if end_time:
        lines.append(f"- **Ended**: {format_timestamp(end_time)} (LiteLLM only)")
    lines.append(f"- **Tokens**: {total_tokens} (LiteLLM only)")
    if models_used:
        lines.append(f"- **Models Used**: {', '.join(sorted(models_used))} (LiteLLM only)")
    else:
        lines.append("- **Models Used**: *[Unknown]* (LiteLLM only)")

    # NOTE: Compactions are now output INLINE at their chronological position in the conversation,
    # not front-loaded here. See AGENT_B_COMPACTION_PLACEMENT_FIX.md for rationale.
    # Each compaction appears as a PIPELINE_SPECIFIC section within the conversation flow.

    lines.append("<!-- END PIPELINE_SPECIFIC -->")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Conversation")
    lines.append("")

    # Add conversation content
    lines.extend(conversation_lines)

    # Footer
    lines.append(f"*Exported from session `{claude_session_id}`*")
    lines.append(f"*{stats['user_turns']} user turns, {stats['assistant_turns']} assistant turns, {stats['tool_calls']} tool calls, {stats['subagents']} subagents*")
    lines.append("")

    main_content = "\n".join(lines)

    # Generate subagent files
    subagent_files: dict[str, str] = {}
    for info in subagent_infos:
        if info.spans:
            # Full conversation available
            subagent_content = _generate_full_subagent_file(info, claude_session_id)
        else:
            # Summary only (linkage failed)
            subagent_content = _generate_summary_only_subagent_file(info, claude_session_id)
        subagent_files[info.filename] = subagent_content

    return LiteLLMMarkdownExport(
        main_content=main_content,
        subagent_files=subagent_files,
        tool_result_files=tool_result_files,
        session_id=claude_session_id,
        total_tokens=total_tokens,
        models_used=sorted(models_used),
        compactions=compactions,
        stats=stats,
    )


def _get_result_language(tool_name: str, tool_input: dict) -> str:
    """Get language hint for tool result code block."""
    if tool_name == "Read":
        return get_language_hint(tool_input.get("file_path", ""))
    elif tool_name == "Bash":
        return "bash"
    else:
        return "text"


def _output_tool_section(
    tool_name: str,
    tool_input: dict,
    result_str: str,
    conversation_lines: list[str],
    tool_result_files: dict[str, str],
    tool_result_sequence: int,
) -> None:
    """Output a tool section to conversation lines."""
    conversation_lines.append(f"### Tool: {tool_name}")
    conversation_lines.append("")
    conversation_lines.append("**Input**:")
    conversation_lines.append("```text")
    conversation_lines.append(format_tool_input(tool_input))
    conversation_lines.append("```")
    conversation_lines.append("")

    char_count = len(result_str)
    lang = _get_result_language(tool_name, tool_input)

    if char_count == 0:
        conversation_lines.append("**Result** (0 chars):")
        conversation_lines.append("*[Empty result]*")
    elif char_count > TOOL_RESULT_FILE_THRESHOLD:
        file_name = f"{tool_result_sequence + 1:03d}_{tool_name.lower()}"
        tool_result_files[file_name] = result_str
        preview = truncate(result_str, TOOL_RESULT_INLINE_LIMIT)
        conversation_lines.append(f"**Result** ({char_count} chars):")
        conversation_lines.append(f"```{lang}")
        conversation_lines.append(preview)
        conversation_lines.append("```")
        conversation_lines.append("")
        conversation_lines.append(f"→ Full result: [tool_results/{file_name}.txt](./tool_results/{file_name}.txt)")
    else:
        result_display = truncate(result_str, TOOL_RESULT_INLINE_LIMIT)
        conversation_lines.append(f"**Result** ({char_count} chars):")
        conversation_lines.append(f"```{lang}")
        conversation_lines.append(result_display)
        conversation_lines.append("```")

    conversation_lines.append("")
    conversation_lines.append("---")
    conversation_lines.append("")


def _process_subagent_result(
    tool_info: dict,
    result_content: dict,
    conversation_lines: list[str],
    subagent_type_counts: dict[str, int],
    subagent_infos: list[SubagentInfo],
    tool_id_to_trace_id: dict[str, str],
    stats: dict[str, Any],
) -> None:
    """Process a subagent (Task) tool result."""
    tool_input = tool_info.get("input", {})
    tool_use_id = tool_info.get("id", "")

    subagent_type = tool_input.get("subagent_type", "unknown")
    task_description = tool_input.get("description", "")
    task_prompt = tool_input.get("prompt", "")

    # Extract response text
    response_content = result_content.get("content", [])
    if isinstance(response_content, str):
        response_text = response_content
    elif isinstance(response_content, list):
        for block in response_content:
            if isinstance(block, dict) and block.get("type") == "text":
                response_text = block.get("text", "")
                break
        else:
            response_text = str(response_content)
    else:
        response_text = str(response_content)

    normalized_type = normalize_subagent_type(subagent_type)
    subagent_type_counts[normalized_type] = subagent_type_counts.get(normalized_type, 0) + 1
    sequence = subagent_type_counts[normalized_type]
    filename = f"subagent_{normalized_type}_{sequence}"

    # Check for trace linkage (80% success rate)
    trace_id = tool_id_to_trace_id.get(tool_use_id)

    subagent_infos.append(SubagentInfo(
        subagent_type=subagent_type,
        normalized_type=normalized_type,
        sequence=sequence,
        filename=filename,
        task_description=task_description,
        task_prompt=task_prompt,
        response_summary=response_text,
        tool_use_id=tool_use_id,
        trace_id=trace_id,
        # spans would be populated if we do full trace reconstruction
    ))

    # Format subagent section
    conversation_lines.append(f"### Subagent: {subagent_type}")
    conversation_lines.append("")
    conversation_lines.append(f"**Task**: {task_description}")

    prompt_preview = truncate(task_prompt, SUBAGENT_PROMPT_PREVIEW_LIMIT)
    if len(task_prompt) > SUBAGENT_PROMPT_PREVIEW_LIMIT:
        conversation_lines.append(f"**Prompt** (first {SUBAGENT_PROMPT_PREVIEW_LIMIT} chars):")
    else:
        conversation_lines.append("**Prompt**:")
    conversation_lines.append(f"> {prompt_preview}")
    conversation_lines.append("")

    summary_preview = truncate(response_text, SUBAGENT_RESPONSE_SUMMARY_LIMIT)
    if len(response_text) > SUBAGENT_RESPONSE_SUMMARY_LIMIT:
        conversation_lines.append(f"**Result Summary** (first {SUBAGENT_RESPONSE_SUMMARY_LIMIT} chars):")
    else:
        conversation_lines.append("**Result Summary**:")
    conversation_lines.append(f"> {summary_preview}")
    conversation_lines.append("")
    conversation_lines.append(f"→ Full conversation: [{filename}.md](./{filename}.md)")
    conversation_lines.append("")
    conversation_lines.append("---")
    conversation_lines.append("")
    stats["subagents"] += 1


def _generate_full_subagent_file(info: SubagentInfo, parent_session_id: str) -> str:
    """Generate a full subagent file when trace linkage succeeded."""
    # For now, use summary-only format since we don't have full conversation reconstruction
    # This matches the 20% case but will be enhanced later
    return _generate_summary_only_subagent_file(info, parent_session_id)


def _generate_summary_only_subagent_file(info: SubagentInfo, parent_session_id: str) -> str:
    """Generate a summary-only subagent file when trace linkage failed."""
    lines = [
        f"# Subagent: {info.subagent_type} ({info.filename})",
        "",
        "## Context",
        "",
        f"- **Parent Session**: `{parent_session_id}`",
    ]

    if info.start_time:
        lines.append(f"- **Started**: {format_timestamp(info.start_time)}")
    if info.end_time:
        lines.append(f"- **Ended**: {format_timestamp(info.end_time)}")

    lines.append("")
    lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
    if info.total_tokens:
        lines.append(f"- **Duration**: {info.total_tokens}s (LiteLLM only)")
    if info.total_tokens:
        lines.append(f"- **Tokens**: {info.total_tokens} (LiteLLM only)")
    if info.tool_count:
        lines.append(f"- **Tool Calls**: {info.tool_count} (LiteLLM only)")
    lines.append("<!-- END PIPELINE_SPECIFIC -->")
    lines.append("")
    lines.append("## Task Prompt")
    lines.append("")
    lines.append(info.task_prompt)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Conversation")
    lines.append("")
    lines.append("*[Full conversation not available - showing response summary only]*")
    lines.append("")
    lines.append("### Response Summary")
    lines.append("")
    lines.append(info.response_summary)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Subagent of session `{parent_session_id}`*")
    lines.append("")

    return "\n".join(lines)


def export_to_files(
    export: LiteLLMMarkdownExport,
    output_dir: str | Path,
    main_filename: str | None = None,
) -> list[Path]:
    """
    Write export result to files.

    Args:
        export: LiteLLMMarkdownExport result.
        output_dir: Directory to write files to.
        main_filename: Filename for main session (default: {session_id}.md).

    Returns:
        List of paths to written files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    # Main file
    if main_filename is None:
        main_filename = f"{export.session_id}.md"

    main_path = output_dir / main_filename
    main_path.write_text(export.main_content, encoding="utf-8")
    written.append(main_path)

    # Subagent files
    for filename, content in sorted(export.subagent_files.items()):
        subagent_path = output_dir / f"{filename}.md"
        subagent_path.write_text(content, encoding="utf-8")
        written.append(subagent_path)

    # Tool result files
    if export.tool_result_files:
        tool_results_dir = output_dir / "tool_results"
        tool_results_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in sorted(export.tool_result_files.items()):
            result_path = tool_results_dir / f"{filename}.txt"
            result_path.write_text(content, encoding="utf-8")
            written.append(result_path)

    return written


# CLI helper for quick testing
if __name__ == "__main__":
    import sys

    print("LiteLLM Markdown Export Module")
    print("This module exports Phoenix trace data to markdown format.")
    print("Use via: from dev_agent_lens.export.markdown_litellm import export_chain_to_unified_markdown")
