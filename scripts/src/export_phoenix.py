#!/usr/bin/env python3
"""
Export Phoenix Trace Data Script

This script exports trace data from Phoenix for the Dev-Agent-Lens project.
It normalizes the Phoenix schema to match Arize format for consistent analysis.

Environment Variables (Optional):
    PHOENIX_URL: Phoenix server URL (default: http://localhost:6006)
    PHOENIX_PROJECT: Project name in Phoenix (default: claude-code-myproject)

Usage Examples:
    # Export data from today (default - JSONL format)
    uv run export_phoenix_data.py

    # Export all available data
    uv run export_phoenix_data.py --all

    # Export data for a custom date range
    uv run export_phoenix_data.py --start-date 2025-10-07 --end-date 2025-10-08

    # Export to CSV format
    uv run export_phoenix_data.py --output phoenix_traces.csv --format csv
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from phoenix.client import Client
    import pandas as pd
    from dotenv import load_dotenv
except ImportError as e:
    print(f"❌ Missing required package: {e}")
    print("\nPlease install required packages:")
    print("  cd scripts && uv sync")
    sys.exit(1)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Export trace data from Phoenix for Dev-Agent-Lens project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        '--start-date',
        type=str,
        help='Start date in ISO format (YYYY-MM-DD). Default: today'
    )

    parser.add_argument(
        '--end-date',
        type=str,
        help='End date in ISO format (YYYY-MM-DD). Default: end of start date'
    )

    parser.add_argument(
        '--all',
        action='store_true',
        help='Export all available data (ignores date filters)'
    )

    parser.add_argument(
        '--output',
        type=str,
        default='phoenix_traces.jsonl',
        help='Output file path (default: phoenix_traces.jsonl)'
    )

    parser.add_argument(
        '--format',
        type=str,
        choices=['jsonl', 'csv', 'parquet'],
        default='jsonl',
        help='Output format (default: jsonl)'
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=100000,
        help='Maximum number of spans to retrieve (default: 100000)'
    )

    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite existing files (re-export data)'
    )

    return parser.parse_args()


def load_environment():
    """Load and validate environment variables."""
    # Load from .env file if it exists
    script_env = Path(__file__).parent / '.env'
    root_env = Path(__file__).parent.parent / '.env'

    if script_env.exists():
        load_dotenv(script_env)
    elif root_env.exists():
        load_dotenv(root_env)

    # Get Phoenix configuration
    phoenix_url = os.getenv('PHOENIX_URL', 'http://localhost:6006')
    phoenix_project = os.getenv('PHOENIX_PROJECT', 'claude-code-myproject')

    return phoenix_url, phoenix_project


def parse_date(date_str: str) -> datetime:
    """Parse ISO date string to datetime."""
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        print(f"❌ Error: Invalid date format '{date_str}'. Use YYYY-MM-DD format.")
        sys.exit(1)


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def normalize_to_arize_schema(phoenix_df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize Phoenix schema to match Arize schema for consistent analysis.

    Maps Phoenix columns to Arize equivalents:
    - context.span_id → context.span_id (same)
    - context.trace_id → context.trace_id (same)
    - parent_id → parent_id (same)
    - name → name (same)
    - span_kind → attributes.openinference.span.kind (already exists)
    - start_time → start_time (convert to ms timestamp)
    - end_time → end_time (convert to ms timestamp)
    """
    df = phoenix_df.copy()

    # Convert timestamps to milliseconds (Arize format)
    if 'start_time' in df.columns:
        df['start_time'] = pd.to_datetime(df['start_time']).astype('int64') // 10**6
    if 'end_time' in df.columns:
        df['end_time'] = pd.to_datetime(df['end_time']).astype('int64') // 10**6

    return df


