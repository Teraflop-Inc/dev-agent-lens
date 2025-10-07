#!/usr/bin/env python3
"""
Analyze trace data to understand session structure

Supports both Arize and Phoenix trace data.

Usage:
    # Auto-detect backend (checks for arize/ or phoenix/ folders)
    uv run main.py analyze

    # Specify file path
    uv run main.py analyze arize/arize_traces.jsonl
    uv run main.py analyze phoenix/phoenix_traces.jsonl
"""
import json
import pandas as pd
from collections import Counter
import sys
import argparse
from pathlib import Path

def find_trace_file():
    """Auto-detect trace file from arize/ or phoenix/ folders."""
    script_dir = Path(__file__).parent.parent

    # Check phoenix first (local development priority)
    phoenix_file = script_dir / 'phoenix' / 'phoenix_traces.jsonl'
    if phoenix_file.exists():
        return phoenix_file

    # Check arize
    arize_file = script_dir / 'arize' / 'arize_traces.jsonl'
    if arize_file.exists():
        return arize_file

    return None

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Analyze trace data session structure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        'file',
        nargs='?',
        type=str,
        help='Path to trace file (default: auto-detect from arize/ or phoenix/)'
    )

    return parser.parse_args()

def main():
    args = parse_args()

    # Determine data file
    if args.file:
        data_file = Path(args.file)
    else:
        data_file = find_trace_file()
        if not data_file:
            print("❌ Error: No trace data found")
            print("\nPlease run the export script first:")
            print("  uv run main.py export")
            print("\nOr specify a file path:")
            print("  uv run main.py analyze <path/to/traces.jsonl>")
            sys.exit(1)
        print(f"🔍 Auto-detected: {data_file}")

    if not data_file.exists():
        print(f"❌ Error: Data file not found: {data_file}")
        sys.exit(1)

    df = pd.read_json(data_file, lines=True)

    print(f"Total records: {len(df)}")
    print(f"\nColumns: {len(df.columns)}")

    # Extract session IDs from metadata
    def extract_session_id(metadata):
        if pd.isna(metadata):
            return None

        # Handle string representation of dict (Phoenix format)
        if isinstance(metadata, str):
            if 'session_' in metadata:
                # Extract from pattern like: _session_ae99895f-9fad-4453-bb91-1005e8ccc4d3
                parts = metadata.split('session_')
                if len(parts) > 1:
                    # Get the session ID (everything after 'session_')
                    session_id = parts[-1].rstrip("'}\"")  # Remove trailing quotes/braces
                    return session_id
            return None

        if not isinstance(metadata, dict):
            return None

        # Try Phoenix format: metadata.user_id with _session_ pattern
        user_id = metadata.get('user_id')
        if user_id and 'session_' in str(user_id):
            return str(user_id).split('session_')[-1]

        # Try Arize format: user_api_key_end_user_id
        user_id = metadata.get('user_api_key_end_user_id')
        if user_id and 'session_' in user_id:
            return user_id.split('session_')[-1]

        # Also try requester_metadata.user_id (Arize format)
        req_meta = metadata.get('requester_metadata', {})
        if isinstance(req_meta, dict):
            user_id = req_meta.get('user_id')
            if user_id and 'session_' in user_id:
                return user_id.split('session_')[-1]

        return None

    # Check both metadata columns
    if 'metadata' in df.columns:
        df['session_id'] = df['metadata'].apply(extract_session_id)
    elif 'attributes.metadata' in df.columns:
        df['session_id'] = df['attributes.metadata'].apply(extract_session_id)
    else:
        print("❌ No metadata column found")
        df['session_id'] = None

    print(f"\n📊 Session Analysis:")
    print(f"  Unique sessions: {df['session_id'].nunique()}")
    print(f"  Records with session ID: {df['session_id'].notna().sum()}")
    print(f"  Records without session ID: {df['session_id'].isna().sum()}")

    # Show session distribution
    print(f"\n📈 Records per session:")
    session_counts = df['session_id'].value_counts().head(10)
    for session, count in session_counts.items():
        print(f"  {session}: {count} records")

    # Analyze span kinds
    print(f"\n🔍 Span kinds:")
    span_kinds = df['attributes.openinference.span.kind'].value_counts()
    for kind, count in span_kinds.items():
        print(f"  {kind}: {count}")

    # Analyze span names
    print(f"\n📝 Top span names:")
    names = df['name'].value_counts().head(10)
    for name, count in names.items():
        print(f"  {name}: {count}")

    # Check for parent-child relationships
    print(f"\n👨‍👦 Parent-child relationships:")
    print(f"  Records with parent_id: {df['parent_id'].notna().sum()}")
    print(f"  Records without parent_id (root spans): {df['parent_id'].isna().sum()}")

    # Pick a small session for detailed analysis
    if not df['session_id'].isna().all():
        small_sessions = session_counts[session_counts < 50].head(5)
        print(f"\n🔬 Small sessions (good for testing):")
        for session, count in small_sessions.items():
            print(f"  {session}: {count} records")

        # Show details of smallest session
        if len(small_sessions) > 0:
            smallest_session = small_sessions.index[0]
            print(f"\n🎯 Analyzing session: {smallest_session}")
            session_df = df[df['session_id'] == smallest_session].copy()
            session_df = session_df.sort_values('start_time')

            for _, row in session_df.head(10).iterrows():
                print(f"  [{row['start_time']}] {row['name']} ({row['attributes.openinference.span.kind']}) - trace:{row['context.trace_id'][:8]}, span:{row['context.span_id'][:8]}")

            # Now reconstruct the full session thread
            print(f"\n🧵 Reconstructing session thread for {smallest_session}...")

            # Get all records for this session
            session_df = df[df['session_id'] == smallest_session].copy()

            # Sort by start_time for chronological order
            session_df = session_df.sort_values('start_time')

            print(f"\nSession has {len(session_df)} records")
            print(f"\nDetailed breakdown:")

            for idx, row in session_df.iterrows():
                kind = row['attributes.openinference.span.kind']
                name = row['name']
                trace_id = row['context.trace_id']
                span_id = row['context.span_id']
                parent_id = row['parent_id']
                start_time = row['start_time']
                end_time = row['end_time']

                # Convert timestamps to datetime if needed
                if not isinstance(start_time, pd.Timestamp):
                    start = pd.to_datetime(start_time, unit='ms')
                else:
                    start = start_time

                if not isinstance(end_time, pd.Timestamp):
                    end = pd.to_datetime(end_time, unit='ms')
                else:
                    end = end_time

                # Calculate duration in seconds
                duration = (end - start).total_seconds() if pd.notna(start) and pd.notna(end) else 0

                # Get input/output if available
                input_val = row.get('attributes.input.value', '')
                output_val = row.get('attributes.output.value', '')

                # Get LLM messages if available
                input_messages = row.get('attributes.llm.input_messages', None)
                output_messages = row.get('attributes.llm.output_messages', None)

                print(f"\n{'='*80}")
                print(f"[{start.strftime('%H:%M:%S.%f')[:-3]}] {name}")
                print(f"  Kind: {kind}")
                print(f"  Trace ID: {trace_id[:16]}...")
                print(f"  Span ID: {span_id}")
                print(f"  Parent ID: {parent_id if parent_id else '(root)'}")
                print(f"  Duration: {duration:.3f}s")

                if input_val:
                    input_str = str(input_val)
                    print(f"  Input ({len(input_str)} chars): {input_str[:500]}...")
                    if len(input_str) > 500:
                        print(f"    [truncated {len(input_str) - 500} more chars]")
                if output_val:
                    output_str = str(output_val)
                    print(f"  Output ({len(output_str)} chars): {output_str[:500]}...")
                    if len(output_str) > 500:
                        print(f"    [truncated {len(output_str) - 500} more chars]")

                if input_messages and isinstance(input_messages, list) and len(input_messages) > 0:
                    print(f"  Input Messages: {len(input_messages)} message(s)")
                    for i, msg in enumerate(input_messages[:2]):  # Show first 2
                        if isinstance(msg, dict):
                            role = msg.get('message.role', 'unknown')
                            content = msg.get('message.content', '')
                            content_str = str(content)
                            print(f"    [{i+1}] Role: {role}")
                            print(f"        Content ({len(content_str)} chars): {content_str[:800]}")
                            if len(content_str) > 800:
                                print(f"        [truncated {len(content_str) - 800} more chars]")

                if output_messages and isinstance(output_messages, str):
                    print(f"  Output Messages: {output_messages[:200]}...")

            # Build parent-child tree
            print(f"\n\n🌳 Span Tree Structure:")

            def print_tree(parent_id, indent=0):
                children = session_df[session_df['parent_id'] == parent_id]
                for _, child in children.sort_values('start_time').iterrows():
                    prefix = "  " * indent + "└─ "
                    print(f"{prefix}{child['name']} ({child['attributes.openinference.span.kind']}) - {child['context.span_id'][:8]}")
                    print_tree(child['context.span_id'], indent + 1)

            # Start with root spans (no parent)
            roots = session_df[session_df['parent_id'].isna()]
            for _, root in roots.sort_values('start_time').iterrows():
                print(f"ROOT: {root['name']} ({root['attributes.openinference.span.kind']}) - {root['context.span_id'][:8]}")
                print_tree(root['context.span_id'], 1)


if __name__ == '__main__':
    main()
