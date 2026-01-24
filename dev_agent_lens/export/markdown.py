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


# Import shared utilities from markdown_renderer
from dev_agent_lens.export.markdown_renderer import (
    truncate,
    normalize_subagent_type,
)


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
    parallel_group_counter = 0  # Counter for parallel tool groups

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

                # Find matching tool call (skip Task tools - handled in subagent branch)
                tool_info = None
                tool_id = None
                for tid, info in list(pending_tools.items()):
                    if not info.get("result_received") and info.get("name") != "Task":
                        tool_info = info
                        tool_id = tid
                        break

                if tool_info:
                    tool_info["result_received"] = True
                    tool_info["result"] = result_str

                    parallel_group = tool_info.get("parallel_group")

                    if parallel_group:
                        # Check if all tools in this group have received results
                        group_tools = [
                            (tid, info) for tid, info in pending_tools.items()
                            if info.get("parallel_group") == parallel_group
                        ]
                        all_received = all(info.get("result_received") for _, info in group_tools)

                        if all_received:
                            # Emit parallel_tools event with all tools
                            tools_list = []
                            for tid, info in group_tools:
                                tools_list.append({
                                    "name": info.get("name", "Unknown"),
                                    "input": info.get("input", {}),
                                    "result": info.get("result", ""),
                                    "tool_use_id": tid,
                                })
                                stats["tool_calls"] += 1
                                del pending_tools[tid]

                            records.append({
                                "record_type": "event",
                                "event_type": "parallel_tools",
                                "order_index": order_index,
                                "tools": tools_list,
                                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                            })
                            order_index += 1
                    else:
                        # Single tool - emit immediately
                        records.append({
                            "record_type": "event",
                            "event_type": "tool",
                            "order_index": order_index,
                            "name": tool_info.get("name", "Unknown"),
                            "input": tool_info.get("input", {}),
                            "result": result_str,
                            "tool_use_id": tool_id,
                            "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                        })
                        order_index += 1
                        stats["tool_calls"] += 1
                        if tool_id:
                            del pending_tools[tool_id]

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
                # If multiple tools, assign to a parallel group
                group_id = None
                if len(msg.tool_uses) > 1:
                    parallel_group_counter += 1
                    group_id = parallel_group_counter

                for tool_use in msg.tool_uses:
                    tool_id = tool_use.get("id", "")
                    pending_tools[tool_id] = {
                        "name": tool_use.get("name"),
                        "input": tool_use.get("input", {}),
                        "result_received": False,
                        "parallel_group": group_id,
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
    # Fields at top level for compatibility with render_jsonl_to_markdown
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
        # Claude-specific fields at top level for shared renderer
        "project_path": cwd or "",
        "git_branch": git_branch,
        "summary": summary,
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

    Implements the AGREED_FORMAT.md specification using the shared renderer.
    This ensures both Claude and LiteLLM pipelines produce identical output format.

    Args:
        session_file: Path to the .jsonl session file.
        project_dir: Project directory containing subagent files. If None,
                     inferred from session_file location.

    Returns:
        MarkdownExport with main content, subagent files, and tool result files.
    """
    from dev_agent_lens.export.markdown_renderer import render_jsonl_to_markdown

    session_file = Path(session_file)

    if project_dir is None:
        project_dir = session_file.parent
    else:
        project_dir = Path(project_dir)

    # Convert Claude JSONL to common format
    records = export_session_to_jsonl(session_file, project_dir)

    if not records or len(records) <= 2:  # Only header and footer
        return MarkdownExport(
            main_content="# Empty Session\n\nNo messages found.\n",
            session_id="",
            stats={"user_turns": 0, "assistant_turns": 0, "tool_calls": 0, "subagents": 0},
        )

    # Use shared renderer with Claude pipeline
    result = render_jsonl_to_markdown(records, pipeline="claude")

    # Extract metadata from header for MarkdownExport
    header = next((r for r in records if r.get("record_type") == "header"), {})
    session_id = header.get("session_id", "")
    project_path = header.get("project_path", "")
    git_branch = header.get("git_branch")
    summary = header.get("summary", "")

    # Load subagent files from disk if they exist
    subagent_files = dict(result.subagent_files)  # Copy from renderer
    for filename in list(subagent_files.keys()):
        # Check if there's a real subagent JSONL file to load
        subagent_jsonl = project_dir / f"{filename}.jsonl"
        if subagent_jsonl.exists():
            try:
                subagent_export = export_session_to_markdown(subagent_jsonl, project_dir)
                subagent_files[filename] = subagent_export.main_content
                # Include nested subagent files
                for nested_name, nested_content in subagent_export.subagent_files.items():
                    subagent_files[nested_name] = nested_content
            except Exception:
                pass  # Keep the summary-only version from renderer

    return MarkdownExport(
        main_content=result.main_content,
        subagent_files=subagent_files,
        tool_result_files=result.tool_result_files,
        session_id=session_id,
        project_path=project_path,
        git_branch=git_branch,
        summary=summary,
        stats=result.stats,
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
