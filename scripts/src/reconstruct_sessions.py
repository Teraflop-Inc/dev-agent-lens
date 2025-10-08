#!/usr/bin/env python3
"""
Reconstruct session threads from trace data using session IDs.

Supports both Arize and Phoenix trace data.

This script groups spans by their session ID found in metadata:
- Phoenix: metadata.user_id with pattern _session_<id>
- Arize: user_api_key_end_user_id or requester_metadata.user_id with pattern session_<id>

Usage:
    # Auto-detect trace file from phoenix/ or arize/ folders
    uv run main.py reconstruct

    # Specify file path
    uv run main.py reconstruct phoenix/phoenix_traces.jsonl

    # Custom output path
    uv run main.py reconstruct --output sessions.jsonl
"""
import json
import pandas as pd
from datetime import timedelta
from typing import Dict, List, Any
import argparse
from pathlib import Path


def load_trace_data(filepath: str) -> pd.DataFrame:
    """Load JSONL trace data into a DataFrame, including tools file if it exists."""
    df = pd.read_json(filepath, lines=True)

    # Also load tools file if it exists (to get complete session)
    tools_path = Path(filepath).parent / Path(filepath).name.replace('traces.jsonl', 'traces_tools.jsonl')
    if tools_path.exists():
        print(f"  📎 Also loading tools: {tools_path.name}")
        tools_df = pd.read_json(tools_path, lines=True)
        df = pd.concat([df, tools_df], ignore_index=True)
        print(f"  ✅ Combined: {len(df)} total spans")

    # Convert timestamps to datetime
    if 'start_time' in df.columns:
        df['start_time'] = pd.to_datetime(df['start_time'], unit='ms', errors='coerce')
    if 'end_time' in df.columns:
        df['end_time'] = pd.to_datetime(df['end_time'], unit='ms', errors='coerce')

    # Sort by time
    df = df.sort_values('start_time')

    return df


def extract_session_id(metadata) -> str | None:
    """Extract session ID from metadata if available."""
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


def reconstruct_by_session_id(df: pd.DataFrame) -> List[pd.DataFrame]:
    """Reconstruct sessions by grouping spans with the same session ID."""
    print("\n" + "=" * 80)
    print("SESSION RECONSTRUCTION: Session ID Grouping")
    print("=" * 80)

    # Extract session IDs - check both metadata columns
    if 'metadata' in df.columns:
        df['session_id'] = df['metadata'].apply(extract_session_id)
    elif 'attributes.metadata' in df.columns:
        df['session_id'] = df['attributes.metadata'].apply(extract_session_id)
    else:
        print("❌ No metadata column found in dataframe")
        return []

    # Check how many have session IDs
    has_session = df['session_id'].notna().sum()
    total = len(df)
    print(f"\nSpans with session ID: {has_session}/{total}")

    if has_session == 0:
        print("❌ No spans have session IDs. Cannot reconstruct sessions.")
        return []

    # Group by session ID
    sessions = []
    for session_id, session_df in df[df['session_id'].notna()].groupby('session_id'):
        sessions.append(session_df.copy())

    print(f"\nDetected {len(sessions)} sessions")

    # Analyze each session
    for i, session_df in enumerate(sessions[:10]):  # Limit to first 10
        session_id = session_df['session_id'].iloc[0]
        print(f"\n{'─' * 80}")
        print(f"SESSION {i+1}: {session_id}")
        print(f"{'─' * 80}")
        print(f"Spans: {len(session_df)}")

        # Time range
        start = session_df['start_time'].min()
        end = session_df['end_time'].max()
        duration = (end - start).total_seconds() if pd.notna(start) and pd.notna(end) else 0
        print(f"Duration: {duration:.2f}s ({start} to {end})")

        # Span types
        span_kinds = session_df['attributes.openinference.span.kind'].value_counts()
        print(f"\nSpan types:")
        for kind, count in span_kinds.items():
            print(f"  {kind}: {count}")

        # Unique traces in this session
        traces = session_df['context.trace_id'].nunique()
        print(f"Unique traces: {traces}")

        # Show timeline with input/output
        print(f"\n📋 Timeline:")
        for idx, row in session_df.iterrows():
            span_time = row['start_time'].strftime('%H:%M:%S.%f')[:-3]
            span_name = row.get('name', 'unknown')
            span_kind = row.get('attributes.openinference.span.kind', '')
            span_id = row.get('context.span_id', '')[:8]
            parent_id = row.get('parent_id', '')

            # Get duration
            span_start = row['start_time']
            span_end = row['end_time']
            span_duration = (span_end - span_start).total_seconds() if pd.notna(span_start) and pd.notna(span_end) else 0

            print(f"\n  [{span_time}] {span_name} ({span_kind}) - {span_id} [{span_duration:.2f}s]")
            if parent_id and isinstance(parent_id, str):
                print(f"    Parent: {parent_id[:8]}")

            # Get input/output
            input_val = row.get('attributes.input.value', '')
            output_val = row.get('attributes.output.value', '')
            input_msgs = row.get('attributes.llm.input_messages', None)
            output_msgs = row.get('attributes.llm.output_messages', None)

            # Show input
            if input_val:
                input_str = str(input_val)
                preview = input_str[:200].replace('\n', ' ')
                print(f"    📥 Input ({len(input_str)} chars): {preview}{'...' if len(input_str) > 200 else ''}")
            elif input_msgs and isinstance(input_msgs, list) and len(input_msgs) > 0:
                print(f"    📥 Input Messages: {len(input_msgs)} message(s)")
                for msg_idx, msg in enumerate(input_msgs[:2]):
                    if isinstance(msg, dict):
                        role = msg.get('message.role', 'unknown')
                        content = str(msg.get('message.content', ''))[:150].replace('\n', ' ')
                        print(f"       [{msg_idx+1}] {role}: {content}...")

            # Show output
            if output_val:
                output_str = str(output_val)
                preview = output_str[:200].replace('\n', ' ')
                print(f"    📤 Output ({len(output_str)} chars): {preview}{'...' if len(output_str) > 200 else ''}")
            elif output_msgs and isinstance(output_msgs, str):
                preview = str(output_msgs)[:200].replace('\n', ' ')
                print(f"    📤 Output: {preview}...")

    return sessions


