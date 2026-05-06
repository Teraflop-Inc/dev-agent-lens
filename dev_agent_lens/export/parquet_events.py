"""
Events Parquet Export for Claude Sessions.

Converts Claude Code JSONL sessions to an events-based Parquet format
optimized for DuckDB analytics queries.

The events format preserves conversation flow with explicit order_idx,
making it easy to answer questions like "What tool was called after
the user asked about X?"
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Optional pyarrow import
try:
    import pyarrow as pa
    import pyarrow.parquet as pq

    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False
    pa = None  # type: ignore
    pq = None  # type: ignore


def _get_events_schema() -> "pa.Schema":
    """Get the events Parquet schema.

    Returns:
        PyArrow schema for events table.

    Raises:
        ImportError: If pyarrow is not installed.
    """
    if not HAS_PYARROW:
        raise ImportError(
            "pyarrow is required for Parquet export. Install with: uv add pyarrow"
        )

    return pa.schema(
        [
            ("session_id", pa.string()),
            ("event_id", pa.string()),
            ("parent_event_id", pa.string()),
            ("order_idx", pa.int32()),
            ("timestamp", pa.timestamp("us", tz="UTC")),
            ("event_type", pa.string()),  # user, assistant, tool, subagent, compaction
            # Content fields (nullable based on event_type)
            ("text", pa.string()),
            ("tool_name", pa.string()),
            ("tool_input", pa.string()),
            ("tool_result", pa.string()),
            ("subagent_type", pa.string()),
            # Claude-specific metadata
            ("project_path", pa.string()),
            ("git_branch", pa.string()),
            # Raw data for debugging
            ("raw_message_json", pa.string()),
        ]
    )


# Get schema lazily to avoid import-time error
EVENTS_SCHEMA = None


def get_events_schema() -> "pa.Schema":
    """Get the events schema, initializing lazily."""
    global EVENTS_SCHEMA
    if EVENTS_SCHEMA is None:
        EVENTS_SCHEMA = _get_events_schema()
    return EVENTS_SCHEMA


@dataclass
class ExportResult:
    """Result of exporting to Parquet."""

    output_path: Path
    """Path to the output Parquet file."""

    event_count: int
    """Number of events written."""

    session_count: int
    """Number of sessions processed."""

    bytes_written: int
    """Size of output file in bytes."""

    event_type_counts: dict[str, int]
    """Count of events by type."""


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _parse_jsonl_file(file_path: Path) -> Iterator[dict[str, Any]]:
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


def _extract_text_content(content: Any) -> str:
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


def _extract_subagent_response_text(tool_use_result: dict) -> str:
    """Extract text content from subagent toolUseResult."""
    content = tool_use_result.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def extract_events_from_session(
    session_file: Path,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Extract events from a Claude Code session JSONL file.

    Args:
        session_file: Path to the .jsonl session file.
        session_id: Session ID to use. If None, uses the filename stem.

    Returns:
        List of event dictionaries ready for Parquet export.
    """
    session_file = Path(session_file)
    if session_id is None:
        session_id = session_file.stem

    events: list[dict[str, Any]] = []
    order_idx = 0

    # Session metadata (extracted from first messages that have it)
    project_path: str | None = None
    git_branch: str | None = None

    # Parse all messages
    messages = list(_parse_jsonl_file(session_file))

    # Track pending tool calls (tool_use_id -> tool info)
    pending_tools: dict[str, dict] = {}

    # First pass: extract session metadata
    for msg in messages:
        if project_path is None and msg.get("cwd"):
            project_path = msg["cwd"]
        if git_branch is None and msg.get("gitBranch"):
            git_branch = msg["gitBranch"]
        if project_path and git_branch:
            break

    # Second pass: extract events
    i = 0
    while i < len(messages):
        msg = messages[i]
        msg_type = msg.get("type", "unknown")
        msg_subtype = msg.get("subtype")
        message = msg.get("message", {})
        content = message.get("content", "")
        timestamp = _parse_timestamp(msg.get("timestamp"))

        # Skip non-conversation types
        if msg_type in ("summary", "file-history-snapshot"):
            i += 1
            continue

        # Handle compaction boundary
        if msg_type == "system" and msg_subtype == "compact_boundary":
            compact_metadata = msg.get("compactMetadata", {})

            # Get summary from next message if it's a user continuation
            summary_text = ""
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if next_msg.get("type") == "user":
                    next_content = next_msg.get("message", {}).get("content", "")
                    summary_text = _extract_text_content(next_content)

            events.append({
                "session_id": session_id,
                "event_id": str(uuid.uuid4()),
                "parent_event_id": None,
                "order_idx": order_idx,
                "timestamp": timestamp,
                "event_type": "compaction",
                "text": summary_text,
                "tool_name": None,
                "tool_input": json.dumps({
                    "trigger": compact_metadata.get("trigger", "unknown"),
                    "pre_tokens": compact_metadata.get("preTokens", 0),
                }),
                "tool_result": None,
                "subagent_type": None,
                "project_path": project_path,
                "git_branch": git_branch,
                "raw_message_json": json.dumps(msg),
            })
            order_idx += 1

            # Skip the continuation summary message
            if i + 1 < len(messages) and messages[i + 1].get("type") == "user":
                i += 2
            else:
                i += 1
            continue

        # User message with tool result
        if msg_type == "user" and msg.get("toolUseResult") is not None:
            result = msg["toolUseResult"]

            # Check if subagent result
            if isinstance(result, dict) and result.get("agentId"):
                # Extract tool_use_id from message content
                tool_use_id = None
                msg_content = msg.get("message", {}).get("content", [])
                if isinstance(msg_content, list):
                    for block in msg_content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id")
                            break

                # Look up Task tool by tool_use_id
                task_info = None
                parent_event_id = None
                if tool_use_id and tool_use_id in pending_tools:
                    task_info = pending_tools[tool_use_id]
                    parent_event_id = task_info.get("parent_event_id")
                    del pending_tools[tool_use_id]

                if task_info:
                    subagent_type = task_info["input"].get("subagent_type", "unknown")
                    task_prompt = task_info["input"].get("prompt", "")
                else:
                    subagent_type = "unknown"
                    task_prompt = result.get("prompt", "")

                response_text = _extract_subagent_response_text(result)

                events.append({
                    "session_id": session_id,
                    "event_id": str(uuid.uuid4()),
                    "parent_event_id": parent_event_id,
                    "order_idx": order_idx,
                    "timestamp": timestamp,
                    "event_type": "subagent",
                    "text": response_text,
                    "tool_name": "Task",
                    "tool_input": task_prompt,
                    "tool_result": None,
                    "subagent_type": subagent_type,
                    "project_path": project_path,
                    "git_branch": git_branch,
                    "raw_message_json": json.dumps(msg),
                })
                order_idx += 1

            else:
                # Regular tool result
                if result is None:
                    result_str = ""
                elif isinstance(result, str):
                    result_str = result
                else:
                    result_str = json.dumps(result)

                # Extract tool_use_id from message content for proper matching
                tool_use_id = None
                msg_content = msg.get("message", {}).get("content", [])
                if isinstance(msg_content, list):
                    for block in msg_content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id")
                            break

                # Look up tool by tool_use_id (not FIFO)
                tool_info = None
                if tool_use_id and tool_use_id in pending_tools:
                    tool_info = pending_tools[tool_use_id]

                if tool_info:
                    # Emit tool event with merged call and result
                    events.append({
                        "session_id": session_id,
                        "event_id": str(uuid.uuid4()),
                        "parent_event_id": tool_info.get("parent_event_id"),
                        "order_idx": order_idx,
                        "timestamp": timestamp,
                        "event_type": "tool",
                        "text": None,
                        "tool_name": tool_info.get("name", "Unknown"),
                        "tool_input": json.dumps(tool_info.get("input", {})),
                        "tool_result": result_str,
                        "subagent_type": None,
                        "project_path": project_path,
                        "git_branch": git_branch,
                        "raw_message_json": json.dumps(msg),
                    })
                    order_idx += 1

                    del pending_tools[tool_use_id]

            i += 1
            continue

        # Regular user message
        if msg_type == "user":
            text_content = _extract_text_content(content)
            if text_content and text_content.strip():
                events.append({
                    "session_id": session_id,
                    "event_id": str(uuid.uuid4()),
                    "parent_event_id": None,
                    "order_idx": order_idx,
                    "timestamp": timestamp,
                    "event_type": "user",
                    "text": text_content,
                    "tool_name": None,
                    "tool_input": None,
                    "tool_result": None,
                    "subagent_type": None,
                    "project_path": project_path,
                    "git_branch": git_branch,
                    "raw_message_json": json.dumps(msg),
                })
                order_idx += 1

            i += 1
            continue

        # Assistant message
        if msg_type == "assistant":
            # Generate event_id for this assistant message (used as parent for tool events)
            assistant_event_id = str(uuid.uuid4())

            # Register tool uses for later matching with results
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id", "")
                        pending_tools[tool_id] = {
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                            "parent_event_id": assistant_event_id,
                        }

            # Extract text content from assistant
            text_content = _extract_text_content(content)
            if text_content and text_content.strip():
                events.append({
                    "session_id": session_id,
                    "event_id": assistant_event_id,
                    "parent_event_id": None,
                    "order_idx": order_idx,
                    "timestamp": timestamp,
                    "event_type": "assistant",
                    "text": text_content,
                    "tool_name": None,
                    "tool_input": None,
                    "tool_result": None,
                    "subagent_type": None,
                    "project_path": project_path,
                    "git_branch": git_branch,
                    "raw_message_json": json.dumps(msg),
                })
                order_idx += 1

            i += 1
            continue

        i += 1

    return events


