"""
Claude Code Session to Markdown Exporter

Converts raw Claude Code JSONL session files into readable markdown format.
Implements the AGREED_FORMAT.md specification for unified export.

Design principles:
- Deterministic output (same input always produces identical output)
- Exact string matching for unit tests
- PIPELINE_SPECIFIC sections for fields that differ between pipelines
- Subagent files named by type and sequence (subagent_{type}_{n}.md)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


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
class MarkdownExport:
    """Result of exporting a session to markdown."""

    main_content: str
    """Main session markdown content."""

    subagent_files: dict[str, str] = field(default_factory=dict)
    """Map of subagent filename (without .md) -> markdown content."""

    tool_result_files: dict[str, str] = field(default_factory=dict)
    """Map of tool result filename -> content for large results."""

    session_id: str = ""
    """Session identifier (full 36-char UUID)."""

    project_path: str = ""
    """Original project path (cwd)."""

    git_branch: str | None = None
    """Git branch if available."""

    summary: str = ""
    """Auto-generated summary if available."""

    stats: dict[str, Any] = field(default_factory=dict)
    """Export statistics (turns, tools, subagents, etc.)."""


@dataclass
class SessionMessage:
    """Parsed message from JSONL."""

    uuid: str
    parent_uuid: str | None
    type: str  # user, assistant, system, summary, file-history-snapshot
    subtype: str | None  # For system messages: compact_boundary, etc.
    timestamp: datetime | None
    content: Any  # str or list of content blocks

    # All tool_use blocks from this message (for parallel detection)
    tool_uses: list[dict] = field(default_factory=list)

    # Tool result from toolUseResult field
    tool_use_result: Any = None  # str for regular tools, dict for subagents

    # Compaction metadata (for compact_boundary messages)
    compact_metadata: dict = field(default_factory=dict)

    # Metadata
    session_id: str | None = None
    agent_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None

    # Raw for debugging
    raw: dict = field(default_factory=dict)


@dataclass
class SubagentInfo:
    """Tracking info for a subagent."""

    agent_id: str
    subagent_type: str
    normalized_type: str
    sequence: int
    filename: str
    task_description: str
    task_prompt: str
    response_summary: str
    duration_ms: int
    total_tokens: int
    tool_count: int


# =============================================================================
# Parsing Functions
# =============================================================================


def parse_jsonl_file(file_path: str | Path) -> Iterator[dict]:
    """Parse a JSONL file, yielding each line as a dict."""
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass  # Skip malformed lines


def parse_message(raw: dict) -> SessionMessage:
    """Parse a raw JSONL object into a SessionMessage."""
    msg_type = raw.get("type", "unknown")
    msg_subtype = raw.get("subtype")

    # Parse timestamp
    ts = raw.get("timestamp")
    timestamp = None
    if ts:
        try:
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # Extract content from message wrapper (for user/assistant)
    # For system messages, content might be at top level
    message = raw.get("message", {})
    content = message.get("content", "")
    if not content and msg_type == "system":
        content = raw.get("content", "")

    # Extract ALL tool_use blocks from assistant messages
    tool_uses = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_uses.append({
                    "name": block.get("name"),
                    "id": block.get("id"),
                    "input": block.get("input", {}),
                })

    # Get toolUseResult (could be string for regular tools or dict for subagents)
    tool_use_result = raw.get("toolUseResult")

    # Get compaction metadata (for compact_boundary messages)
    compact_metadata = {}
    if msg_subtype == "compact_boundary":
        metadata = raw.get("compactMetadata", {})
        compact_metadata = {
            "trigger": metadata.get("trigger", "unknown"),
            "pre_tokens": metadata.get("preTokens", 0),
        }

    return SessionMessage(
        uuid=raw.get("uuid", ""),
        parent_uuid=raw.get("parentUuid"),
        type=msg_type,
        subtype=msg_subtype,
        timestamp=timestamp,
        content=content,
        tool_uses=tool_uses,
        tool_use_result=tool_use_result,
        compact_metadata=compact_metadata,
        session_id=raw.get("sessionId"),
        agent_id=raw.get("agentId"),
        cwd=raw.get("cwd"),
        git_branch=raw.get("gitBranch"),
        raw=raw,
    )


# =============================================================================
# Formatting Helpers
# =============================================================================


def truncate(text: str, limit: int) -> str:
    """Truncate text to limit chars, showing (limit-3) + '...' if exceeded."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def normalize_subagent_type(subagent_type: str) -> str:
    """Normalize subagent type for filename: lowercase, replace - and spaces with _."""
    return subagent_type.lower().replace("-", "_").replace(" ", "_")


