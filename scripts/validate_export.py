#!/usr/bin/env python3
"""
Ground Truth Validation Script

Compares raw span data from parquet against exported markdown to catch missing content.

Usage:
    uv run python scripts/validate_export.py --session <session_id>
    uv run python scripts/validate_export.py --session 3640c6d77574ea64f556583219487860
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


# Thresholds from 4.1 decisions
TOOL_INLINE_THRESHOLD = 500  # chars
SUBAGENT_INLINE_THRESHOLD = 1000  # chars


@dataclass
class ValidationResult:
    """Results from validating a session export."""

    session_id: str

    # User messages
    user_messages_expected: int = 0
    user_messages_found: int = 0
    user_messages_missing: list[str] = field(default_factory=list)

    # Assistant messages
    assistant_messages_expected: int = 0
    assistant_messages_found: int = 0
    assistant_messages_missing: list[str] = field(default_factory=list)

    # Tool calls
    tool_calls_expected: int = 0
    tool_calls_found: int = 0
    tool_calls_missing: list[str] = field(default_factory=list)

    # Tool results
    tool_results_expected: int = 0
    tool_results_inline: int = 0
    tool_results_linked: int = 0
    tool_results_missing: int = 0

    # Subagents
    subagents_expected: int = 0
    subagents_found: int = 0
    subagents_missing: list[str] = field(default_factory=list)

    # Warnings
    warnings: list[str] = field(default_factory=list)

    # Content for inspection
    export_content: str = ""

    @property
    def passed(self) -> bool:
        """Check if validation passed (all expected content found)."""
        return (
            self.user_messages_found == self.user_messages_expected
            and self.assistant_messages_found == self.assistant_messages_expected
            and self.tool_calls_found == self.tool_calls_expected
            and self.tool_results_missing == 0
            and self.subagents_found == self.subagents_expected
        )

    def print_report(self) -> None:
        """Print a human-readable validation report."""
        symbols = {
            'pass': '✅',
            'fail': '❌',
            'warn': '⚠️ ',
            'info': 'ℹ️ ',
        }

        print(f"\n{'='*60}")
        print(f"Validation Report: {self.session_id}")
        print(f"{'='*60}\n")

        # User messages
        user_status = symbols['pass'] if self.user_messages_found == self.user_messages_expected else symbols['fail']
        print(f"{user_status} User messages: {self.user_messages_found}/{self.user_messages_expected} found")
        if self.user_messages_missing:
            for msg in self.user_messages_missing:
                preview = msg[:80].replace('\n', ' ')
                print(f"   Missing: {preview}...")

        # Assistant messages
        asst_status = symbols['pass'] if self.assistant_messages_found == self.assistant_messages_expected else symbols['fail']
        print(f"{asst_status} Assistant messages: {self.assistant_messages_found}/{self.assistant_messages_expected} found")
        if self.assistant_messages_missing:
            for msg in self.assistant_messages_missing:
                preview = msg[:80].replace('\n', ' ')
                print(f"   Missing: {preview}...")

        # Tool calls
        tool_status = symbols['pass'] if self.tool_calls_found == self.tool_calls_expected else symbols['fail']
        print(f"{tool_status} Tool calls: {self.tool_calls_found}/{self.tool_calls_expected} found")
        if self.tool_calls_missing:
            for tool in self.tool_calls_missing:
                print(f"   Missing: {tool}")

        # Tool results
        total_results = self.tool_results_inline + self.tool_results_linked
        result_status = symbols['pass'] if self.tool_results_missing == 0 else symbols['fail']
        print(f"{result_status} Tool results: {total_results}/{self.tool_results_expected} found")
        if total_results > 0:
            print(f"   {symbols['info']}Inline: {self.tool_results_inline}, Linked: {self.tool_results_linked}")
        if self.tool_results_missing > 0:
            print(f"   Missing: {self.tool_results_missing}")

        # Subagents
        subagent_status = symbols['pass'] if self.subagents_found == self.subagents_expected else symbols['fail']
        print(f"{subagent_status} Subagents: {self.subagents_found}/{self.subagents_expected} found")
        if self.subagents_missing:
            for sub in self.subagents_missing:
                print(f"   Missing: {sub}")

        # Warnings
        if self.warnings:
            print(f"\n{symbols['warn']}Warnings:")
            for warning in self.warnings:
                print(f"   {warning}")

        # Final result
        print(f"\n{'='*60}")
        if self.passed:
            if self.warnings:
                print(f"{symbols['warn']} RESULT: PASS (with warnings)")
            else:
                print(f"{symbols['pass']} RESULT: PASS")
        else:
            print(f"{symbols['fail']} RESULT: FAIL")
        print(f"{'='*60}\n")


def load_session_spans(session_id: str, parquet_path: str) -> list[dict[str, Any]]:
    """Load all spans for a session from parquet file."""
    df = pq.read_table(parquet_path).to_pandas()
    session_df = df[df['session_id'] == session_id].sort_values('start_time')

    spans = []
    for _, row in session_df.iterrows():
        span = row.to_dict()

        # Extract input/output from raw_attributes_json if not in direct fields
        if span.get('raw_attributes_json'):
            try:
                attrs = json.loads(span['raw_attributes_json'])
                if not span.get('input_value'):
                    span['input_value'] = attrs.get('attributes', {}).get('input', {}).get('value', '')
                if not span.get('output_value'):
                    span['output_value'] = attrs.get('attributes', {}).get('output', {}).get('value', '')
            except (json.JSONDecodeError, TypeError):
                pass

        spans.append(span)

    return spans


def parse_message_content(content: str) -> list[dict[str, Any]]:
    """
    Parse message content which may be JSON array of message blocks.
    Returns list of message dictionaries with 'type' and content.
    """
    if not content:
        return []

    try:
        parsed = json.loads(content) if isinstance(content, str) else content
        if isinstance(parsed, list):
            messages = []
            for item in parsed:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        messages.append({"type": "text", "text": item.get("text", "")})
                    elif item.get("type") == "tool_use":
                        messages.append({
                            "type": "tool_use",
                            "tool": item.get("name", "unknown"),
                            "input": item.get("input", {}),
                            "id": item.get("id", ""),
                        })
                    elif item.get("type") == "tool_result":
                        messages.append({
                            "type": "tool_result",
                            "content": item.get("content", ""),
                            "tool_use_id": item.get("tool_use_id", ""),
                        })
            return messages
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Return as plain text if parsing failed
    return [{"type": "text", "text": content}]


def is_system_reminder(text: str) -> bool:
    """Check if text is a system reminder that should be excluded."""
    return text.strip().startswith("<system-reminder>")


def is_warmup_message(text: str) -> bool:
    """Check if text is a warmup/initialization message."""
    clean = text.strip().strip('"')
    return clean == "Warmup"


def extract_user_messages(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract all user messages from INPUT of Internal_Prompt spans."""
    user_messages = []

    for span in spans:
        name = span.get('name', '')
        if not name.startswith('Claude_Code_Internal_Prompt_'):
            continue

        input_val = span.get('input_value', '')
        if not input_val:
            continue

        # Skip compaction-related inputs (per 4.1 decisions)
        # Compaction task: "Your task is to create a detailed summary..."
        # Compaction continuation: "This session is being continued..." + "The conversation is summarized below:"
        if 'task is to create a detailed summary' in input_val:
            continue
        if 'The conversation is summarized below:' in input_val:
            continue

        messages = parse_message_content(input_val)
        for msg in messages:
            if msg['type'] == 'text':
                text = msg.get('text', '').strip()

                # Skip system reminders
                if is_system_reminder(text):
                    continue

                # Skip tool results being echoed
                if text.startswith("Command:") and "\nOutput:" in text:
                    continue

                # Skip JSON-like tool results
                if text.startswith("{") and text.endswith("}") and len(text) > 100:
                    continue

                # Include warmup messages (will be shown as initialization)
                if is_warmup_message(text):
                    user_messages.append({
                        'text': text,
                        'is_warmup': True,
                        'span_id': span.get('span_id'),
                    })
                    continue

                # Include substantive user messages (>10 chars)
                if text and len(text) > 10:
                    user_messages.append({
                        'text': text,
                        'is_warmup': False,
                        'span_id': span.get('span_id'),
                    })

    return user_messages


