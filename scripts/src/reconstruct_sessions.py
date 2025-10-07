#!/usr/bin/env python3
"""
Reconstruct session threads from Arize trace data using parent_id relationships.

This script works around the missing session metadata by:
1. Using trace_id to group related spans
2. Using parent_id to build hierarchical span trees
3. Detecting conversation boundaries via time gaps
"""
import json
import pandas as pd
from datetime import timedelta
from typing import Dict, List, Any
import argparse


def load_trace_data(filepath: str) -> pd.DataFrame:
    """Load JSONL trace data into a DataFrame."""
    df = pd.read_json(filepath, lines=True)

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
    if pd.isna(metadata) or not isinstance(metadata, dict):
        return None

    # Try user_api_key_end_user_id first
    user_id = metadata.get('user_api_key_end_user_id')
    if user_id and 'session_' in user_id:
        return user_id.split('session_')[-1]

    # Also try requester_metadata.user_id
    req_meta = metadata.get('requester_metadata', {})
    if isinstance(req_meta, dict):
        user_id = req_meta.get('user_id')
        if user_id and 'session_' in user_id:
            return user_id.split('session_')[-1]

    return None


def build_span_tree(df: pd.DataFrame, root_span_id: str, indent: int = 0) -> List[str]:
    """Recursively build a tree of spans from parent-child relationships."""
    lines = []

    # Find all children of this span
    children = df[df['parent_id'] == root_span_id].sort_values('start_time')

    for _, child in children.iterrows():
        prefix = "  " * indent + "└─ "
        kind = child.get('attributes.openinference.span.kind', '')
        name = child.get('name', 'unknown')
        span_id = child.get('context.span_id', '')[:8]

        # Get duration
        start = child['start_time']
        end = child['end_time']
        duration = (end - start).total_seconds() if pd.notna(start) and pd.notna(end) else 0

        lines.append(f"{prefix}{name} ({kind}) - {span_id} [{duration:.2f}s]")

        # Recurse for children
        child_lines = build_span_tree(df, child['context.span_id'], indent + 1)
        lines.extend(child_lines)

    return lines


def reconstruct_by_trace_id(df: pd.DataFrame, limit: int = 5) -> None:
    """Reconstruct sessions by grouping spans by trace_id."""
    print("=" * 80)
    print("SESSION RECONSTRUCTION: Trace ID Grouping")
    print("=" * 80)

    # Get unique trace IDs
    trace_ids = df['context.trace_id'].unique()
    print(f"\nTotal unique traces: {len(trace_ids)}")

    # Analyze first N traces
    for i, trace_id in enumerate(trace_ids[:limit]):
        trace_df = df[df['context.trace_id'] == trace_id].copy()
        trace_df = trace_df.sort_values('start_time')

        print(f"\n{'─' * 80}")
        print(f"TRACE {i+1}: {trace_id[:16]}...")
        print(f"{'─' * 80}")
        print(f"Total spans: {len(trace_df)}")

        # Get time range
        start = trace_df['start_time'].min()
        end = trace_df['end_time'].max()
        duration = (end - start).total_seconds() if pd.notna(start) and pd.notna(end) else 0
        print(f"Time range: {start} to {end} ({duration:.2f}s)")

        # Get span types
        span_kinds = trace_df['attributes.openinference.span.kind'].value_counts()
        print(f"\nSpan types:")
        for kind, count in span_kinds.items():
            print(f"  {kind}: {count}")

        # Build tree from root spans
        print(f"\nSpan hierarchy:")
        roots = trace_df[trace_df['parent_id'].isna()]

        for _, root in roots.iterrows():
            root_kind = root.get('attributes.openinference.span.kind', '')
            root_name = root.get('name', 'unknown')
            root_span_id = root.get('context.span_id', '')[:8]

            # Get duration
            root_start = root['start_time']
            root_end = root['end_time']
            root_duration = (root_end - root_start).total_seconds() if pd.notna(root_start) and pd.notna(root_end) else 0

            print(f"ROOT: {root_name} ({root_kind}) - {root_span_id} [{root_duration:.2f}s]")

            # Build tree recursively
            tree_lines = build_span_tree(trace_df, root['context.span_id'], 1)
            for line in tree_lines:
                print(line)