def export_claude_to_events_parquet(
    session_file: Path | str | None = None,
    session_files: list[Path | str] | None = None,
    output_path: Path | str | None = None,
    session_id: str | None = None,
    compression: str = "zstd",
) -> ExportResult:
    """
    Export Claude sessions to events Parquet format.

    Can export a single session or multiple sessions to a single Parquet file.

    Args:
        session_file: Path to a single session JSONL file.
        session_files: List of session JSONL files to export together.
        output_path: Path for output Parquet file. If None, uses session_file
            with .parquet extension.
        session_id: Override session ID (only for single file export).
        compression: Parquet compression codec (zstd, snappy, gzip, none).

    Returns:
        ExportResult with statistics about the export.

    Raises:
        ImportError: If pyarrow is not installed.
        ValueError: If neither session_file nor session_files is provided.
    """
    if not HAS_PYARROW:
        raise ImportError(
            "pyarrow is required for Parquet export. Install with: uv add pyarrow"
        )

    # Handle input files
    if session_file is not None:
        files = [Path(session_file)]
    elif session_files is not None:
        files = [Path(f) for f in session_files]
    else:
        raise ValueError("Either session_file or session_files must be provided")

    # Determine output path
    if output_path is None:
        if len(files) == 1:
            output_path = files[0].with_suffix(".parquet")
        else:
            output_path = Path("events.parquet")
    else:
        output_path = Path(output_path)

    # Ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect all events
    all_events: list[dict[str, Any]] = []
    session_count = 0
    event_type_counts: dict[str, int] = {}

    for file in files:
        file = Path(file)
        if not file.exists():
            continue

        sid = session_id if len(files) == 1 and session_id else None
        events = extract_events_from_session(file, session_id=sid)

        all_events.extend(events)
        session_count += 1

        # Count event types
        for event in events:
            event_type = event.get("event_type", "unknown")
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1

    if not all_events:
        # Create empty Parquet file with schema
        schema = get_events_schema()
        table = pa.Table.from_pylist([], schema=schema)
        pq.write_table(
            table,
            output_path,
            compression=compression if compression != "none" else None,
        )
        return ExportResult(
            output_path=output_path,
            event_count=0,
            session_count=0,
            bytes_written=output_path.stat().st_size,
            event_type_counts={},
        )

    # Write to Parquet
    schema = get_events_schema()
    table = pa.Table.from_pylist(all_events, schema=schema)
    pq.write_table(
        table,
        output_path,
        compression=compression if compression != "none" else None,
    )

    return ExportResult(
        output_path=output_path,
        event_count=len(all_events),
        session_count=session_count,
        bytes_written=output_path.stat().st_size,
        event_type_counts=event_type_counts,
    )


def export_claude_sessions_to_events_parquet(
    claude_dir: Path | str | None = None,
    output_path: Path | str | None = None,
    session_ids: list[str] | None = None,
    limit: int | None = None,
    compression: str = "zstd",
) -> ExportResult:
    """
    Export Claude sessions from the sessions directory to events Parquet.

    Args:
        claude_dir: Claude sessions directory. Defaults to ~/.claude/projects.
        output_path: Path for output Parquet file.
        session_ids: Filter to specific session IDs.
        limit: Maximum number of sessions to export.
        compression: Parquet compression codec.

    Returns:
        ExportResult with statistics.
    """
    from dev_agent_lens.clients.claude import ClaudeClient

    client = ClaudeClient(claude_dir=claude_dir)
    sessions = client.list_sessions(limit=limit)

    # Filter by session IDs if specified
    if session_ids:
        sessions = [s for s in sessions if s.session_id in session_ids]

    session_files = [s.file_path for s in sessions]

    if output_path is None:
        output_path = Path("claude_events.parquet")

    return export_claude_to_events_parquet(
        session_files=session_files,
        output_path=output_path,
        compression=compression,
    )