def extract_assistant_messages(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract all assistant messages from OUTPUT of Internal_Prompt spans."""
    assistant_messages = []

    for span in spans:
        name = span.get('name', '')
        if not name.startswith('Claude_Code_Internal_Prompt_'):
            continue

        output_val = span.get('output_value', '')
        if not output_val:
            continue

        messages = parse_message_content(output_val)
        for msg in messages:
            if msg['type'] == 'text':
                text = msg.get('text', '').strip()

                # Skip ancillary patterns
                if '"isNewTopic"' in text or '"is_displaying_contents"' in text:
                    continue

                # Skip very short routing signals
                if text in ('#', '{', '[{"type": "text", "text": "{"}]'):
                    continue

                if text:
                    assistant_messages.append({
                        'text': text,
                        'span_id': span.get('span_id'),
                    })

    return assistant_messages


def extract_tool_calls(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract all tool_use blocks from OUTPUT of Internal_Prompt spans.

    NOTE: Task tool calls (subagents) are excluded since they're tracked separately.
    """
    tool_calls = []

    for span in spans:
        name = span.get('name', '')
        if not name.startswith('Claude_Code_Internal_Prompt_'):
            continue

        output_val = span.get('output_value', '')
        if not output_val:
            continue

        messages = parse_message_content(output_val)
        for msg in messages:
            if msg['type'] == 'tool_use':
                # Skip Task tool (subagents) - they're validated separately
                if msg['tool'] == 'Task':
                    continue

                tool_calls.append({
                    'tool_name': msg['tool'],
                    'tool_id': msg['id'],
                    'tool_input': msg['input'],
                    'span_id': span.get('span_id'),
                })

    return tool_calls


def extract_tool_results(spans: list[dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    """
    Extract tool results from INPUT of subsequent spans (tool_result messages).

    Returns:
        Tuple of (tool_results dict, set of subagent tool_use_ids)
    """
    tool_results = {}
    subagent_tool_ids = set()

    # First, identify which tool_use_ids are subagents (Task tool)
    for span in spans:
        output_val = span.get('output_value', '')
        if output_val:
            messages = parse_message_content(output_val)
            for msg in messages:
                if msg['type'] == 'tool_use' and msg['tool'] == 'Task':
                    subagent_tool_ids.add(msg['id'])

    # Now extract tool results, excluding subagent results
    for span in spans:
        input_val = span.get('input_value', '')
        if not input_val:
            continue

        messages = parse_message_content(input_val)
        for msg in messages:
            if msg['type'] == 'tool_result':
                tool_use_id = msg.get('tool_use_id', '')
                content = msg.get('content', '')

                # Skip subagent results (validated separately)
                if tool_use_id in subagent_tool_ids:
                    continue

                if tool_use_id:
                    # Content might be a list of content blocks or a string
                    if isinstance(content, list):
                        text_parts = []
                        for item in content:
                            if isinstance(item, dict) and item.get('type') == 'text':
                                text_parts.append(item.get('text', ''))
                        content_str = '\n'.join(text_parts)
                    else:
                        content_str = str(content)

                    tool_results[tool_use_id] = content_str

    return tool_results, subagent_tool_ids


def extract_subagents(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract subagent (Task tool) calls from tool_use blocks."""
    subagents = []

    for span in spans:
        name = span.get('name', '')
        if not name.startswith('Claude_Code_Internal_Prompt_'):
            continue

        output_val = span.get('output_value', '')
        if not output_val:
            continue

        messages = parse_message_content(output_val)
        for msg in messages:
            if msg['type'] == 'tool_use' and msg['tool'] == 'Task':
                tool_input = msg.get('input', {})
                subagents.append({
                    'tool_id': msg['id'],
                    'subagent_type': tool_input.get('subagent_type', 'unknown'),
                    'prompt': tool_input.get('prompt', ''),
                    'span_id': span.get('span_id'),
                })

    return subagents


def run_export(session_id: str, parquet_path: str) -> str:
    """Run the markdown export and return the content."""
    # Import here to avoid circular dependencies
    from dev_agent_lens.analysis.chains import (
        build_conversation_chains,
        export_chain_to_markdown,
    )

    # Load spans from parquet
    df = pq.read_table(parquet_path).to_pandas()

    # Build session structure expected by chains module
    session_df = df[df['session_id'] == session_id]
    if session_df.empty:
        raise ValueError(f"No spans found for session {session_id}")

    spans = []
    for _, row in session_df.iterrows():
        spans.append(row.to_dict())

    sessions = [{'session_id': session_id, 'spans': spans}]

    # Build chains and export
    chains = build_conversation_chains(sessions)
    if not chains:
        raise ValueError(f"No chains built for session {session_id}")

    chain = chains[0]
    result = export_chain_to_markdown(
        chain,
        sessions,
        include_tool_calls=True,
        include_metadata=True,
        scaffolded=True,
    )

    return result.main_content


def validate_session(session_id: str, parquet_path: str) -> ValidationResult:
    """
    Validate that exported markdown contains all expected content from raw spans.

    Args:
        session_id: Session ID to validate
        parquet_path: Path to parquet file with raw span data

    Returns:
        ValidationResult with comparison results
    """
    result = ValidationResult(session_id=session_id)

    # Load raw spans
    print(f"Loading spans for session {session_id}...")
    spans = load_session_spans(session_id, parquet_path)
    print(f"Found {len(spans)} spans")

    # Extract expected content
    print("Extracting expected content from raw spans...")
    user_messages = extract_user_messages(spans)
    assistant_messages = extract_assistant_messages(spans)
    tool_calls = extract_tool_calls(spans)
    tool_results, subagent_tool_ids = extract_tool_results(spans)
    subagents = extract_subagents(spans)

    result.user_messages_expected = len(user_messages)
    result.assistant_messages_expected = len(assistant_messages)
    result.tool_calls_expected = len(tool_calls)
    result.tool_results_expected = len(tool_results)
    result.subagents_expected = len(subagents)

    print(f"Expected: {result.user_messages_expected} user msgs, "
          f"{result.assistant_messages_expected} assistant msgs, "
          f"{result.tool_calls_expected} tool calls, "
          f"{result.subagents_expected} subagents")

    # Run export
    print("\nRunning markdown export...")
    export_content = run_export(session_id, parquet_path)
    result.export_content = export_content

    # Normalize content for comparison
    export_normalized = export_content.lower()

    # Validate user messages
    print("\nValidating user messages...")
    for msg in user_messages:
        text = msg['text']

        # For warmup messages, check for initialization marker
        if msg.get('is_warmup'):
            if '[session initialization]' in export_normalized:
                result.user_messages_found += 1
            else:
                result.user_messages_missing.append(text)
                result.warnings.append("Warmup message not shown as [Session initialization]")
        else:
            # Check if message text appears in export (case-insensitive, first 100 chars)
            search_text = text[:100].lower()
            if search_text in export_normalized:
                result.user_messages_found += 1
            else:
                result.user_messages_missing.append(text)

    # Validate assistant messages
    print("Validating assistant messages...")
    for msg in assistant_messages:
        text = msg['text']
        # Check first 100 chars (accounts for truncation)
        search_text = text[:100].lower()
        if search_text in export_normalized:
            result.assistant_messages_found += 1
        else:
            result.assistant_messages_missing.append(text)

    # Validate tool calls
    print("Validating tool calls...")
    for tool in tool_calls:
        tool_name = tool['tool_name']
        tool_id = tool['tool_id']

        # Check if tool name appears (summary representation)
        # Tool calls appear as "🔧 **#N ToolName**: summary"
        if tool_name.lower() in export_normalized:
            result.tool_calls_found += 1
        else:
            result.tool_calls_missing.append(f"{tool_name} ({tool_id[:8]})")

    # Validate tool results
    print("Validating tool results...")
    for tool_id, content in tool_results.items():
        content_size = len(content)

        # Check if result is inline or linked based on threshold
        if content_size <= TOOL_INLINE_THRESHOLD:
            # Should be inline - check if content appears (try both raw and blockquote-formatted)
            search_text = content[:100].lower()
            # Also try with markdown blockquote formatting (> prefix on each line)
            search_lines = content[:100].split('\n')
            search_blockquoted = '\n'.join(f'> {line}' for line in search_lines).lower()

            if search_text in export_normalized or search_blockquoted in export_normalized:
                result.tool_results_inline += 1
            else:
                result.tool_results_missing += 1
                result.warnings.append(f"Small result ({content_size} chars) should be inline but not found")
        else:
            # Should be linked - check if tool_calls/ directory reference appears
            if 'tool_calls/' in export_content and tool_id[:8] in export_content:
                result.tool_results_linked += 1
            else:
                # Check if content appears inline (try both raw and blockquote-formatted)
                search_text = content[:100].lower()
                search_lines = content[:100].split('\n')
                search_blockquoted = '\n'.join(f'> {line}' for line in search_lines).lower()

                if search_text in export_normalized or search_blockquoted in export_normalized:
                    # Found inline when it should be linked
                    result.tool_results_linked += 1
                    result.warnings.append(f"Large result ({content_size} chars) found inline, expected link")
                else:
                    result.tool_results_missing += 1

    # Validate subagents
    print("Validating subagents...")
    for sub in subagents:
        subagent_type = sub['subagent_type']
        tool_id = sub['tool_id']

        # Check if subagent reference appears
        # Subagents appear as "📦 **Subagent**: type"
        if subagent_type.lower() in export_normalized and 'subagent' in export_normalized:
            result.subagents_found += 1
        else:
            result.subagents_missing.append(f"{subagent_type} ({tool_id[:8]})")

    return result


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Validate markdown export against raw span data"
    )
    parser.add_argument(
        '--session',
        required=True,
        help='Session ID to validate'
    )
    parser.add_argument(
        '--parquet',
        default=None,
        help='Path to parquet file (default: from OxenStore)'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show detailed export content'
    )

    args = parser.parse_args()

    # Get parquet path
    if args.parquet:
        parquet_path = args.parquet
    else:
        from dev_agent_lens.storage.oxen_store import OxenStore
        from pathlib import Path
        store = OxenStore()
        # Find spans parquet file (phoenix-local-alex_spans.parquet)
        parquet_dir = Path(store.data_path) / 'parquet'
        parquet_files = list(parquet_dir.glob('*_spans.parquet'))
        if not parquet_files:
            print(f"Error: No spans parquet files found in {parquet_dir}")
            return 1
        # Use the first one (or phoenix-local-alex if available)
        parquet_path = str(parquet_files[0])
        for pf in parquet_files:
            if 'phoenix-local-alex' in pf.name:
                parquet_path = str(pf)
                break

    if not Path(parquet_path).exists():
        print(f"Error: Parquet file not found at {parquet_path}")
        return 1

    try:
        # Run validation
        result = validate_session(args.session, parquet_path)

        # Print report
        result.print_report()

        # Show export content if verbose
        if args.verbose:
            print("\n" + "="*60)
            print("EXPORT CONTENT")
            print("="*60)
            print(result.export_content)

        # Return exit code
        return 0 if result.passed else 1

    except Exception as e:
        print(f"\nError during validation: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
