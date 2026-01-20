"""
Claude Code Session to Markdown Exporter

Converts raw Claude Code JSONL session files into readable markdown format.
Designed for LLM consumption with flat structure and reference-based subagent handling.

Design principles:
- Flat chronological format (linear pass, easy to follow)
- Reference mode for subagents (summary inline, full content in separate files)
- Preserve conversation flow while stripping metadata noise
- Truncate long tool outputs to prevent context bloat
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


@dataclass
class MarkdownExport:
    """Result of exporting a session to markdown."""

    main_content: str
    """Main session markdown content."""

    subagent_files: dict[str, str] = field(default_factory=dict)
    """Map of agent_id -> markdown content for referenced subagents."""

    session_id: str = ""
    """Session identifier."""

    project_path: str = ""
    """Original project path."""

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
    timestamp: datetime | None
    content: Any  # str or list of content blocks

    # Tool-related
    tool_use: dict | None = None  # {name, id, input}
    tool_result: dict | None = None  # {tool_use_id, content, is_error}
    tool_use_result: dict | None = None  # For subagent results with agentId

    # Metadata
    model: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    slug: str | None = None
    cwd: str | None = None
    git_branch: str | None = None

    # Raw for debugging
    raw: dict = field(default_factory=dict)


def parse_jsonl_file(file_path: str | Path) -> Iterator[dict]:
    """Parse a JSONL file, yielding each line as a dict."""
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                # Log but don't fail on bad lines
                pass


def parse_message(raw: dict) -> SessionMessage:
    """Parse a raw JSONL object into a SessionMessage."""
    msg_type = raw.get("type", "unknown")

    # Parse timestamp
    ts = raw.get("timestamp")
    timestamp = None
    if ts:
        try:
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # Extract content from message wrapper
    message = raw.get("message", {})
    content = message.get("content", "")

    # Handle assistant messages with tool_use
    tool_use = None
    tool_result = None

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_use":
                    tool_use = {
                        "name": block.get("name"),
                        "id": block.get("id"),
                        "input": block.get("input", {}),
                    }
                elif block.get("type") == "tool_result":
                    tool_result = {
                        "tool_use_id": block.get("tool_use_id"),
                        "content": block.get("content"),
                        "is_error": block.get("is_error", False),
                    }

    # Check for toolUseResult (subagent results have agentId, regular tools have string)
    tool_use_result_raw = raw.get("toolUseResult")
    tool_use_result = None

    if isinstance(tool_use_result_raw, dict) and tool_use_result_raw.get("agentId"):
        # This is a subagent result
        tool_use_result = tool_use_result_raw
    elif isinstance(tool_use_result_raw, str):
        # Regular tool result as string - use the tool_result from message.content instead
        # The string version is redundant with what's in message.content
        pass
    elif isinstance(tool_use_result_raw, dict):
        # Dict without agentId - might be a regular tool result format
        pass

    return SessionMessage(
        uuid=raw.get("uuid", ""),
        parent_uuid=raw.get("parentUuid"),
        type=msg_type,
        timestamp=timestamp,
        content=content,
        tool_use=tool_use,
        tool_result=tool_result,
        tool_use_result=tool_use_result,
        model=message.get("model"),
        session_id=raw.get("sessionId"),
        agent_id=raw.get("agentId"),
        slug=raw.get("slug"),
        cwd=raw.get("cwd"),
        git_branch=raw.get("gitBranch"),
        raw=raw,
    )


def extract_text_content(content: Any) -> str:
    """Extract plain text from message content."""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts)

    return str(content) if content else ""


def format_tool_call(tool_use: dict, max_input_length: int = 500) -> str:
    """Format a tool call for markdown display."""
    name = tool_use.get("name", "Unknown")
    tool_input = tool_use.get("input", {})

    # Handle common tool types specially
    if name == "Read":
        file_path = tool_input.get("file_path", "")
        return f"**Tool (Read)**: `{file_path}`"

    elif name == "Write":
        file_path = tool_input.get("file_path", "")
        return f"**Tool (Write)**: `{file_path}`"

    elif name == "Edit":
        file_path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")[:50]
        return f"**Tool (Edit)**: `{file_path}`"

    elif name == "Bash":
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            return f"**Tool (Bash)**: {desc}\n```bash\n{cmd}\n```"
        return f"**Tool (Bash)**:\n```bash\n{cmd}\n```"

    elif name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"**Tool (Glob)**: `{pattern}` in `{path or '.'}`"

    elif name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"**Tool (Grep)**: `{pattern}` in `{path or '.'}`"

    elif name == "Task":
        subagent_type = tool_input.get("subagent_type", "")
        description = tool_input.get("description", "")
        prompt = tool_input.get("prompt", "")[:200]
        return f"**Tool (Task/{subagent_type})**: {description}\n> {prompt}..."

    elif name == "TodoWrite":
        todos = tool_input.get("todos", [])
        items = []
        for t in todos[:5]:  # Limit to first 5
            status = t.get("status", "pending")
            content = t.get("content", "")
            items.append(f"  - [{status}] {content}")
        return "**Tool (TodoWrite)**:\n" + "\n".join(items)

    elif name.startswith("mcp__"):
        # MCP tool call
        return f"**Tool ({name})**"

    else:
        # Generic tool
        input_str = json.dumps(tool_input, indent=2)
        if len(input_str) > max_input_length:
            input_str = input_str[:max_input_length] + "..."
        return f"**Tool ({name})**:\n```json\n{input_str}\n```"


def format_tool_call_brief(tool_use: dict) -> str:
    """Format a tool call briefly for parallel group display (single line)."""
    name = tool_use.get("name", "Unknown")
    tool_input = tool_use.get("input", {})

    if name == "Read":
        file_path = tool_input.get("file_path", "")
        # Shorten path if too long
        if len(file_path) > 60:
            file_path = "..." + file_path[-57:]
        return f"Read: `{file_path}`"

    elif name == "Write":
        file_path = tool_input.get("file_path", "")
        if len(file_path) > 60:
            file_path = "..." + file_path[-57:]
        return f"Write: `{file_path}`"

    elif name == "Edit":
        file_path = tool_input.get("file_path", "")
        if len(file_path) > 60:
            file_path = "..." + file_path[-57:]
        return f"Edit: `{file_path}`"

    elif name == "Bash":
        desc = tool_input.get("description", "")
        cmd = tool_input.get("command", "")
        if desc:
            return f"Bash: {desc[:50]}"
        return f"Bash: `{cmd[:50]}{'...' if len(cmd) > 50 else ''}`"

    elif name == "Glob":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"Glob: `{pattern}` in `{path or '.'}`"

    elif name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f"Grep: `{pattern}` in `{path or '.'}`"

    elif name == "Task":
        subagent_type = tool_input.get("subagent_type", "")
        description = tool_input.get("description", "")
        return f"Task/{subagent_type}: {description[:50]}"

    elif name == "TodoWrite":
        todos = tool_input.get("todos", [])
        return f"TodoWrite: {len(todos)} item(s)"

    elif name.startswith("mcp__"):
        # MCP tool call - extract the tool name after mcp__server__
        parts = name.split("__")
        if len(parts) >= 3:
            return f"MCP: {parts[-1]}"
        return f"MCP: {name}"

    else:
        return f"{name}"


def format_tool_result(
    tool_result: dict | None,
    tool_use_result: dict | None,
    max_length: int = 1000,
    label: str = ""
) -> str:
    """Format tool result for markdown display.

    Args:
        tool_result: Regular tool result dict.
        tool_use_result: Subagent result dict (with agentId).
        max_length: Maximum content length before truncation.
        label: Optional label like "[1a]" for parallel tool results.
    """
    label_prefix = f"{label} " if label else ""

    if tool_use_result:
        # This is a subagent result
        agent_id = tool_use_result.get("agentId", "")
        status = tool_use_result.get("status", "")
        content = tool_use_result.get("content", [])
        duration = tool_use_result.get("totalDurationMs", 0)
        tokens = tool_use_result.get("totalTokens", 0)

        # Extract text from content
        text = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break
        elif isinstance(content, str):
            text = content

        if len(text) > max_length:
            text = text[:max_length] + "..."

        duration_sec = duration / 1000 if duration else 0
        return (
            f"→ {label_prefix}**Subagent Result** (agent-{agent_id}): {status}\n"
            f"  Duration: {duration_sec:.1f}s | Tokens: {tokens:,}\n"
            f"  See: `agent-{agent_id}.md`\n\n"
            f"  **Summary**:\n{_indent(text, '  ')}"
        )

    if tool_result:
        is_error = tool_result.get("is_error", False)
        content = tool_result.get("content", "")

        # Extract text
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            content = "\n".join(texts)

        if len(content) > max_length:
            content = content[:max_length] + "\n... (truncated)"

        if is_error:
            return f"→ {label_prefix}**Error**:\n```\n{content}\n```"
        else:
            return f"→ {label_prefix}**Result**:\n{content}"

    return ""


def _indent(text: str, prefix: str = "  ") -> str:
    """Indent all lines of text."""
    return "\n".join(prefix + line for line in text.split("\n"))


def export_session_to_markdown(
    session_file: str | Path,
    project_dir: str | Path | None = None,
    max_tool_result_length: int = 1000,
    include_file_snapshots: bool = False,
    include_timestamps: bool = True,
) -> MarkdownExport:
    """
    Export a Claude Code session JSONL file to markdown.

    Args:
        session_file: Path to the .jsonl session file.
        project_dir: Project directory containing subagent files. If None,
                     inferred from session_file location.
        max_tool_result_length: Maximum length for tool result content.
        include_file_snapshots: Whether to include file-history-snapshot entries.
        include_timestamps: Whether to include timestamps on messages.

    Returns:
        MarkdownExport with main content and referenced subagent files.
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
        return MarkdownExport(main_content="# Empty Session\n\nNo messages found.")

    # Extract session metadata from first message
    first_msg = messages[0]
    session_id = session_file.stem  # UUID from filename
    project_path = first_msg.cwd or str(project_dir)
    git_branch = first_msg.git_branch

    # Find summary if exists
    summary = ""
    for msg in messages:
        if msg.type == "summary":
            summary_content = msg.raw.get("summary", "")
            if isinstance(summary_content, str):
                summary = summary_content
            break

    # Build markdown
    lines: list[str] = []

    # Header
    lines.append(f"# Session: {session_id[:8]}...")
    lines.append("")
    lines.append(f"**Project**: `{project_path}`")
    if git_branch:
        lines.append(f"**Branch**: `{git_branch}`")

    # Timestamps
    timestamps = [m.timestamp for m in messages if m.timestamp]
    if timestamps:
        start = min(timestamps)
        end = max(timestamps)
        lines.append(f"**Date**: {start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%H:%M')}")

    if summary:
        lines.append(f"**Summary**: {summary}")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Stats tracking
    stats = {
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "subagent_calls": 0,
        "parallel_tool_groups": 0,
        "errors": 0,
    }

    # Track subagents we need to export
    subagent_ids: set[str] = set()

    # First pass: identify parallel tool call groups
    # A parallel group is consecutive assistant messages with tool_use within 2 seconds
    parallel_groups: dict[int, list[int]] = {}  # start_index -> [indices]
    tool_id_to_label: dict[str, str] = {}  # tool_use_id -> label like "[1a]"

    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.type == "assistant" and msg.tool_use:
            # Start of potential parallel group
            group_start = i
            group_indices = [i]
            group_start_time = msg.timestamp

            # Look ahead for more tool calls within 2 seconds
            j = i + 1
            while j < len(messages):
                next_msg = messages[j]
                if next_msg.type == "assistant" and next_msg.tool_use:
                    # Check if within 2 seconds of group start
                    if group_start_time and next_msg.timestamp:
                        time_diff = abs((next_msg.timestamp - group_start_time).total_seconds())
                        if time_diff <= 2:
                            group_indices.append(j)
                            j += 1
                            continue
                    elif not group_start_time or not next_msg.timestamp:
                        # No timestamps, assume sequential - check parent chain
                        # If this tool's parent is the previous tool, they're parallel
                        prev_msg = messages[group_indices[-1]]
                        if next_msg.parent_uuid == prev_msg.uuid:
                            group_indices.append(j)
                            j += 1
                            continue
                break

            # Only mark as parallel if 2+ tools
            if len(group_indices) >= 2:
                parallel_groups[group_start] = group_indices
                # Assign labels [1a], [1b], [1c], etc. for this group
                group_num = stats["parallel_tool_groups"] + 1
                for label_idx, idx in enumerate(group_indices):
                    tool_id = messages[idx].tool_use.get("id")
                    if tool_id:
                        label_letter = chr(ord('a') + label_idx)
                        tool_id_to_label[tool_id] = f"[{group_num}{label_letter}]"
                stats["parallel_tool_groups"] += 1
                i = j  # Skip past the group
            else:
                i += 1
        else:
            i += 1

    # Second pass: process messages with parallel group awareness
    processed_indices: set[int] = set()

    for i, msg in enumerate(messages):
        if i in processed_indices:
            continue

        # Skip certain types
        if msg.type == "file-history-snapshot" and not include_file_snapshots:
            continue
        if msg.type == "summary":
            continue  # Already in header

        # User message
        if msg.type == "user":
            # Check if this is a tool result
            if msg.tool_result or msg.tool_use_result:
                # Check if this result belongs to a parallel tool call
                result_label = ""
                if msg.tool_result:
                    tool_use_id = msg.tool_result.get("tool_use_id", "")
                    if tool_use_id in tool_id_to_label:
                        result_label = tool_id_to_label[tool_use_id]

                result_text = format_tool_result(
                    msg.tool_result,
                    msg.tool_use_result,
                    max_tool_result_length,
                    label=result_label
                )
                if result_text:
                    lines.append(result_text)
                    lines.append("")

                # Track subagent
                if msg.tool_use_result and msg.tool_use_result.get("agentId"):
                    subagent_ids.add(msg.tool_use_result["agentId"])
                    stats["subagent_calls"] += 1

                if msg.tool_result and msg.tool_result.get("is_error"):
                    stats["errors"] += 1
            else:
                # Regular user message
                content = extract_text_content(msg.content)
                if content and content.strip():
                    timestamp_str = ""
                    if include_timestamps and msg.timestamp:
                        timestamp_str = f" ({msg.timestamp.strftime('%H:%M')})"

                    lines.append(f"**User**{timestamp_str}: {content}")
                    lines.append("")
                    stats["user_turns"] += 1

        # Assistant message
        elif msg.type == "assistant":
            # Check if this is the start of a parallel group
            if i in parallel_groups:
                group_indices = parallel_groups[i]
                timestamp_str = ""
                if include_timestamps and msg.timestamp:
                    timestamp_str = f" ({msg.timestamp.strftime('%H:%M')})"

                lines.append(f"**Parallel Tools**{timestamp_str}:")
                for idx in group_indices:
                    tool_msg = messages[idx]
                    if tool_msg.tool_use:
                        tool_id = tool_msg.tool_use.get("id", "")
                        label = tool_id_to_label.get(tool_id, "")
                        tool_text = format_tool_call_brief(tool_msg.tool_use)
                        lines.append(f"  - {label} {tool_text}")
                        stats["tool_calls"] += 1
                        if tool_msg.tool_use.get("name") == "Task":
                            stats["subagent_calls"] += 1
                    processed_indices.add(idx)
                lines.append("")
            elif i not in processed_indices:
                # Single tool call (not part of parallel group)
                if msg.tool_use:
                    tool_text = format_tool_call(msg.tool_use)
                    lines.append(tool_text)
                    lines.append("")
                    stats["tool_calls"] += 1

                    # Check if it's a Task (subagent)
                    if msg.tool_use.get("name") == "Task":
                        stats["subagent_calls"] += 1

                # Text content
                content = extract_text_content(msg.content)
                if content and content.strip():
                    timestamp_str = ""
                    if include_timestamps and msg.timestamp:
                        timestamp_str = f" ({msg.timestamp.strftime('%H:%M')})"

                    lines.append(f"**Assistant**{timestamp_str}: {content}")
                    lines.append("")
                    stats["assistant_turns"] += 1

        # System message
        elif msg.type == "system":
            content = extract_text_content(msg.content)
            if content and content.strip():
                lines.append(f"**System**: {content}")
                lines.append("")

    # Export referenced subagent files
    subagent_files: dict[str, str] = {}
    for agent_id in subagent_ids:
        agent_file = project_dir / f"agent-{agent_id}.jsonl"
        if agent_file.exists():
            try:
                agent_export = export_session_to_markdown(
                    agent_file,
                    project_dir,
                    max_tool_result_length,
                    include_file_snapshots,
                    include_timestamps,
                )
                subagent_files[agent_id] = agent_export.main_content
                # Recursively include nested subagents
                subagent_files.update(agent_export.subagent_files)
            except Exception as e:
                subagent_files[agent_id] = f"# Error loading agent-{agent_id}\n\n{e}"

    return MarkdownExport(
        main_content="\n".join(lines),
        subagent_files=subagent_files,
        session_id=session_id,
        project_path=project_path,
        summary=summary,
        stats=stats,
    )


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
    for agent_id, content in export.subagent_files.items():
        agent_path = output_dir / f"agent-{agent_id}.md"
        agent_path.write_text(content, encoding="utf-8")
        written.append(agent_path)

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
