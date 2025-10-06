#!/usr/bin/env python3
"""
Analyze Arize trace data to understand session structure
"""
import json
import pandas as pd
from collections import Counter

# Load the data
df = pd.read_json('../oxen/dev-agent-lens/traces/arize_traces_10-01-2025.jsonl', lines=True)

print(f"Total records: {len(df)}")
print(f"\nColumns: {len(df.columns)}")

# Extract session IDs from metadata
def extract_session_id(metadata):
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

df['session_id'] = df['attributes.metadata'].apply(extract_session_id)

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
        print(f"\n🎯 Smallest session detail: {smallest_session}")
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
                print(f"  Input: {str(input_val)[:200]}...")
            if output_val:
                print(f"  Output: {str(output_val)[:200]}...")

            if input_messages and isinstance(input_messages, list) and len(input_messages) > 0:
                print(f"  Input Messages: {len(input_messages)} message(s)")
                for msg in input_messages[:2]:  # Show first 2
                    if isinstance(msg, dict):
                        role = msg.get('message.role', 'unknown')
                        content = msg.get('message.content', '')
                        print(f"    [{role}] {str(content)[:150]}...")

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