def save_reconstructed_sessions(conversations: List[pd.DataFrame], output_path: Path) -> None:
    """Save reconstructed sessions to JSONL file."""
    print(f"\n💾 Saving {len(conversations)} reconstructed sessions to: {output_path}")

    # Create output directory if needed
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sessions_data = []
    for i, conv_df in enumerate(conversations):
        # Convert timestamps back to ISO format for JSON serialization
        conv_df_copy = conv_df.copy()
        if 'start_time' in conv_df_copy.columns:
            conv_df_copy['start_time'] = conv_df_copy['start_time'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')
        if 'end_time' in conv_df_copy.columns:
            conv_df_copy['end_time'] = conv_df_copy['end_time'].dt.strftime('%Y-%m-%d %H:%M:%S.%f')

        session = {
            'session_number': i + 1,
            'span_count': len(conv_df),
            'start_time': str(conv_df['start_time'].min()),
            'end_time': str(conv_df['end_time'].max()),
            'duration_seconds': (conv_df['end_time'].max() - conv_df['start_time'].min()).total_seconds(),
            'unique_traces': conv_df['context.trace_id'].nunique(),
            'spans': conv_df_copy.to_dict('records')
        }
        sessions_data.append(session)

    # Write to JSONL
    with open(output_path, 'w') as f:
        for session in sessions_data:
            f.write(json.dumps(session) + '\n')

    # Get file size
    file_size = output_path.stat().st_size
    size_kb = file_size / 1024
    print(f"✅ Saved {len(sessions_data)} sessions ({size_kb:.1f} KB)")
    print(f"\nEach line in the JSONL file contains:")
    print(f"  - session_number: Session identifier")
    print(f"  - span_count: Number of spans in this session")
    print(f"  - start_time/end_time: Time range")
    print(f"  - duration_seconds: Total duration")
    print(f"  - unique_traces: Number of unique trace IDs")
    print(f"  - spans: Array of all span data")


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


def main():
    parser = argparse.ArgumentParser(
        description='Reconstruct sessions from trace data using session IDs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        'input_file',
        nargs='?',
        help='Path to input JSONL file (default: auto-detect from arize/ or phoenix/)'
    )
    parser.add_argument(
        '--output',
        type=str,
        help='Output file path for reconstructed sessions (default: auto-detect based on input)'
    )

    args = parser.parse_args()

    # Determine input file
    if args.input_file:
        input_file = Path(args.input_file)
    else:
        input_file = find_trace_file()
        if not input_file:
            print("❌ Error: No trace data found")
            print("\nPlease run the export script first:")
            print("  uv run main.py export")
            print("\nOr specify a file path:")
            print("  uv run main.py reconstruct <path/to/traces.jsonl>")
            return
        print(f"🔍 Auto-detected: {input_file}")

    if not input_file.exists():
        print(f"❌ Error: File not found: {input_file}")
        return

    # Determine output file
    if args.output:
        output_file = Path(args.output)
    else:
        # Auto-detect: phoenix/phoenix_traces.jsonl -> phoenix/phoenix_sessions.jsonl
        output_file = input_file.parent / input_file.name.replace('traces', 'sessions')

    print(f"Loading trace data from: {input_file}")
    df = load_trace_data(str(input_file))
    print(f"Loaded {len(df)} spans")

    # Reconstruct sessions by session ID
    sessions = reconstruct_by_session_id(df)

    # Save reconstructed sessions
    if sessions:
        save_reconstructed_sessions(sessions, output_file)
    else:
        print("\n⚠️  No sessions reconstructed. Check that spans have session IDs in metadata.")


if __name__ == '__main__':
    main()
