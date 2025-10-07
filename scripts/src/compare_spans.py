#!/usr/bin/env python3
"""
Compare spans within a session to identify duplication and accumulation patterns.

This script analyzes session data to detect if later spans contain duplicated
content from earlier spans, which is common in conversational AI where context
accumulates across turns.

Uses fuzzy/subset matching on actual content (not metadata) from both inputs
and outputs. Checks if text from earlier spans appears anywhere in later spans.

Usage:
    uv run compare_spans.py phoenix/phoenix_sessions.jsonl
    uv run compare_spans.py phoenix/phoenix_sessions.jsonl --session 1
"""

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import List, Dict, Any


def load_sessions(filepath: Path) -> List[Dict]:
    """Load sessions from JSONL file."""
    sessions = []
    with open(filepath, 'r') as f:
        for line in f:
            sessions.append(json.loads(line))
    return sessions


def compare_messages(msg1: List, msg2: List) -> Dict[str, Any]:
    """Compare two message lists and return overlap statistics."""
    # Convert to comparable format
    msg1_str = [json.dumps(m, sort_keys=True) if isinstance(m, dict) else str(m) for m in (msg1 or [])]
    msg2_str = [json.dumps(m, sort_keys=True) if isinstance(m, dict) else str(m) for m in (msg2 or [])]

    # Find duplicates (messages from msg1 that appear in msg2)
    duplicates = [m for m in msg1_str if m in msg2_str]

    # Find new messages (in msg2 but not in msg1)
    new_msgs = [m for m in msg2_str if m not in msg1_str]

    overlap_pct = (len(duplicates) / len(msg2_str) * 100) if msg2_str else 0

    return {
        'total_previous': len(msg1_str),
        'total_current': len(msg2_str),
        'duplicated_count': len(duplicates),
        'new_count': len(new_msgs),
        'overlap_percentage': overlap_pct,
        'duplicated_messages': duplicates,
        'new_messages': new_msgs
    }


def extract_text_from_content(content) -> List[str]:
    """Recursively extract text from nested content structures."""
    texts = []

    if isinstance(content, str):
        # Try to parse as JSON/dict first (handle both JSON and Python dict string format)
        if content.strip().startswith('[') or content.strip().startswith('{'):
            # Try JSON first
            try:
                parsed = json.loads(content)
                texts.extend(extract_text_from_content(parsed))
                return texts
            except json.JSONDecodeError:
                pass

            # Try Python literal eval (handles single quotes properly)
            try:
                parsed = ast.literal_eval(content)
                texts.extend(extract_text_from_content(parsed))
                return texts
            except:
                pass

        # Not parseable or parsing failed - treat as plain text
        if content.strip():
            texts.append(content.strip())
    elif isinstance(content, dict):
        # Handle common content structures
        if 'text' in content:
            texts.extend(extract_text_from_content(content['text']))
        if 'thinking' in content:
            texts.extend(extract_text_from_content(content['thinking']))
        if 'content' in content:
            texts.extend(extract_text_from_content(content['content']))
        if 'message.content' in content:
            texts.extend(extract_text_from_content(content['message.content']))
    elif isinstance(content, list):
        for item in content:
            texts.extend(extract_text_from_content(item))

    return texts


def extract_content(span: Dict) -> str:
    """Extract all text content from a span (inputs and outputs)."""
    content_parts = []

    # Get input messages content
    input_msgs = span.get('attributes.llm.input_messages', [])
    if isinstance(input_msgs, list):
        for msg in input_msgs:
            if isinstance(msg, dict):
                msg_content = msg.get('message.content', '')
                if msg_content:
                    content_parts.extend(extract_text_from_content(msg_content))

    # Get input value
    input_val = span.get('attributes.input.value', '')
    if input_val:
        content_parts.extend(extract_text_from_content(input_val))

    # Get output messages
    output_msgs = span.get('attributes.llm.output_messages', '')
    if output_msgs:
        content_parts.extend(extract_text_from_content(output_msgs))

    # Get output value
    output_val = span.get('attributes.output.value', '')
    if output_val:
        content_parts.extend(extract_text_from_content(output_val))

    return '\n'.join(content_parts)


