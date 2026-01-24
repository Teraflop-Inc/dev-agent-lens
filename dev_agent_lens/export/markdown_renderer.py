"""
Markdown Renderer for JSONL Conversation Records

Converts JSONL conversation records (from export_chain_to_jsonl) into markdown format
matching AGREED_FORMAT.md specification.

This module is the second half of a two-stage pipeline:
1. export_chain_to_jsonl() -> JSONL records with conversation events
2. render_jsonl_to_markdown() -> Markdown output (this module)

The separation allows:
- JSONL to be the canonical, queryable intermediate format
- Markdown to be generated deterministically from JSONL
- Different renderers for different output formats (markdown, HTML, etc.)

Design principles:
- Deterministic output (same JSONL always produces identical markdown)
- PIPELINE_SPECIFIC sections for LiteLLM-only fields (tokens, models, compaction)
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
    """Result of rendering JSONL records to markdown."""

    main_content: str
    """Main session markdown content."""

    subagent_files: dict[str, str] = field(default_factory=dict)
    """Map of subagent filename (without .md) -> markdown content."""

    tool_result_files: dict[str, str] = field(default_factory=dict)
    """Map of tool result filename -> content for large results."""

    session_id: str = ""
    """Session identifier (Claude session UUID from metadata)."""

    stats: dict[str, Any] = field(default_factory=dict)
    """Export statistics (turns, tools, subagents, etc.)."""


# =============================================================================
# Helper Functions
# =============================================================================


def truncate(text: str, limit: int) -> str:
    """Truncate text to limit chars, showing (limit-3) + '...' if exceeded."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


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