def detect_conversation_boundaries(df: pd.DataFrame, gap_threshold_seconds: int = 300) -> List[pd.DataFrame]:
    """
    Detect conversation boundaries by finding time gaps between spans.

    Args:
        gap_threshold_seconds: Time gap in seconds to consider a new conversation (default 5 minutes)

    Returns:
        List of DataFrames, each representing a potential conversation
    """
    conversations = []
    current_conversation = []
    last_end_time = None

    for idx, row in df.iterrows():
        start_time = row['start_time']

        if last_end_time is None:
            # First span
            current_conversation.append(idx)
        else:
            # Check time gap
            gap = (start_time - last_end_time).total_seconds()

            if gap > gap_threshold_seconds:
                # New conversation detected
                if current_conversation:
                    conversations.append(df.loc[current_conversation].copy())
                current_conversation = [idx]
            else:
                current_conversation.append(idx)

        last_end_time = row['end_time']

    # Add final conversation
    if current_conversation:
        conversations.append(df.loc[current_conversation].copy())

    return conversations


def reconstruct_by_time_windows(df: pd.DataFrame, gap_threshold: int = 300) -> None:
    """Reconstruct sessions by detecting time gaps between spans."""
    print("\n" + "=" * 80)
    print("SESSION RECONSTRUCTION: Time Window Detection")
    print("=" * 80)
    print(f"\nDetecting conversations with {gap_threshold}s gap threshold...")

    conversations = detect_conversation_boundaries(df, gap_threshold)

    print(f"\nDetected {len(conversations)} potential conversations")

    # Analyze each conversation
    for i, conv_df in enumerate(conversations[:10]):  # Limit to first 10
        print(f"\n{'─' * 80}")
        print(f"CONVERSATION {i+1}")
        print(f"{'─' * 80}")
        print(f"Spans: {len(conv_df)}")

        # Time range
        start = conv_df['start_time'].min()
        end = conv_df['end_time'].max()
        duration = (end - start).total_seconds() if pd.notna(start) and pd.notna(end) else 0
        print(f"Duration: {duration:.2f}s ({start} to {end})")

        # Span types
        span_kinds = conv_df['attributes.openinference.span.kind'].value_counts()
        print(f"\nSpan types:")
        for kind, count in span_kinds.items():
            print(f"  {kind}: {count}")

        # Unique traces in this conversation
        traces = conv_df['context.trace_id'].nunique()
        print(f"Unique traces: {traces}")

        # Check for session metadata
        has_session = conv_df['attributes.metadata'].apply(extract_session_id).notna().sum()
        print(f"Spans with session ID: {has_session}/{len(conv_df)}")


def main():
    parser = argparse.ArgumentParser(description='Reconstruct sessions from Arize trace data')
    parser.add_argument(
        'input_file',
        default='../oxen/dev-agent-lens/traces/arize_traces_10-01-2025.jsonl',
        nargs='?',
        help='Path to input JSONL file'
    )
    parser.add_argument(
        '--gap-threshold',
        type=int,
        default=300,
        help='Time gap in seconds to detect new conversations (default: 300)'
    )
    parser.add_argument(
        '--trace-limit',
        type=int,
        default=5,
        help='Number of traces to analyze for trace-based reconstruction (default: 5)'
    )
    parser.add_argument(
        '--method',
        choices=['trace', 'time', 'both'],
        default='both',
        help='Reconstruction method to use (default: both)'
    )

    args = parser.parse_args()

    print(f"Loading trace data from: {args.input_file}")
    df = load_trace_data(args.input_file)
    print(f"Loaded {len(df)} spans")

    if args.method in ['trace', 'both']:
        reconstruct_by_trace_id(df, limit=args.trace_limit)

    if args.method in ['time', 'both']:
        reconstruct_by_time_windows(df, gap_threshold=args.gap_threshold)


if __name__ == '__main__':
    main()