def check_content_containment(earlier_span: Dict, later_span: Dict) -> Dict[str, Any]:
    """Check if content from earlier span is contained in later span (fuzzy/subset matching)."""
    earlier_content = extract_content(earlier_span)
    later_content = extract_content(later_span)

    # Extract individual text chunks from earlier span to check
    chunks = []

    # Get input messages
    input_msgs = earlier_span.get('attributes.llm.input_messages', [])
    if isinstance(input_msgs, list):
        for msg in input_msgs:
            if isinstance(msg, dict):
                content = msg.get('message.content', '')
                if content:
                    texts = extract_text_from_content(content)
                    for text in texts:
                        if text and text.strip():
                            chunks.append(('input_msg', text.strip()))

    # Get input value
    input_val = earlier_span.get('attributes.input.value', '')
    if input_val:
        texts = extract_text_from_content(input_val)
        for text in texts:
            if text and text.strip():
                chunks.append(('input_val', text.strip()))

    # Get output
    output_msgs = earlier_span.get('attributes.llm.output_messages', '')
    if output_msgs:
        texts = extract_text_from_content(output_msgs)
        for text in texts:
            if text and text.strip():
                chunks.append(('output_msg', text.strip()))

    output_val = earlier_span.get('attributes.output.value', '')
    if output_val:
        texts = extract_text_from_content(output_val)
        for text in texts:
            if text and text.strip():
                chunks.append(('output_val', text.strip()))

    # Check which chunks are contained in later content
    contained = []
    missing = []

    for chunk_type, chunk_text in chunks:
        # Fuzzy check: is this chunk contained as substring in later content?
        if chunk_text in later_content:
            # Find the position and context
            pos = later_content.find(chunk_text)
            # Get some context around the match (50 chars before and after)
            context_start = max(0, pos - 50)
            context_end = min(len(later_content), pos + len(chunk_text) + 50)
            context = later_content[context_start:context_end]

            contained.append((chunk_type, chunk_text, pos, context))
        else:
            missing.append((chunk_type, chunk_text))

    total_chunks = len(chunks)
    is_complete_subset = len(missing) == 0 and total_chunks > 0
    containment_pct = (len(contained) / total_chunks * 100) if total_chunks > 0 else 0

    return {
        'is_complete_subset': is_complete_subset,
        'total_chunks': total_chunks,
        'contained_count': len(contained),
        'missing_count': len(missing),
        'containment_percentage': containment_pct,
        'contained_chunks': contained,
        'missing_chunks': missing
    }