def format_timestamp(dt: datetime | str | None) -> str:
    """Format datetime as 'YYYY-MM-DD HH:MM:SS UTC'."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        # Parse ISO format
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
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


def normalize_subagent_type(subagent_type: str) -> str:
    """Normalize subagent type for filename: lowercase, replace - and spaces with _."""
    return subagent_type.lower().replace("-", "_").replace(" ", "_")


# =============================================================================
# Tool Section Rendering
# =============================================================================


def _render_parallel_tools_section(
    tools: list[dict],
    tool_result_files: dict[str, str],
    tool_result_sequence: int,
) -> tuple[list[str], int]:
    """
    Render a parallel tools section to markdown lines per AGREED_FORMAT.md.

    Args:
        tools: List of tool dicts with name, input, result
        tool_result_files: Dict to store large result files
        tool_result_sequence: Counter for result file naming

    Returns:
        Tuple of (markdown_lines, updated_tool_result_sequence)
    """
    lines: list[str] = []
    count = len(tools)

    lines.append(f"### Parallel Tools ({count} calls)")
    lines.append("")

    # Summary table
    lines.append("| # | Tool | Target |")
    lines.append("|---|------|--------|")
    for i, tool in enumerate(tools, 1):
        name = tool.get("name", "Unknown")
        target = get_tool_target_brief(name, tool.get("input", {}))
        lines.append(f"| {i} | {name} | {target} |")
    lines.append("")

    # Individual results
    for i, tool in enumerate(tools, 1):
        name = tool.get("name", "Unknown")
        result = tool.get("result", "")
        tool_input = tool.get("input", {})

        lines.append(f"**[{i}]** {name}")
        lines.append("")

        # Show input
        input_str = format_tool_input(tool_input)
        if input_str:
            lines.append("**Input**:")
            lines.append("```text")
            lines.append(input_str)
            lines.append("```")
            lines.append("")

        # Show result
        if result:
            result_len = len(result)
            lang = "text"
            if name == "Read":
                file_path = tool_input.get("file_path", "")
                lang = get_language_hint(file_path)
            elif name == "Bash":
                lang = "bash"

            if result_len > TOOL_RESULT_FILE_THRESHOLD:
                tool_result_sequence += 1
                filename = f"tool_result_{tool_result_sequence}.txt"
                tool_result_files[filename] = result

                lines.append(f"**Result** ({result_len:,} chars):")
                lines.append(f"```{lang}")
                lines.append(truncate(result, TOOL_RESULT_INLINE_LIMIT))
                lines.append("```")
                lines.append("")
                lines.append(f"→ Full result: [{filename}](./{filename})")
            elif result_len > TOOL_RESULT_INLINE_LIMIT:
                lines.append(f"**Result** ({result_len:,} chars):")
                lines.append(f"```{lang}")
                lines.append(truncate(result, TOOL_RESULT_INLINE_LIMIT))
                lines.append("```")
            else:
                lines.append(f"**Result** ({result_len:,} chars):")
                lines.append(f"```{lang}")
                lines.append(result)
                lines.append("```")
        else:
            lines.append("**Result**: (empty)")
        lines.append("")

    lines.append("---")
    lines.append("")

    return lines, tool_result_sequence


def _render_tool_section(
    tool_name: str,
    tool_input: dict,
    result: str,
    tool_result_files: dict[str, str],
    tool_result_sequence: int,
) -> tuple[list[str], int]:
    """
    Render a tool section to markdown lines per AGREED_FORMAT.md.

    Returns:
        Tuple of (markdown_lines, updated_tool_result_sequence)
    """
    lines: list[str] = []

    lines.append(f"### Tool: {tool_name}")
    lines.append("")

    # Format input - per spec: **Input**: with ```text
    input_str = format_tool_input(tool_input)
    if input_str:
        lines.append("**Input**:")
        lines.append("```text")
        lines.append(input_str)
        lines.append("```")
        lines.append("")

    # Format result - per spec: **Result** (N chars): with language hint
    if result:
        result_len = len(result)
        # Determine language hint
        lang = "text"
        if tool_name == "Read":
            file_path = tool_input.get("file_path", "")
            lang = get_language_hint(file_path)
        elif tool_name == "Bash":
            lang = "bash"

        if result_len > TOOL_RESULT_FILE_THRESHOLD:
            # Create external file
            tool_result_sequence += 1
            filename = f"tool_result_{tool_result_sequence}.txt"
            tool_result_files[filename] = result

            # Show truncated inline + link to full file
            lines.append(f"**Result** ({result_len:,} chars):")
            lines.append(f"```{lang}")
            lines.append(truncate(result, TOOL_RESULT_INLINE_LIMIT))
            lines.append("```")
            lines.append("")
            lines.append(f"→ Full result: [{filename}](./{filename})")
        elif result_len > TOOL_RESULT_INLINE_LIMIT:
            # Truncate inline
            lines.append(f"**Result** ({result_len:,} chars):")
            lines.append(f"```{lang}")
            lines.append(truncate(result, TOOL_RESULT_INLINE_LIMIT))
            lines.append("```")
        else:
            # Full inline
            lines.append(f"**Result** ({result_len:,} chars):")
            lines.append(f"```{lang}")
            lines.append(result)
            lines.append("```")
    else:
        lines.append("**Result**: (empty)")

    lines.append("")
    lines.append("---")
    lines.append("")

    return lines, tool_result_sequence


# =============================================================================
# Subagent Section Rendering
# =============================================================================


def _render_subagent_section(
    subagent_type: str,
    description: str,
    prompt: str,
    response: str,
    filename: str,
) -> list[str]:
    """Render a subagent section to markdown lines per AGREED_FORMAT.md."""
    lines: list[str] = []

    lines.append(f"### Subagent: {subagent_type}")
    lines.append("")

    if description:
        lines.append(f"**Task**: {description}")

    # Prompt preview - per spec: **Prompt** (first 200 chars):
    if prompt:
        prompt_preview = truncate(prompt, SUBAGENT_PROMPT_PREVIEW_LIMIT)
        if len(prompt) > SUBAGENT_PROMPT_PREVIEW_LIMIT:
            lines.append(f"**Prompt** (first {SUBAGENT_PROMPT_PREVIEW_LIMIT} chars):")
        else:
            lines.append("**Prompt**:")
        lines.append(f"> {prompt_preview}")
        lines.append("")

    # Result summary - per spec: **Result Summary** (first 500 chars):
    if response:
        response_summary = truncate(response, SUBAGENT_RESPONSE_SUMMARY_LIMIT)
        if len(response) > SUBAGENT_RESPONSE_SUMMARY_LIMIT:
            lines.append(f"**Result Summary** (first {SUBAGENT_RESPONSE_SUMMARY_LIMIT} chars):")
        else:
            lines.append("**Result Summary**:")
        lines.append(f"> {response_summary}")
        lines.append("")

    # Per spec: → Full conversation: [filename](./filename)
    lines.append(f"→ Full conversation: [{filename}.md](./{filename}.md)")
    lines.append("")
    lines.append("---")
    lines.append("")

    return lines


# =============================================================================
# Compaction Section Rendering
# =============================================================================


COMPACTION_SUMMARY_INLINE_LIMIT = 500  # Truncate summaries longer than this


def _render_compaction_section(
    number: int,
    summary: str,
    pipeline: str = "litellm",
    pre_compaction_tokens: int | None = None,
    trigger: str | None = None,
    tool_result_files: dict[str, str] | None = None,
) -> list[str]:
    """Render a compaction section to markdown lines per AGREED_FORMAT.md."""
    lines: list[str] = []

    lines.append(f"### Compaction #{number}")
    lines.append("")

    # Pipeline-specific metadata
    lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
    if pipeline == "claude":
        if trigger:
            lines.append(f"- **Trigger**: {trigger} (Claude only)")
        if pre_compaction_tokens:
            lines.append(f"- **Pre-compaction tokens**: {pre_compaction_tokens:,} (Claude only)")
    else:
        # LiteLLM pipeline - detected from traces
        lines.append("- **Detected from**: LiteLLM traces (LiteLLM only)")
    lines.append("<!-- END PIPELINE_SPECIFIC -->")
    lines.append("")

    # Context summary (outside PIPELINE_SPECIFIC per spec)
    # Truncate long summaries and create external file
    if len(summary) > COMPACTION_SUMMARY_INLINE_LIMIT and tool_result_files is not None:
        # Store full summary in external file
        filename = f"compaction_{number}_summary"
        tool_result_files[filename] = summary

        # Show truncated inline with link
        truncated = truncate(summary, COMPACTION_SUMMARY_INLINE_LIMIT)
        lines.append("> **Context Summary**:")
        for line in truncated.split("\n"):
            for subline in line.replace("\\n", "\n").split("\n"):
                lines.append(f"> {subline}")
        lines.append("")
        lines.append(f"→ Full summary: [{filename}.txt](./{filename}.txt)")
    else:
        lines.append("> **Context Summary**:")
        for line in summary.split("\n"):
            # Handle literal \n in the string
            for subline in line.replace("\\n", "\n").split("\n"):
                lines.append(f"> {subline}")
    lines.append("")

    lines.append("---")
    lines.append("")

    return lines


# =============================================================================
# Subagent File Rendering
# =============================================================================


def _render_subagent_file(
    subagent_type: str,
    description: str,
    prompt: str,
    response: str,
    filename: str = "",
    events: list[dict[str, Any]] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    total_tokens: int = 0,
) -> str:
    """
    Render a subagent conversation file.

    Args:
        subagent_type: Type of the subagent
        description: Task description
        prompt: Full task prompt
        response: Response summary
        filename: Filename (without .md) for header
        events: List of conversation events (tools, messages) if available
        start_time: Start timestamp
        end_time: End timestamp
        total_tokens: Token count

    Returns:
        Markdown content for the subagent file
    """
    lines: list[str] = []

    # Header - include filename per test expectations
    if filename:
        lines.append(f"# Subagent: {subagent_type} ({filename})")
    else:
        lines.append(f"# Subagent: {subagent_type}")
    lines.append("")

    # Metadata in PIPELINE_SPECIFIC
    lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
    if start_time:
        lines.append(f"- **Started**: {format_timestamp(start_time)}")
    if end_time:
        lines.append(f"- **Ended**: {format_timestamp(end_time)}")
    if total_tokens:
        lines.append(f"- **Tokens**: {total_tokens:,}")
    lines.append("<!-- END PIPELINE_SPECIFIC -->")
    lines.append("")

    # Task description
    lines.append("## Task")
    lines.append("")
    if description:
        lines.append(f"**Description:** {description}")
        lines.append("")
    lines.append("**Prompt:**")
    lines.append("")
    lines.append(prompt if prompt else "(no prompt)")
    lines.append("")

    # Conversation events (if we have linked trace)
    if events:
        lines.append("## Conversation")
        lines.append("")

        tool_result_files: dict[str, str] = {}
        tool_result_sequence = 0

        for event in events:
            event_type = event.get("event_type", "")

            if event_type == "user":
                lines.append("### User")
                lines.append("")
                lines.append(event.get("text", ""))
                lines.append("")
                lines.append("---")
                lines.append("")

            elif event_type == "assistant":
                lines.append("### Assistant")
                lines.append("")
                lines.append(event.get("text", ""))
                lines.append("")
                lines.append("---")
                lines.append("")

            elif event_type == "tool":
                tool_lines, tool_result_sequence = _render_tool_section(
                    event.get("name", "Unknown"),
                    event.get("input", {}),
                    event.get("result", ""),
                    tool_result_files,
                    tool_result_sequence,
                )
                lines.extend(tool_lines)
    else:
        # No linked conversation available
        lines.append("*[Full conversation not available - linked trace not found]*")
        lines.append("")

    # Response summary
    lines.append("## Response")
    lines.append("")
    lines.append(response if response else "(no response)")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# Main Rendering Function
# =============================================================================


def render_jsonl_to_markdown(
    records: list[dict[str, Any]],
    pipeline: str = "litellm",
) -> MarkdownExport:
    """
    Render JSONL conversation records to markdown format.

    This function takes the output of export_chain_to_jsonl() or
    export_claude_session_to_jsonl() and produces markdown matching
    AGREED_FORMAT.md specification.

    Args:
        records: List of JSONL records with header, events, and footer
        pipeline: "litellm" or "claude" - determines PIPELINE_SPECIFIC content

    Returns:
        MarkdownExport with main content, subagent files, and tool result files.
    """
    # Extract header
    header = next((r for r in records if r.get("record_type") == "header"), {})

    chain_id = header.get("chain_id", "unknown")
    session_id = header.get("claude_session_id", chain_id)
    start_time = header.get("start_time")
    end_time = header.get("end_time")
    total_tokens = header.get("total_tokens", 0)
    models_used = list(header.get("metrics", {}).get("models_used", {}).keys())
    compaction_count = header.get("compaction_count", 0)

    # Claude-specific fields
    project_path = header.get("project_path", "")
    git_branch = header.get("git_branch")
    summary = header.get("summary", "")

    # Stats
    stats = {
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "subagents": 0,
        "compactions": 0,
    }

    # Track subagent files and tool result files
    subagent_files: dict[str, str] = {}
    tool_result_files: dict[str, str] = {}
    tool_result_sequence = 0

    # Track subagent types for sequencing
    subagent_type_counts: dict[str, int] = {}

    # Build main content
    lines: list[str] = []

    # Header section per AGREED_FORMAT.md: # Session: {first_8_chars}
    session_id_short = session_id[:8] if len(session_id) >= 8 else session_id
    lines.append(f"# Session: {session_id_short}")
    lines.append("")

    # Metadata section per AGREED_FORMAT.md
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Session ID**: `{session_id}`")
    lines.append("")

    # Pipeline-specific metadata
    lines.append("<!-- BEGIN PIPELINE_SPECIFIC -->")
    if pipeline == "claude":
        if start_time:
            lines.append(f"- **Started**: {format_timestamp(start_time)} (Claude only)")
        if end_time:
            lines.append(f"- **Ended**: {format_timestamp(end_time)} (Claude only)")
        if project_path:
            lines.append(f"- **Project**: `{project_path}` (Claude only)")
        branch_str = f"`{git_branch}`" if git_branch else "*[No branch]*"
        lines.append(f"- **Branch**: {branch_str} (Claude only)")
        summary_str = summary if summary else "*[No summary]*"
        lines.append(f"- **Summary**: {summary_str} (Claude only)")
    else:
        # LiteLLM pipeline
        if start_time:
            lines.append(f"- **Started**: {format_timestamp(start_time)} (LiteLLM only)")
        if end_time:
            lines.append(f"- **Ended**: {format_timestamp(end_time)} (LiteLLM only)")
        if models_used:
            lines.append(f"- **Models**: {', '.join(models_used)} (LiteLLM only)")
        if compaction_count:
            lines.append(f"- **Compactions**: {compaction_count} (LiteLLM only)")
    lines.append("<!-- END PIPELINE_SPECIFIC -->")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Conversation section header per AGREED_FORMAT.md
    lines.append("## Conversation")
    lines.append("")

    # Process conversation events
    events = [r for r in records if r.get("record_type") == "event"]

    for event in events:
        event_type = event.get("event_type", "")

        if event_type == "user":
            lines.append("### User")
            lines.append("")
            lines.append(event.get("text", ""))
            lines.append("")
            lines.append("---")
            lines.append("")
            stats["user_turns"] += 1

        elif event_type == "assistant":
            lines.append("### Assistant")
            lines.append("")
            lines.append(event.get("text", ""))
            lines.append("")
            lines.append("---")
            lines.append("")
            stats["assistant_turns"] += 1

        elif event_type == "tool":
            tool_lines, tool_result_sequence = _render_tool_section(
                event.get("name", "Unknown"),
                event.get("input", {}),
                event.get("result", ""),
                tool_result_files,
                tool_result_sequence,
            )
            lines.extend(tool_lines)
            stats["tool_calls"] += 1

        elif event_type == "parallel_tools":
            parallel_lines, tool_result_sequence = _render_parallel_tools_section(
                event.get("tools", []),
                tool_result_files,
                tool_result_sequence,
            )
            lines.extend(parallel_lines)
            stats["tool_calls"] += len(event.get("tools", []))

        elif event_type == "subagent":
            subagent_type = event.get("subagent_type", "unknown")
            description = event.get("description", "")
            prompt = event.get("prompt", "")
            response = event.get("response", "")
            subagent_events = event.get("events")  # May be None if no trace linkage

            # Generate filename
            normalized_type = normalize_subagent_type(subagent_type)
            subagent_type_counts[normalized_type] = subagent_type_counts.get(normalized_type, 0) + 1
            sequence = subagent_type_counts[normalized_type]
            filename = f"subagent_{normalized_type}_{sequence}"

            # Render section in main file
            section_lines = _render_subagent_section(
                subagent_type,
                description,
                prompt,
                response,
                filename,
            )
            lines.extend(section_lines)

            # Render subagent file
            subagent_content = _render_subagent_file(
                subagent_type,
                description,
                prompt,
                response,
                filename=filename,
                events=subagent_events,
                start_time=event.get("start_time"),
                end_time=event.get("end_time"),
                total_tokens=event.get("total_tokens", 0),
            )
            subagent_files[filename] = subagent_content
            stats["subagents"] += 1

        elif event_type == "compaction":
            compaction_lines = _render_compaction_section(
                event.get("number", 0),
                event.get("summary", ""),
                pipeline=pipeline,
                pre_compaction_tokens=event.get("pre_tokens"),  # JSONL uses pre_tokens
                trigger=event.get("trigger"),
                tool_result_files=tool_result_files,
            )
            lines.extend(compaction_lines)
            stats["compactions"] += 1

    # Footer with stats per AGREED_FORMAT.md
    lines.append("---")
    lines.append("")
    lines.append(f"*Exported from session `{session_id}`*")
    # Include compaction count if any
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

    # Join all lines
    main_content = "\n".join(lines)

    return MarkdownExport(
        main_content=main_content,
        subagent_files=subagent_files,
        tool_result_files=tool_result_files,
        session_id=session_id,
        stats=stats,
    )