def is_compaction_boundary(message: dict) -> bool:
    """Check if a message is a compaction boundary marker."""
    return (
        message.get("type") == "system" and
        message.get("subtype") == "compact_boundary"
    )


def get_compaction_metadata(message: dict) -> dict:
    """Extract compaction metadata from a boundary message."""
    metadata = message.get("compactMetadata", {})
    return {
        "trigger": metadata.get("trigger", "unknown"),
        "pre_tokens": metadata.get("preTokens", 0),
    }


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


def extract_text_content(content: Any) -> str:
    """Extract plain text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts)

    return str(content) if content else ""


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

    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        if len(path) > PARALLEL_TOOL_TARGET_LIMIT:
            return "..." + path[-(PARALLEL_TOOL_TARGET_LIMIT - 3) :]
        return path

    elif tool_name == "Edit":
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


def extract_subagent_response_text(tool_use_result: dict) -> str:
    """Extract text content from subagent toolUseResult."""
    content = tool_use_result.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


# =============================================================================
# JSONL Intermediate Format Export
# =============================================================================


def export_session_to_jsonl(
    session_file: str | Path,
    project_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    Export a Claude Code session to JSONL intermediate format.

    Uses a schema compatible with the shared markdown renderer so both
    Claude and LiteLLM pipelines can use the same rendering code.

    Args:
        session_file: Path to the .jsonl session file.
        project_dir: Project directory containing subagent files. If None,
                     inferred from session_file location.

    Returns:
        List of JSONL records representing the conversation.
    """
    session_file = Path(session_file)

    if project_dir is None:
        project_dir = session_file.parent
    else:
        project_dir = Path(project_dir)

    # Parse all messages
    messages: list[SessionMessage] = []
    for raw in parse_jsonl_file(session_file):
        msg = parse_message(raw)
        messages.append(msg)

    if not messages:
        return [
            {"record_type": "header", "session_id": "", "schema_version": "1.0"},
            {"record_type": "footer", "stats": {"user_turns": 0, "assistant_turns": 0, "tool_calls": 0, "subagents": 0, "compactions": 0}},
        ]

    # Extract session metadata
    session_id = session_file.stem  # UUID from filename
    cwd = None
    git_branch = None
    summary = ""

    for msg in messages:
        if msg.type == "summary":
            summary = msg.raw.get("summary", "")
        if msg.cwd and not cwd:
            cwd = msg.cwd
        if msg.git_branch and not git_branch:
            git_branch = msg.git_branch

    # Get timestamps
    timestamps = [m.timestamp for m in messages if m.timestamp]
    start_time = min(timestamps) if timestamps else None
    end_time = max(timestamps) if timestamps else None

    # Build JSONL records
    records: list[dict[str, Any]] = []

    # Stats tracking
    stats = {
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "subagents": 0,
        "compactions": 0,
    }

    # Subagent tracking
    subagent_type_counts: dict[str, int] = {}

    # Track pending tool calls
    pending_tools: dict[str, dict] = {}
    order_index = 0

    # Process messages
    i = 0
    while i < len(messages):
        msg = messages[i]

        # Skip non-conversation types
        if msg.type in ("summary", "file-history-snapshot"):
            i += 1
            continue

        # Handle compaction boundary
        if msg.type == "system" and msg.subtype == "compact_boundary":
            stats["compactions"] += 1

            # Get summary from next message
            summary_text = ""
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if next_msg.type == "user":
                    summary_text = extract_text_content(next_msg.content)

            records.append({
                "record_type": "event",
                "event_type": "compaction",
                "order_index": order_index,
                "number": stats["compactions"],
                "trigger": msg.compact_metadata.get("trigger", "unknown"),
                "pre_tokens": msg.compact_metadata.get("pre_tokens", 0),
                "summary": summary_text,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
            })
            order_index += 1

            # Skip the continuation summary message
            if i + 1 < len(messages) and messages[i + 1].type == "user":
                i += 2
            else:
                i += 1
            continue

        # User message with tool result
        if msg.type == "user" and msg.tool_use_result is not None:
            result = msg.tool_use_result

            # Check if subagent result
            if isinstance(result, dict) and result.get("agentId"):
                agent_id = result.get("agentId", "")

                # Find matching Task tool
                task_info = None
                for tid, info in list(pending_tools.items()):
                    if info.get("name") == "Task":
                        task_info = info
                        del pending_tools[tid]
                        break

                if task_info:
                    subagent_type = task_info["input"].get("subagent_type", "unknown")
                    task_description = task_info["input"].get("description", "")
                    task_prompt = task_info["input"].get("prompt", "")
                else:
                    subagent_type = "unknown"
                    task_description = ""
                    task_prompt = result.get("prompt", "")

                normalized_type = normalize_subagent_type(subagent_type)
                subagent_type_counts[normalized_type] = subagent_type_counts.get(normalized_type, 0) + 1
                sequence = subagent_type_counts[normalized_type]
                filename = f"subagent_{normalized_type}_{sequence}"

                response_text = extract_subagent_response_text(result)

                records.append({
                    "record_type": "event",
                    "event_type": "subagent",
                    "order_index": order_index,
                    "subagent_type": subagent_type,
                    "description": task_description,
                    "prompt": task_prompt,
                    "response": response_text,
                    "filename": filename,
                    "agent_id": agent_id,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                })
                order_index += 1
                stats["subagents"] += 1

            else:
                # Regular tool result
                result_str = str(result) if result else ""

                # Find matching tool call
                tool_info = None
                tool_id = None
                for tid, info in list(pending_tools.items()):
                    if not info.get("result_received"):
                        tool_info = info
                        tool_id = tid
                        break

                if tool_info:
                    tool_name = tool_info.get("name", "Unknown")
                    tool_input = tool_info.get("input", {})
                    tool_info["result_received"] = True

                    records.append({
                        "record_type": "event",
                        "event_type": "tool",
                        "order_index": order_index,
                        "name": tool_name,
                        "input": tool_input,
                        "result": result_str,
                        "tool_use_id": tool_id,
                        "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                    })
                    order_index += 1
                    stats["tool_calls"] += 1

            i += 1
            continue

        # Regular user message
        if msg.type == "user":
            content = extract_text_content(msg.content)
            if content and content.strip():
                records.append({
                    "record_type": "event",
                    "event_type": "user",
                    "order_index": order_index,
                    "text": content,
                    "message_id": msg.uuid,
                    "parent_id": msg.parent_uuid,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                })
                order_index += 1
                stats["user_turns"] += 1

            i += 1
            continue

        # Assistant message
        if msg.type == "assistant":
            # Register tool uses
            if msg.tool_uses:
                for tool_use in msg.tool_uses:
                    tool_id = tool_use.get("id", "")
                    pending_tools[tool_id] = {
                        "name": tool_use.get("name"),
                        "input": tool_use.get("input", {}),
                        "result_received": False,
                    }

            # Text content
            text_content = extract_text_content(msg.content)
            if text_content and text_content.strip():
                records.append({
                    "record_type": "event",
                    "event_type": "assistant",
                    "order_index": order_index,
                    "text": text_content,
                    "message_id": msg.uuid,
                    "parent_id": msg.parent_uuid,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                })
                order_index += 1
                stats["assistant_turns"] += 1

            i += 1
            continue

        i += 1

    # Create header record (insert at beginning)
    header = {
        "record_type": "header",
        "schema_version": "1.0",
        "session_id": session_id,
        "chain_id": session_id,  # For LiteLLM compatibility
        "claude_session_id": session_id,
        "pipeline": "claude",
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "compaction_count": stats["compactions"],
        "metadata": {
            "project_path": cwd,
            "git_branch": git_branch,
            "summary": summary,
        },
    }

    # Create footer record
    footer = {
        "record_type": "footer",
        "stats": stats,
    }

    return [header] + records + [footer]


# =============================================================================
# Main Export Function
# =============================================================================


def export_session_to_markdown(
    session_file: str | Path,
    project_dir: str | Path | None = None,
) -> MarkdownExport:
    """
    Export a Claude Code session JSONL file to markdown.

    Implements the AGREED_FORMAT.md specification.

    Args:
        session_file: Path to the .jsonl session file.
        project_dir: Project directory containing subagent files. If None,
                     inferred from session_file location.

    Returns:
        MarkdownExport with main content, subagent files, and tool result files.
    """
    session_file = Path(session_file)

    if project_dir is None:
        project_dir = session_file.parent
    else:
        project_dir = Path(project_dir)

    # Parse all messages
    messages: list[SessionMessage] = []
    for raw in parse_jsonl_file(session_file):
        msg = parse_message(raw)
        messages.append(msg)

    if not messages:
        return MarkdownExport(
            main_content="# Empty Session\n\nNo messages found.\n",
            session_id="",
            stats={"user_turns": 0, "assistant_turns": 0, "tool_calls": 0, "subagents": 0},
        )

    # Extract session metadata
    session_id = session_file.stem  # UUID from filename
    cwd = None
    git_branch = None
    summary = ""

    for msg in messages:
        if msg.type == "summary":
            summary = msg.raw.get("summary", "")
        if msg.cwd and not cwd:
            cwd = msg.cwd
        if msg.git_branch and not git_branch:
            git_branch = msg.git_branch

    # Get timestamps
    timestamps = [m.timestamp for m in messages if m.timestamp]
    start_time = min(timestamps) if timestamps else None
    end_time = max(timestamps) if timestamps else None

    # Stats tracking
    stats = {
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "subagents": 0,
    }

    # Subagent tracking: type -> count for sequence numbering
    subagent_type_counts: dict[str, int] = {}
    subagent_infos: list[SubagentInfo] = []

    # Tool result file tracking
    tool_result_files: dict[str, str] = {}
    tool_result_sequence = 0

    # Build conversation content
    conversation_lines: list[str] = []

    # Track which messages we've processed (for tool results)
    tool_use_id_to_info: dict[str, dict] = {}  # tool_use_id -> {name, input, ...}

    for msg in messages:
        # Skip non-conversation types
        if msg.type == "summary":
            continue
        if msg.type == "file-history-snapshot":
            continue

        # User message
        if msg.type == "user":
            # Check if this is a tool result
            if msg.tool_use_result is not None:
                # This is a tool result, will be processed with the tool call
                pass
            else:
                # Regular user message
                content = extract_text_content(msg.content)
                if content and content.strip():
                    conversation_lines.append("### User")
                    conversation_lines.append("")
                    conversation_lines.append(content)
                    conversation_lines.append("")
                    conversation_lines.append("---")
                    conversation_lines.append("")
                    stats["user_turns"] += 1

        # Assistant message
        elif msg.type == "assistant":
            # Check for tool uses
            if msg.tool_uses:
                if len(msg.tool_uses) > 1:
                    # Parallel tools
                    conversation_lines.append(f"### Parallel Tools ({len(msg.tool_uses)} calls)")
                    conversation_lines.append("")
                    conversation_lines.append("| # | Tool | Target |")
                    conversation_lines.append("|---|------|--------|")

                    for i, tool_use in enumerate(msg.tool_uses, 1):
                        tool_name = tool_use.get("name", "Unknown")
                        tool_input = tool_use.get("input", {})
                        tool_id = tool_use.get("id", "")
                        target = get_tool_target_brief(tool_name, tool_input)
                        conversation_lines.append(f"| {i} | {tool_name} | {target} |")
                        tool_use_id_to_info[tool_id] = {
                            "name": tool_name,
                            "input": tool_input,
                            "parallel_index": i,
                            "is_parallel": True,
                        }
                        stats["tool_calls"] += 1

                    conversation_lines.append("")
                    # Results will be added when we see the tool_use_result messages

                else:
                    # Single tool call
                    tool_use = msg.tool_uses[0]
                    tool_name = tool_use.get("name", "Unknown")
                    tool_input = tool_use.get("input", {})
                    tool_id = tool_use.get("id", "")

                    # Check if it's a Task (subagent) - handle specially
                    if tool_name == "Task":
                        tool_use_id_to_info[tool_id] = {
                            "name": tool_name,
                            "input": tool_input,
                            "is_parallel": False,
                        }
                        stats["tool_calls"] += 1
                        # The subagent section will be added when we see the result
                    else:
                        tool_use_id_to_info[tool_id] = {
                            "name": tool_name,
                            "input": tool_input,
                            "is_parallel": False,
                        }
                        stats["tool_calls"] += 1
                        # Tool header and input will be shown, result comes later

            # Text content (separate from tool use)
            text_content = extract_text_content(msg.content)
            if text_content and text_content.strip():
                conversation_lines.append("### Assistant")
                conversation_lines.append("")
                conversation_lines.append(text_content)
                conversation_lines.append("")
                conversation_lines.append("---")
                conversation_lines.append("")
                stats["assistant_turns"] += 1

    # Now we need to re-process to properly interleave tool calls with results
    # The above approach doesn't work well. Let me rewrite with a cleaner approach.

    # Clear and restart
    conversation_lines = []
    stats = {"user_turns": 0, "assistant_turns": 0, "tool_calls": 0, "subagents": 0, "compactions": 0}
    subagent_type_counts = {}
    subagent_infos = []
    tool_result_files = {}
    tool_result_sequence = 0
    compaction_files: dict[str, str] = {}  # For compaction summaries > 500 chars

    # Track pending tool calls waiting for results
    pending_tools: dict[str, dict] = {}  # tool_use_id -> info
    pending_parallel_group: list[dict] | None = None  # For parallel tools
    pending_parallel_results: dict[str, str] = {}  # tool_use_id -> result content

    i = 0
    while i < len(messages):
        msg = messages[i]

        # Skip non-conversation types
        if msg.type in ("summary", "file-history-snapshot"):
            i += 1
            continue

        # Handle compaction boundary (system message with subtype compact_boundary)
        if msg.type == "system" and msg.subtype == "compact_boundary":
            stats["compactions"] += 1
            compaction_num = stats["compactions"]

            # Get metadata
            trigger = msg.compact_metadata.get("trigger", "unknown")
            pre_tokens = msg.compact_metadata.get("pre_tokens", 0)

            # Get continuation summary from next message (should be user message)
            summary_text = ""
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if next_msg.type == "user":
                    summary_text = extract_text_content(next_msg.content)

            # Format compaction section
            conversation_lines.append(f"### Compaction #{compaction_num}")
            conversation_lines.append("")
            conversation_lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
            conversation_lines.append(f"- **Trigger**: {trigger} (Claude only)")
            conversation_lines.append(f"- **Pre-compaction tokens**: {pre_tokens} (Claude only)")
            conversation_lines.append("<!-- END PIPELINE_SPECIFIC -->")
            conversation_lines.append("")

            if summary_text:
                summary_preview = truncate(summary_text, SUBAGENT_RESPONSE_SUMMARY_LIMIT)
                conversation_lines.append("> **Context Summary**:")
                # Format as blockquote (each line prefixed with >)
                for line in summary_preview.split("\n"):
                    conversation_lines.append(f"> {line}")
                conversation_lines.append("")

                if len(summary_text) > SUBAGENT_RESPONSE_SUMMARY_LIMIT:
                    file_name = f"compaction_{compaction_num}_summary"
                    compaction_files[file_name] = summary_text
                    conversation_lines.append(f"→ Full summary: [{file_name}.txt](./{file_name}.txt)")
                    conversation_lines.append("")

            conversation_lines.append("---")
            conversation_lines.append("")

            # Skip the next message (the continuation summary) since we already processed it
            if i + 1 < len(messages) and messages[i + 1].type == "user":
                i += 2
            else:
                i += 1
            continue

        # User message with tool result
        if msg.type == "user" and msg.tool_use_result is not None:
            result = msg.tool_use_result

            # Check if this is a subagent result (dict with agentId)
            if isinstance(result, dict) and result.get("agentId"):
                agent_id = result.get("agentId", "")
                # Find the corresponding Task tool call
                task_prompt = result.get("prompt", "")

                # Look for Task tool info in pending_tools
                task_info = None
                for tid, info in list(pending_tools.items()):
                    if info.get("name") == "Task":
                        task_info = info
                        del pending_tools[tid]
                        break

                if task_info:
                    subagent_type = task_info["input"].get("subagent_type", "unknown")
                    task_description = task_info["input"].get("description", "")
                    task_prompt = task_info["input"].get("prompt", task_prompt)
                else:
                    subagent_type = "unknown"
                    task_description = ""

                normalized_type = normalize_subagent_type(subagent_type)
                subagent_type_counts[normalized_type] = subagent_type_counts.get(normalized_type, 0) + 1
                sequence = subagent_type_counts[normalized_type]
                filename = f"subagent_{normalized_type}_{sequence}"

                response_text = extract_subagent_response_text(result)
                duration_ms = result.get("totalDurationMs", 0)
                total_tokens = result.get("totalTokens", 0)
                tool_count = result.get("totalToolUseCount", 0)

                subagent_infos.append(SubagentInfo(
                    agent_id=agent_id,
                    subagent_type=subagent_type,
                    normalized_type=normalized_type,
                    sequence=sequence,
                    filename=filename,
                    task_description=task_description,
                    task_prompt=task_prompt,
                    response_summary=response_text,
                    duration_ms=duration_ms,
                    total_tokens=total_tokens,
                    tool_count=tool_count,
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

            else:
                # Regular tool result (string)
                result_str = str(result) if result else ""

                # Find the tool this result belongs to
                # Look at the previous assistant message for tool_use
                tool_info = None
                tool_id = None
                for tid, info in list(pending_tools.items()):
                    if not info.get("result_received"):
                        tool_info = info
                        tool_id = tid
                        break

                if tool_info:
                    tool_name = tool_info.get("name", "Unknown")
                    tool_input = tool_info.get("input", {})
                    is_parallel = tool_info.get("is_parallel", False)
                    parallel_index = tool_info.get("parallel_index", 0)

                    if is_parallel:
                        # Store result for parallel group
                        pending_parallel_results[tool_id] = result_str
                        tool_info["result_received"] = True

                        # Check if all parallel results received
                        if pending_parallel_group:
                            all_received = all(
                                pending_tools.get(t.get("id", ""), {}).get("result_received", False)
                                for t in pending_parallel_group
                            )
                            if all_received:
                                # Output parallel results
                                conversation_lines.append("**Results**:")
                                conversation_lines.append("")
                                for j, tool in enumerate(pending_parallel_group, 1):
                                    t_id = tool.get("id", "")
                                    t_name = tool.get("name", "Unknown")
                                    t_result = pending_parallel_results.get(t_id, "")
                                    char_count = len(t_result)

                                    if char_count == 0:
                                        conversation_lines.append(f"**[{j}]** (0 chars):")
                                        conversation_lines.append("*[Empty result]*")
                                    elif char_count > TOOL_RESULT_FILE_THRESHOLD:
                                        # External file needed
                                        tool_result_sequence += 1
                                        file_name = f"{tool_result_sequence:03d}_{t_name.lower()}"
                                        tool_result_files[file_name] = t_result
                                        preview = truncate(t_result, TOOL_RESULT_INLINE_LIMIT)
                                        conversation_lines.append(f"**[{j}]** ({char_count} chars):")
                                        conversation_lines.append(preview)
                                        conversation_lines.append("")
                                        conversation_lines.append(f"→ Full result: [tool_results/{file_name}.txt](./tool_results/{file_name}.txt)")
                                    else:
                                        result_display = truncate(t_result, TOOL_RESULT_INLINE_LIMIT)
                                        # Determine language hint
                                        t_input = pending_tools.get(t_id, {}).get("input", {})
                                        if t_name == "Read":
                                            lang = get_language_hint(t_input.get("file_path", ""))
                                        elif t_name == "Bash":
                                            lang = "bash"
                                        else:
                                            lang = "text"
                                        conversation_lines.append(f"**[{j}]** ({char_count} chars):")
                                        conversation_lines.append(f"```{lang}")
                                        conversation_lines.append(result_display)
                                        conversation_lines.append("```")
                                    conversation_lines.append("")

                                conversation_lines.append("---")
                                conversation_lines.append("")

                                # Clean up
                                for t in pending_parallel_group:
                                    t_id = t.get("id", "")
                                    if t_id in pending_tools:
                                        del pending_tools[t_id]
                                pending_parallel_group = None
                                pending_parallel_results = {}

                    else:
                        # Single tool - output tool section now
                        conversation_lines.append(f"### Tool: {tool_name}")
                        conversation_lines.append("")
                        conversation_lines.append("**Input**:")
                        conversation_lines.append("```text")
                        conversation_lines.append(format_tool_input(tool_input))
                        conversation_lines.append("```")
                        conversation_lines.append("")

                        char_count = len(result_str)
                        if char_count == 0:
                            conversation_lines.append("**Result** (0 chars):")
                            conversation_lines.append("*[Empty result]*")
                        elif char_count > TOOL_RESULT_FILE_THRESHOLD:
                            tool_result_sequence += 1
                            file_name = f"{tool_result_sequence:03d}_{tool_name.lower()}"
                            tool_result_files[file_name] = result_str
                            preview = truncate(result_str, TOOL_RESULT_INLINE_LIMIT)
                            conversation_lines.append(f"**Result** ({char_count} chars):")
                            # Determine language
                            if tool_name == "Read":
                                lang = get_language_hint(tool_input.get("file_path", ""))
                            elif tool_name == "Bash":
                                lang = "bash"
                            else:
                                lang = "text"
                            conversation_lines.append(f"```{lang}")
                            conversation_lines.append(preview)
                            conversation_lines.append("```")
                            conversation_lines.append("")
                            conversation_lines.append(f"→ Full result: [tool_results/{file_name}.txt](./tool_results/{file_name}.txt)")
                        else:
                            result_display = truncate(result_str, TOOL_RESULT_INLINE_LIMIT)
                            if tool_name == "Read":
                                lang = get_language_hint(tool_input.get("file_path", ""))
                            elif tool_name == "Bash":
                                lang = "bash"
                            else:
                                lang = "text"
                            conversation_lines.append(f"**Result** ({char_count} chars):")
                            conversation_lines.append(f"```{lang}")
                            conversation_lines.append(result_display)
                            conversation_lines.append("```")

                        conversation_lines.append("")
                        conversation_lines.append("---")
                        conversation_lines.append("")

                        if tool_id:
                            del pending_tools[tool_id]

            i += 1
            continue

        # Regular user message (no tool result)
        if msg.type == "user":
            content = extract_text_content(msg.content)
            if content and content.strip():
                conversation_lines.append("### User")
                conversation_lines.append("")
                conversation_lines.append(content)
                conversation_lines.append("")
                conversation_lines.append("---")
                conversation_lines.append("")
                stats["user_turns"] += 1
            i += 1
            continue

        # Assistant message
        if msg.type == "assistant":
            # Extract text content (non-tool parts)
            text_content = extract_text_content(msg.content)

            if msg.tool_uses:
                # Has tool calls
                if len(msg.tool_uses) > 1:
                    # Parallel tools
                    conversation_lines.append(f"### Parallel Tools ({len(msg.tool_uses)} calls)")
                    conversation_lines.append("")
                    conversation_lines.append("| # | Tool | Target |")
                    conversation_lines.append("|---|------|--------|")

                    pending_parallel_group = msg.tool_uses
                    for j, tool_use in enumerate(msg.tool_uses, 1):
                        tool_name = tool_use.get("name", "Unknown")
                        tool_input = tool_use.get("input", {})
                        tool_id = tool_use.get("id", "")
                        target = get_tool_target_brief(tool_name, tool_input)
                        conversation_lines.append(f"| {j} | {tool_name} | {target} |")
                        pending_tools[tool_id] = {
                            "name": tool_name,
                            "input": tool_input,
                            "is_parallel": True,
                            "parallel_index": j,
                            "result_received": False,
                        }
                        stats["tool_calls"] += 1

                    conversation_lines.append("")

                else:
                    # Single tool
                    tool_use = msg.tool_uses[0]
                    tool_name = tool_use.get("name", "Unknown")
                    tool_input = tool_use.get("input", {})
                    tool_id = tool_use.get("id", "")

                    pending_tools[tool_id] = {
                        "name": tool_name,
                        "input": tool_input,
                        "is_parallel": False,
                    }
                    stats["tool_calls"] += 1

                    # Don't output tool section yet - wait for result
                    # But if there's text content before the tool, output that
                    if text_content and text_content.strip():
                        conversation_lines.append("### Assistant")
                        conversation_lines.append("")
                        conversation_lines.append(text_content)
                        conversation_lines.append("")
                        conversation_lines.append("---")
                        conversation_lines.append("")
                        stats["assistant_turns"] += 1
                        text_content = ""  # Don't output again

            # Text content without tool use
            if text_content and text_content.strip() and not msg.tool_uses:
                conversation_lines.append("### Assistant")
                conversation_lines.append("")
                conversation_lines.append(text_content)
                conversation_lines.append("")
                conversation_lines.append("---")
                conversation_lines.append("")
                stats["assistant_turns"] += 1

            i += 1
            continue

        i += 1

    # Build main content
    lines: list[str] = []

    # Header
    lines.append(f"# Session: {session_id[:8]}")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Session ID**: `{session_id}`")
    lines.append("")

    # Pipeline-specific section (Claude only fields)
    lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
    if start_time:
        lines.append(f"- **Started**: {format_timestamp(start_time)} (Claude only)")
    if end_time:
        lines.append(f"- **Ended**: {format_timestamp(end_time)} (Claude only)")
    if cwd:
        lines.append(f"- **Project**: `{cwd}` (Claude only)")
    if git_branch:
        lines.append(f"- **Branch**: `{git_branch}` (Claude only)")
    else:
        lines.append("- **Branch**: *[No branch]* (Claude only)")
    if summary:
        lines.append(f"- **Summary**: {summary} (Claude only)")
    else:
        lines.append("- **Summary**: *[No summary]* (Claude only)")
    lines.append("<!-- END PIPELINE_SPECIFIC -->")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Conversation")
    lines.append("")

    # Add conversation content
    lines.extend(conversation_lines)

    # Footer
    lines.append(f"*Exported from session `{session_id}`*")
    footer_parts = [
        f"{stats['user_turns']} user turns",
        f"{stats['assistant_turns']} assistant turns",
        f"{stats['tool_calls']} tool calls",
        f"{stats['subagents']} subagents",
    ]
    if stats["compactions"] > 0:
        footer_parts.append(f"{stats['compactions']} compactions")
    lines.append(f"*{', '.join(footer_parts)}*")
    lines.append("")

    main_content = "\n".join(lines)

    # Generate subagent files
    subagent_files: dict[str, str] = {}
    for info in subagent_infos:
        agent_file = project_dir / f"agent-{info.agent_id}.jsonl"
        if agent_file.exists():
            try:
                # Recursively export subagent
                subagent_export = export_session_to_markdown(agent_file, project_dir)

                # Wrap in subagent format
                subagent_lines = [
                    f"# Subagent: {info.subagent_type} ({info.filename})",
                    "",
                    "## Context",
                    "",
                    f"- **Parent Session**: `{session_id}`",
                ]

                # Get subagent timestamps
                subagent_messages = list(parse_jsonl_file(agent_file))
                subagent_parsed = [parse_message(m) for m in subagent_messages]
                subagent_ts = [m.timestamp for m in subagent_parsed if m.timestamp]

                subagent_lines.append("")
                subagent_lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
                if subagent_ts:
                    subagent_lines.append(f"- **Started**: {format_timestamp(min(subagent_ts))} (Claude only)")
                    subagent_lines.append(f"- **Ended**: {format_timestamp(max(subagent_ts))} (Claude only)")
                subagent_lines.append(f"- **Agent ID**: `{info.agent_id}` (Claude only)")
                subagent_lines.append("<!-- END PIPELINE_SPECIFIC -->")
                subagent_lines.append("")
                subagent_lines.append("## Task Prompt")
                subagent_lines.append("")
                subagent_lines.append(info.task_prompt)
                subagent_lines.append("")
                subagent_lines.append("---")
                subagent_lines.append("")
                subagent_lines.append("## Conversation")
                subagent_lines.append("")

                # Extract conversation part from subagent export
                subagent_main = subagent_export.main_content
                # Find the ## Conversation section
                if "## Conversation" in subagent_main:
                    conv_start = subagent_main.index("## Conversation") + len("## Conversation")
                    # Find the footer
                    footer_marker = "*Exported from session"
                    if footer_marker in subagent_main:
                        conv_end = subagent_main.index(footer_marker)
                        conversation_part = subagent_main[conv_start:conv_end].strip()
                        subagent_lines.append(conversation_part)
                    else:
                        subagent_lines.append(subagent_main[conv_start:].strip())
                else:
                    subagent_lines.append(subagent_export.main_content)

                subagent_lines.append("")
                subagent_lines.append("---")
                subagent_lines.append("")
                subagent_lines.append(f"*Subagent of session `{session_id}`*")
                subagent_lines.append("")

                subagent_files[info.filename] = "\n".join(subagent_lines)

                # Include nested subagent files
                for nested_name, nested_content in subagent_export.subagent_files.items():
                    subagent_files[nested_name] = nested_content

            except Exception as e:
                # Subagent file failed to load - use summary only format
                subagent_files[info.filename] = _generate_summary_only_subagent(info, session_id)
        else:
            # Subagent file doesn't exist - use summary only format
            subagent_files[info.filename] = _generate_summary_only_subagent(info, session_id)

    # Merge compaction files into tool_result_files (they're all supplementary text files)
    all_result_files = {**tool_result_files, **compaction_files}

    return MarkdownExport(
        main_content=main_content,
        subagent_files=subagent_files,
        tool_result_files=all_result_files,
        session_id=session_id,
        project_path=cwd or "",
        git_branch=git_branch,
        summary=summary,
        stats=stats,
    )


def _generate_summary_only_subagent(info: SubagentInfo, parent_session_id: str) -> str:
    """Generate a summary-only subagent file when full conversation unavailable."""
    lines = [
        f"# Subagent: {info.subagent_type} ({info.filename})",
        "",
        "## Context",
        "",
        f"- **Parent Session**: `{parent_session_id}`",
        "",
        "<!-- BEGIN PIPELINE_SPECIFIC -->",
        f"- **Agent ID**: `{info.agent_id}` (Claude only)",
        "<!-- END PIPELINE_SPECIFIC -->",
        "",
        "## Task Prompt",
        "",
        info.task_prompt,
        "",
        "---",
        "",
        "## Conversation",
        "",
        "*[Full conversation not available - showing response summary only]*",
        "",
        "### Response Summary",
        "",
        info.response_summary,
        "",
        "---",
        "",
        f"*Subagent of session `{parent_session_id}`*",
        "",
    ]
    return "\n".join(lines)


def export_to_files(
    export: MarkdownExport,
    output_dir: str | Path,
    main_filename: str | None = None,
) -> list[Path]:
    """
    Write export result to files.

    Args:
        export: MarkdownExport result.
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

    # Tool result files (separate compaction files from regular tool results)
    if export.tool_result_files:
        tool_results_dir = None
        for filename, content in sorted(export.tool_result_files.items()):
            if filename.startswith("compaction_"):
                # Compaction summaries go at root level
                result_path = output_dir / f"{filename}.txt"
            else:
                # Regular tool results go in tool_results/
                if tool_results_dir is None:
                    tool_results_dir = output_dir / "tool_results"
                    tool_results_dir.mkdir(parents=True, exist_ok=True)
                result_path = tool_results_dir / f"{filename}.txt"
            result_path.write_text(content, encoding="utf-8")
            written.append(result_path)

    return written


# CLI helper for quick testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m dev_agent_lens.export.markdown <session.jsonl> [output_dir]")
        sys.exit(1)

    session_file = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    export = export_session_to_markdown(session_file)
    files = export_to_files(export, output_dir)

    print(f"Exported {len(files)} files:")
    for f in files:
        print(f"  - {f}")

    print(f"\nStats: {export.stats}")