def classify_span(row):
    """Classify span type for filtering."""
    input_val = row.get('attributes.input.value', '')
    output_val = row.get('attributes.output.value', '')
    span_name = row.get('name', '')
    span_kind = row.get('attributes.openinference.span.kind', '')
    model_name = row.get('attributes.llm.model_name', '')
    input_msgs = row.get('attributes.llm.input_messages', '')
    output_msgs = row.get('attributes.llm.output_messages', '')

    # Tool-related spans
    if span_kind == 'TOOL':
        return 'tools'

    # Check for tool_use or tool_result in input/output
    input_str = str(input_val) if input_val else ''
    output_str = str(output_val) if output_val else ''

    if 'tool_result' in input_str or 'tool_use' in input_str or 'tool_use' in output_str:
        return 'tools'

    # Combine all text for pattern matching
    all_text = (input_str + output_str + str(input_msgs) + str(output_msgs)).lower()

    # Quota calls → ancillary (check before haiku since quota uses haiku)
    if 'quota' in input_str.lower() and len(input_str) < 50:  # Simple quota check
        return 'quota'
    if 'quota' in str(input_msgs).lower() and 'quota' in str(output_str).lower() and len(str(output_str)) < 10:
        return 'quota'

    # Haiku model calls → holdover (needs further categorization)
    if model_name and 'haiku' in model_name.lower():
        return 'haiku_holdover'

    # LiteLLM test calls → system overhead
    if 'test' in all_text and 'litellm' in all_text.lower():
        return 'litellm_system_overhead'

    # "Is new topic" calls → ancillary
    if 'new topic' in all_text or 'is_new_topic' in all_text:
        return 'is_new_topic'

    # Incomplete LLM requests (has input but no output)
    if span_kind == 'LLM' and 'litellm' in span_name:
        if input_val and (not output_val or output_val == '' or str(output_val).strip() == ''):
            return 'incomplete'

    # Safety classification prompts
    if 'policy_spec' in input_str and 'Claude Code Code Bash' in input_str:
        return 'safety'

    # Title/summarization prompts
    if 'Please write a 5-10 word title' in input_str:
        return 'summarization'

    return 'main'