def analyze_session(session: Dict) -> None:
    """Analyze a single session for span duplication."""
    print(f"\n{'═' * 80}")
    print(f"SESSION {session['session_number']}: {session.get('session_id', 'unknown')}")
    print(f"{'═' * 80}")
    print(f"Total spans: {session['span_count']}")
    print(f"Duration: {session['duration_seconds']:.2f}s")
    print(f"Unique traces: {session['unique_traces']}")

    spans = session['spans']

    # First, do containment analysis
    print(f"\n{'─' * 80}")
    print(f"CONTAINMENT ANALYSIS")
    print(f"{'─' * 80}")
    print(f"Checking if earlier spans are completely contained in later spans...\n")

    if len(spans) > 1:
        last_span = spans[-1]

        for i, span in enumerate(spans[:-1]):  # All except last
            result = check_content_containment(span, last_span)

            status = "✅ FULLY CONTAINED" if result['is_complete_subset'] else "❌ PARTIAL/NOT CONTAINED"
            print(f"Span {i+1} → Span {len(spans)}: {status}")
            print(f"  Content chunks in Span {i+1}: {result['total_chunks']}")
            print(f"  Found in Span {len(spans)}: {result['contained_count']} ({result['containment_percentage']:.1f}%)")

            if result['contained_count'] > 0:
                print(f"\n  ✅ Contained chunks and their locations:")
                for chunk_type, chunk_text, pos, context in result['contained_chunks'][:5]:  # Show first 5
                    chunk_preview = chunk_text[:100].replace('\n', ' ')
                    print(f"\n    [{chunk_type}] at position {pos}")
                    print(f"      Chunk: {chunk_preview}{'...' if len(chunk_text) > 100 else ''}")
                    print(f"      Context: ...{context.replace(chr(10), ' ')}...")

            if result['missing_count'] > 0:
                print(f"\n  ❌ Missing chunks:")
                for chunk_type, chunk_text in result['missing_chunks'][:3]:  # Show first 3
                    preview = chunk_text[:200].replace('\n', ' ')
                    print(f"    [{chunk_type}] {preview}{'...' if len(chunk_text) > 200 else ''}")
            print()

    print(f"{'─' * 80}\n")

    # Analyze each span
    for i, span in enumerate(spans):
        print(f"\n{'─' * 80}")
        print(f"SPAN {i+1}: {span.get('name', 'unknown')} ({span.get('context.span_id', '')[:16]})")
        print(f"{'─' * 80}")
        print(f"Start: {span.get('start_time')}")
        print(f"Duration: {(span.get('end_time', span.get('start_time')) - span.get('start_time', 0)):.2f}s" if isinstance(span.get('start_time'), (int, float)) else "N/A")

        # Get input/output data
        input_msgs = span.get('attributes.llm.input_messages', [])
        output_msgs = span.get('attributes.llm.output_messages', '')
        input_val = span.get('attributes.input.value', '')

        # Show basic stats
        if isinstance(input_msgs, list):
            print(f"Input messages: {len(input_msgs)} messages")
        if input_val:
            print(f"Input value: {len(str(input_val))} chars")
        if output_msgs:
            print(f"Output: {len(str(output_msgs))} chars")

        # Compare with previous span
        if i > 0:
            prev_span = spans[i - 1]
            prev_input_msgs = prev_span.get('attributes.llm.input_messages', [])

            if isinstance(input_msgs, list) and isinstance(prev_input_msgs, list):
                comparison = compare_messages(prev_input_msgs, input_msgs)

                print(f"\n  📊 Comparison with Span {i}:")
                print(f"    Messages in previous: {comparison['total_previous']}")
                print(f"    Messages in current:  {comparison['total_current']}")
                print(f"    Duplicated:           {comparison['duplicated_count']} ({comparison['overlap_percentage']:.1f}%)")
                print(f"    New:                  {comparison['new_count']}")

                if comparison['duplicated_count'] > 0:
                    print(f"    ⚠️  DUPLICATION DETECTED: {comparison['duplicated_count']} messages from previous span")

                if comparison['new_count'] > 0:
                    print(f"\n  🆕 New messages:")
                    for j, new_msg in enumerate(comparison['new_messages'][:3]):  # Show first 3
                        msg_dict = json.loads(new_msg) if new_msg.startswith('{') else new_msg
                        if isinstance(msg_dict, dict):
                            role = msg_dict.get('message.role', 'unknown')
                            content = str(msg_dict.get('message.content', ''))[:150]
                            print(f"      [{j+1}] {role}: {content}...")
                        else:
                            print(f"      [{j+1}] {str(msg_dict)[:150]}...")
        else:
            print(f"\n  📌 BASELINE SPAN (first in session)")

    # Summary
    print(f"\n{'═' * 80}")
    print(f"SESSION SUMMARY")
    print(f"{'═' * 80}")

    total_input_msgs = sum(len(s.get('attributes.llm.input_messages', [])) if isinstance(s.get('attributes.llm.input_messages'), list) else 0 for s in spans)
    total_input_chars = sum(len(str(s.get('attributes.input.value', ''))) for s in spans)

    print(f"Total input messages across all spans: {total_input_msgs}")
    print(f"Total input characters: {total_input_chars:,}")
    print(f"Average messages per span: {total_input_msgs / len(spans):.1f}")

    # Calculate duplication factor
    if len(spans) > 1:
        first_span_msgs = len(spans[0].get('attributes.llm.input_messages', []))
        last_span_msgs = len(spans[-1].get('attributes.llm.input_messages', []))
        if first_span_msgs > 0:
            growth_factor = last_span_msgs / first_span_msgs
            print(f"Message growth factor: {growth_factor:.1f}x (from {first_span_msgs} to {last_span_msgs})")


def main():
    parser = argparse.ArgumentParser(
        description="Compare spans within sessions to identify duplication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        'session_file',
        type=str,
        help='Path to session JSONL file'
    )
    parser.add_argument(
        '--session',
        type=int,
        help='Analyze specific session number (default: all)'
    )

    args = parser.parse_args()

    session_file = Path(args.session_file)
    if not session_file.exists():
        print(f"❌ Error: File not found: {session_file}")
        sys.exit(1)

    print(f"Loading sessions from: {session_file}")
    sessions = load_sessions(session_file)
    print(f"Loaded {len(sessions)} session(s)")

    # Analyze sessions
    if args.session:
        # Analyze specific session
        target_sessions = [s for s in sessions if s['session_number'] == args.session]
        if not target_sessions:
            print(f"❌ Error: Session {args.session} not found")
            sys.exit(1)
        analyze_session(target_sessions[0])
    else:
        # Analyze all sessions
        for session in sessions:
            analyze_session(session)


if __name__ == '__main__':
    main()
