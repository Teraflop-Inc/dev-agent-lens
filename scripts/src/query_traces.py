#!/usr/bin/env python3
"""
Query Claude Code Traces from Phoenix

Search for Claude Code sessions and group results by session ID.

Usage:
    uv run main.py query --search "CWORK-797"
    uv run main.py query --session-id <session-id>
    uv run main.py query --search "linear" --export results.json
"""

import argparse
import os
import sys
import json
from typing import Dict, List

try:
    import phoenix as px
    from phoenix.trace.dsl import SpanQuery
    import pandas as pd
    from dotenv import load_dotenv
except ImportError as e:
    print(f"❌ Missing required package: {e}")
    print("\nInstall with: cd scripts && uv sync")
    sys.exit(1)

load_dotenv()


def get_phoenix_client():
    """Get Phoenix client."""
    phoenix_url = os.getenv("PHOENIX_BASE_URL", "http://98.149.54.126:6106")
    print(f"🔗 Connecting to Phoenix at {phoenix_url}")

    try:
        # Phoenix client uses endpoint parameter (or reads from PHOENIX_COLLECTOR_ENDPOINT)
        os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = phoenix_url
        return px.Client()
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        sys.exit(1)


def query_by_search(client, search_string: str, project_name: str = "dev-agent-lens") -> pd.DataFrame:
    """Query traces containing search string (client-side filtering)."""
    print(f"🔍 Searching for: '{search_string}'")
    print(f"📦 Project: {project_name}")

    try:
        # Fetch all spans from project
        print("📥 Fetching spans from Phoenix...")
        df = client.get_spans_dataframe(
            project_name=project_name,
            timeout=120,
            limit=10000  # Fetch up to 10k spans
        )

        if df.empty:
            print("❌ No spans found in project")
            return df

        print(f"✅ Retrieved {len(df)} spans")

        # Client-side filter for search string
        print(f"🔍 Filtering for '{search_string}'...")
        search_lower = search_string.lower()
        mask = pd.Series([False] * len(df), index=df.index)

        # Search in input and output columns (use attributes.* column names)
        search_columns = ['attributes.input.value', 'attributes.output.value']
        for col in search_columns:
            if col in df.columns:
                mask |= df[col].astype(str).str.lower().str.contains(search_lower, na=False, regex=False)

        filtered = df[mask]

        if filtered.empty:
            print(f"❌ No traces found containing: '{search_string}'")
        else:
            print(f"✅ Found {len(filtered)} matching span(s)")

        return filtered

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def query_by_session_id(client, session_id: str, project_name: str = "dev-agent-lens") -> pd.DataFrame:
    """Query traces by exact session ID (client-side filtering)."""
    print(f"🔍 Querying for session_id: {session_id}")
    print(f"📦 Project: {project_name}")

    try:
        # Fetch all spans from project
        print("📥 Fetching spans from Phoenix...")
        df = client.get_spans_dataframe(
            project_name=project_name,
            timeout=120,
            limit=10000  # Fetch up to 10k spans
        )

        if df.empty:
            print("❌ No spans found in project")
            return df

        print(f"✅ Retrieved {len(df)} spans")

        # Filter by trace_id (session ID in Phoenix)
        print(f"🔍 Filtering for trace_id (session): '{session_id}'...")
        if 'context.trace_id' in df.columns:
            filtered = df[df['context.trace_id'] == session_id]
        else:
            filtered = pd.DataFrame()

        if filtered.empty:
            print(f"❌ No traces found for session: {session_id}")
        else:
            print(f"✅ Found {len(filtered)} span(s)")

        return filtered

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def extract_session_id(row) -> str:
    """
    Extract Claude Code session ID from metadata fields.

    Checks multiple locations for session ID with 'session_' pattern:
    - metadata.user_id
    - metadata.user_api_key_end_user_id
    - metadata.requester_metadata.user_id
    """
    metadata = row.get('attributes.metadata')

    if not metadata or not isinstance(metadata, dict):
        # Fallback to trace_id if no metadata
        return row.get('context.trace_id', 'unknown')

    # Try Phoenix format: metadata.user_id with _session_ pattern
    user_id = metadata.get('user_id')
    if user_id and 'session_' in str(user_id):
        return str(user_id).split('session_')[-1]

    # Try Arize format: user_api_key_end_user_id
    user_id = metadata.get('user_api_key_end_user_id')
    if user_id and 'session_' in str(user_id):
        return str(user_id).split('session_')[-1]

    # Also try requester_metadata.user_id (Arize format)
    req_meta = metadata.get('requester_metadata', {})
    if isinstance(req_meta, dict):
        user_id = req_meta.get('user_id')
        if user_id and 'session_' in str(user_id):
            return str(user_id).split('session_')[-1]

    # Fallback to trace_id
    return row.get('context.trace_id', 'unknown')


def group_by_session(df: pd.DataFrame) -> Dict[str, List[Dict]]:
    """
    Group traces by Claude Code session ID.

    Extracts session ID from metadata fields, falls back to trace_id.
    """
    if df.empty:
        return {}

    # Extract session ID for each row
    df['session_id'] = df.apply(extract_session_id, axis=1)

    # Group by session ID
    grouped = {}
    for session_id, group_df in df.groupby('session_id'):
        if pd.notna(session_id):
            grouped[str(session_id)] = group_df.to_dict('records')

    return grouped


def print_results(grouped: Dict[str, List[Dict]], verbose: bool = False):
    """Print results."""
    if not grouped:
        print("\n❌ No results")
        return

    total = sum(len(traces) for traces in grouped.values())
    print(f"\n✅ Found {total} trace(s) across {len(grouped)} session(s)")
    print("\n📊 Sessions:")

    for session_id, traces in grouped.items():
        print(f"  {session_id}: {len(traces)} trace(s)")

    if verbose:
        print("\n" + "="*80)
        for session_id, traces in grouped.items():
            print(f"\nSession: {session_id}")
            for i, trace in enumerate(traces[:3], 1):  # Show first 3
                print(f"  Trace {i}:")
                print(f"    Span: {trace.get('span_id', 'N/A')[:16]}...")
                print(f"    Name: {trace.get('name', 'N/A')}")
            if len(traces) > 3:
                print(f"  ... and {len(traces) - 3} more")
    else:
        print("\n💡 Use --verbose for details")


def export_results(grouped: Dict[str, List[Dict]], output_path: str):
    """Export to JSON."""
    if not grouped:
        print("⚠️  No data to export")
        return

    try:
        with open(output_path, 'w') as f:
            json.dump(grouped, f, indent=2, default=str)

        total = sum(len(traces) for traces in grouped.values())
        print(f"\n💾 Exported to: {output_path}")
        print(f"   Sessions: {len(grouped)}, Traces: {total}")
    except Exception as e:
        print(f"❌ Export error: {e}")


def parse_args():
    parser = argparse.ArgumentParser(description="Query Claude Code traces from Phoenix")
    parser.add_argument("--search", help="Search string in trace content")
    parser.add_argument("--session-id", help="Query by session ID")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--export", help="Export to JSON file")
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.search and not args.session_id:
        print("❌ Must specify --search or --session-id")
        sys.exit(1)

    client = get_phoenix_client()

    # Query
    if args.session_id:
        df = query_by_session_id(client, args.session_id)
    else:
        df = query_by_search(client, args.search)

    # Group and display
    grouped = group_by_session(df)
    print_results(grouped, args.verbose)

    if args.export:
        export_results(grouped, args.export)


if __name__ == "__main__":
    main()