def export_traces(args):
    """Export trace data from Phoenix."""
    output_path = Path(args.output)

    # Delete existing files if --overwrite is specified
    if args.overwrite:
        # Delete all related output files
        patterns = [
            output_path,
            output_path.parent / f"{output_path.stem}_tools{output_path.suffix}",
            output_path.parent / f"{output_path.stem}_haiku_holdover{output_path.suffix}",
            output_path.parent / f"{output_path.stem}_litellm_overhead{output_path.suffix}",
            output_path.parent / f"{output_path.stem}_ancillary{output_path.suffix}",
        ]
        deleted = []
        for file_path in patterns:
            if file_path.exists():
                file_path.unlink()
                deleted.append(file_path.name)
        if deleted:
            print(f"🗑️  Overwrite mode: deleted {len(deleted)} existing file(s)")
            for name in deleted:
                print(f"    - {name}")

    phoenix_url, phoenix_project = load_environment()

    print(f"🔄 Connecting to Phoenix at {phoenix_url}")
    print(f"📦 Project: {phoenix_project}")

    # Initialize Phoenix client
    try:
        client = Client(base_url=phoenix_url)
    except Exception as e:
        print(f"❌ Failed to connect to Phoenix: {e}")
        print("\nTroubleshooting:")
        print(f"  - Ensure Phoenix is running at {phoenix_url}")
        print(f"  - Check PHOENIX_URL environment variable if using custom URL")
        sys.exit(1)

    # Configure date range
    start_time = None
    end_time = None

    if not args.all:
        if args.start_date:
            start_time = parse_date(args.start_date)
        else:
            # Default to today
            start_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        if args.end_date:
            end_time = parse_date(args.end_date)
        else:
            # Default to end of start date
            end_time = start_time + timedelta(days=1)

        print(f"📅 Exporting traces from {start_time.date()} to {end_time.date()}")
    else:
        print("📅 Exporting all available traces")

    # Export data from Phoenix
    print(f"🔄 Fetching trace data from Phoenix...")
    fetch_start = time.time()

    try:
        df = client.spans.get_spans_dataframe(
            project_name=phoenix_project,
            start_time=start_time,
            end_time=end_time,
            limit=args.limit
        )
        fetch_duration = time.time() - fetch_start

        if df.empty:
            print("⚠️  No trace data found for the specified criteria")
            print("\nTroubleshooting:")
            print(f"  - Verify project '{phoenix_project}' exists in Phoenix")
            print(f"  - Check date range contains data")
            print(f"  - Ensure traces are being sent to Phoenix")
            return

        print(f"✅ Retrieved {len(df)} spans in {fetch_duration:.2f}s")

        # Normalize to Arize schema
        print(f"🔄 Normalizing schema to match Arize format...")
        df = normalize_to_arize_schema(df)

        # Classify and split data
        print(f"🔍 Classifying spans...")
        df['classification'] = df.apply(classify_span, axis=1)

        classification_counts = df['classification'].value_counts()
        print(f"✅ Classification complete:")
        for classification, count in classification_counts.items():
            print(f"  {classification}: {count} records")

        # Save datasets
        output_path = Path(args.output)
        print(f"\n💾 Saving classified datasets...")
        save_start = time.time()

        # Save main dataset
        main_df = df[df['classification'] == 'main'].copy()
        if args.format == 'parquet':
            main_df.to_parquet(output_path, index=False)
        elif args.format == 'csv':
            main_df.to_csv(output_path, index=False)
        else:  # jsonl
            main_df.to_json(output_path, orient='records', lines=True)

        main_size = format_file_size(output_path.stat().st_size)
        print(f"  [main] {len(main_df)} records → {output_path.name} ({main_size})")

        # Save tools dataset
        tools_df = df[df['classification'] == 'tools'].copy()
        if len(tools_df) > 0:
            tools_output = output_path.parent / f"{output_path.stem}_tools{output_path.suffix}"

            if args.format == 'parquet':
                tools_df.to_parquet(tools_output, index=False)
            elif args.format == 'csv':
                tools_df.to_csv(tools_output, index=False)
            else:  # jsonl
                tools_df.to_json(tools_output, orient='records', lines=True)

            tools_size = format_file_size(tools_output.stat().st_size)
            print(f"  [tools] {len(tools_df)} records → {tools_output.name} ({tools_size})")

        # Save haiku_holdover dataset (needs further categorization)
        haiku_df = df[df['classification'] == 'haiku_holdover'].copy()
        if len(haiku_df) > 0:
            haiku_output = output_path.parent / f"{output_path.stem}_haiku_holdover{output_path.suffix}"

            if args.format == 'parquet':
                haiku_df.to_parquet(haiku_output, index=False)
            elif args.format == 'csv':
                haiku_df.to_csv(haiku_output, index=False)
            else:  # jsonl
                haiku_df.to_json(haiku_output, orient='records', lines=True)

            haiku_size = format_file_size(haiku_output.stat().st_size)
            print(f"  [haiku_holdover] {len(haiku_df)} records → {haiku_output.name} ({haiku_size})")

        # Save litellm_system_overhead dataset
        overhead_df = df[df['classification'] == 'litellm_system_overhead'].copy()
        if len(overhead_df) > 0:
            overhead_output = output_path.parent / f"{output_path.stem}_litellm_overhead{output_path.suffix}"

            if args.format == 'parquet':
                overhead_df.to_parquet(overhead_output, index=False)
            elif args.format == 'csv':
                overhead_df.to_csv(overhead_output, index=False)
            else:  # jsonl
                overhead_df.to_json(overhead_output, orient='records', lines=True)

            overhead_size = format_file_size(overhead_output.stat().st_size)
            print(f"  [litellm_overhead] {len(overhead_df)} records → {overhead_output.name} ({overhead_size})")

        # Save ancillary data (quota, is_new_topic, safety, summarization, incomplete)
        ancillary_types = ['quota', 'is_new_topic', 'safety', 'summarization', 'incomplete']
        ancillary_df = df[df['classification'].isin(ancillary_types)].copy()
        if len(ancillary_df) > 0:
            ancillary_output = output_path.parent / f"{output_path.stem}_ancillary{output_path.suffix}"

            if args.format == 'parquet':
                ancillary_df.to_parquet(ancillary_output, index=False)
            elif args.format == 'csv':
                ancillary_df.to_csv(ancillary_output, index=False)
            else:  # jsonl
                ancillary_df.to_json(ancillary_output, orient='records', lines=True)

            ancillary_size = format_file_size(ancillary_output.stat().st_size)
            print(f"  [ancillary] {len(ancillary_df)} records → {ancillary_output.name} ({ancillary_size})")

            # Show breakdown
            for classification, count in ancillary_df['classification'].value_counts().items():
                print(f"    - {classification}: {count} records")

        save_duration = time.time() - save_start
        total_duration = time.time() - fetch_start

        print(f"\n⏱️  Processing time: {save_duration:.2f}s | Total: {total_duration:.2f}s")

        # Print summary
        print(f"\n📊 Final Summary:")
        print(f"  Total records: {len(df)}")
        print(f"  Main dataset (Sonnet): {len(main_df)} records")
        print(f"  Tools dataset: {len(tools_df)} records")
        print(f"  Haiku holdover: {len(haiku_df)} records")
        print(f"  LiteLLM overhead: {len(overhead_df)} records")
        print(f"  Ancillary dataset: {len(ancillary_df)} records")
        if len(ancillary_df) > 0:
            for classification, count in ancillary_df['classification'].value_counts().items():
                print(f"    - {classification}: {count}")
        print(f"  Columns: {len(df.columns)}")

    except Exception as e:
        print(f"❌ Error exporting data: {e}")
        import traceback
        traceback.print_exc()
        print("\nTroubleshooting:")
        print(f"  - Verify Phoenix is running at {phoenix_url}")
        print(f"  - Check that project '{phoenix_project}' exists")
        sys.exit(1)


def main():
    """Main entry point."""
    args = parse_args()
    export_traces(args)


if __name__ == '__main__':
    main()
