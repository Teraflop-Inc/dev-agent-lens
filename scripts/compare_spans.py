#!/usr/bin/env python3
"""
Compare spans within a session to identify duplication and accumulation patterns.

This script analyzes session data to detect if later spans contain duplicated
content from earlier spans, which is common in conversational AI where context
accumulates across turns.

Usage:
    uv run compare_spans.py phoenix/phoenix_sessions.jsonl
    uv run compare_spans.py phoenix/phoenix_sessions.jsonl --session 1
    uv run compare_spans.py phoenix/phoenix_sessions.jsonl --detailed
"""

import argparse
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


def check_complete_containment(earlier_msgs: List, later_msgs: List) -> Dict[str, Any]:
    """Check if all messages from earlier span are contained in later span."""
    earlier_str = [json.dumps(m, sort_keys=True) if isinstance(m, dict) else str(m) for m in (earlier_msgs or [])]
    later_str = [json.dumps(m, sort_keys=True) if isinstance(m, dict) else str(m) for m in (later_msgs or [])]

    # Check if all earlier messages exist in later messages
    contained = [m for m in earlier_str if m in later_str]
    missing = [m for m in earlier_str if m not in later_str]

    is_complete_subset = len(missing) == 0 and len(earlier_str) > 0
    containment_pct = (len(contained) / len(earlier_str) * 100) if earlier_str else 0

    return {
        'is_complete_subset': is_complete_subset,
        'total_earlier': len(earlier_str),
        'contained_count': len(contained),
        'missing_count': len(missing),
        'containment_percentage': containment_pct,
        'missing_messages': missing
    }


def analyze_session(session: Dict, detailed: bool = False) -> None:
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
        last_msgs = last_span.get('attributes.llm.input_messages', [])

        for i, span in enumerate(spans[:-1]):  # All except last
            span_msgs = span.get('attributes.llm.input_messages', [])

            if isinstance(span_msgs, list) and isinstance(last_msgs, list):
                result = check_complete_containment(span_msgs, last_msgs)

                status = "✅ FULLY CONTAINED" if result['is_complete_subset'] else "❌ PARTIAL/NOT CONTAINED"
                print(f"Span {i+1} → Span {len(spans)}: {status}")
                print(f"  Messages in Span {i+1}: {result['total_earlier']}")
                print(f"  Found in Span {len(spans)}: {result['contained_count']} ({result['containment_percentage']:.1f}%)")
                if result['missing_count'] > 0:
                    print(f"  Missing: {result['missing_count']} messages")
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

                if detailed and comparison['new_count'] > 0:
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
    parser.add_argument(
        '--detailed',
        action='store_true',
        help='Show detailed message content'
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
        analyze_session(target_sessions[0], detailed=args.detailed)
    else:
        # Analyze all sessions
        for session in sessions:
            analyze_session(session, detailed=args.detailed)


if __name__ == '__main__':
    main()
