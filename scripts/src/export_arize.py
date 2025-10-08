#!/usr/bin/env python3
"""
Export Arize Trace Data Script

This script exports trace data from Arize AX platform for the Dev-Agent-Lens project.
It supports both date range filtering and exporting all available data.

Environment Variables Required:
    ARIZE_API_KEY: Your Arize API key
    ARIZE_SPACE_KEY: Your Arize space key (space_id)
    ARIZE_MODEL_ID: Model ID in Arize (default: 'dev-agent-lens')

Usage Examples:
    # Export data for today (default - JSONL format)
    uv run python scripts/export_arize_data.py

    # Export all available data
    uv run python scripts/export_arize_data.py --all

    # Export data for a custom date range
    uv run python scripts/export_arize_data.py --start-date 2025-10-01 --end-date 2025-10-06

    # Export to CSV format
    uv run python scripts/export_arize_data.py --output traces.csv --format csv

    # Export as Parquet
    uv run python scripts/export_arize_data.py --output traces.parquet --format parquet
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from arize.exporter import ArizeExportClient
    from arize.utils.types import Environments
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
        description="Export trace data from Arize AX for Dev-Agent-Lens project",
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
        default='arize_traces.jsonl',
        help='Output file path (default: arize_traces.jsonl)'
    )

    parser.add_argument(
        '--format',
        type=str,
        choices=['jsonl', 'csv', 'parquet'],
        default='jsonl',
        help='Output format (default: jsonl)'
    )

    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite existing files (skip cache, re-download data)'
    )

    return parser.parse_args()


def load_environment():
    """Load and validate environment variables."""
    # Load from .env file if it exists (check scripts/.env first, then root .env)
    script_env = Path(__file__).parent / '.env'
    root_env = Path(__file__).parent.parent / '.env'

    if script_env.exists():
        load_dotenv(script_env)
    elif root_env.exists():
        load_dotenv(root_env)

    # Validate required environment variables
    api_key = os.getenv('ARIZE_API_KEY')
    space_key = os.getenv('ARIZE_SPACE_KEY')
    model_id = os.getenv('ARIZE_MODEL_ID', 'dev-agent-lens')

    if not api_key:
        print("❌ Error: ARIZE_API_KEY environment variable is not set")
        print("\nPlease set your Arize API key:")
        print("  export ARIZE_API_KEY='your-api-key-here'")
        print("\nOr add it to your .env file:")
        print("  ARIZE_API_KEY=your-api-key-here")
        sys.exit(1)

    if not space_key:
        print("❌ Error: ARIZE_SPACE_KEY environment variable is not set")
        print("\nPlease set your Arize space key:")
        print("  export ARIZE_SPACE_KEY='your-space-key-here'")
        print("\nOr add it to your .env file:")
        print("  ARIZE_SPACE_KEY=your-space-key-here")
        sys.exit(1)

    return api_key, space_key, model_id


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


def classify_span(row):
    """Classify span type for filtering."""
    input_val = row.get('attributes.input.value', '')
    output_val = row.get('attributes.output.value', '')
    span_name = row.get('name', '')
    span_kind = row.get('attributes.openinference.span.kind', '')
    model_name = row.get('attributes.llm.model_name', '')

    # Get LLM messages if available (Arize stores them differently)
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
    if model_name and 'haiku' in str(model_name).lower():
        return 'haiku_holdover'

    # LiteLLM test calls → system overhead
    if 'test' in all_text and 'litellm' in all_text.lower():
        return 'litellm_system_overhead'

    # "Is new topic" calls → ancillary
    if 'new topic' in all_text or 'is_new_topic' in all_text:
        return 'is_new_topic'

    # Incomplete LLM requests (has input but no output)
    if span_kind == 'LLM' and span_name == 'litellm_request':
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
    """Export trace data from Arize."""
    output_path = Path(args.output)
    raw_file = output_path.parent / f"{output_path.stem}_raw{output_path.suffix}"

    # Check if raw file already exists (unless --overwrite is specified)
    if raw_file.exists() and not args.overwrite:
        print(f"✅ Found existing raw data: {raw_file}")
        print(f"⏭️  Skipping download, loading from file...")
        print(f"   (Use --overwrite to re-download)")
        start_time = time.time()
        df = pd.read_json(raw_file, lines=True)
        load_duration = time.time() - start_time
        print(f"✅ Loaded {len(df)} records in {load_duration:.2f}s")
    else:
        if args.overwrite and raw_file.exists():
            print(f"🗑️  Overwrite mode: deleting existing raw file")
        # Load environment
        api_key, space_key, model_id = load_environment()

        # Initialize Arize export client with explicit API key
        print("🔄 Initializing Arize export client...")
        client = ArizeExportClient(api_key=api_key)

        # Prepare export parameters
        export_params = {
            'space_id': space_key,
            'model_id': model_id,
            'environment': Environments.TRACING,
        }

        # Configure date range
        if not args.all:
            if args.start_date:
                start_date = parse_date(args.start_date)
            else:
                # Default to today
                start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

            if args.end_date:
                end_date = parse_date(args.end_date)
            else:
                # Default to end of start date
                end_date = start_date + timedelta(days=1)

            export_params['start_time'] = start_date
            export_params['end_time'] = end_date

            print(f"📅 Exporting traces from {start_date.date()} to {end_date.date()}")
        else:
            print("📅 Exporting all available traces")

        # Export data
        print(f"🔄 Fetching trace data from Arize (model_id: {model_id})...")
        start_time = time.time()
        try:
            df = client.export_model_to_df(**export_params)
            fetch_duration = time.time() - start_time

            if df.empty:
                print("⚠️  No trace data found for the specified criteria")
                print("\nTroubleshooting:")
                print(f"  - Verify model_id '{model_id}' exists in Arize")
                print(f"  - Check date range contains data")
                print(f"  - Ensure traces are being sent to Arize from Dev-Agent-Lens")
                return

            print(f"✅ Retrieved {len(df)} trace records in {fetch_duration:.2f}s")

            # Save raw file
            print(f"💾 Saving raw data to: {raw_file.absolute()}")
            if args.format == 'parquet':
                df.to_parquet(raw_file, index=False)
            elif args.format == 'csv':
                df.to_csv(raw_file, index=False)
            else:  # jsonl
                df.to_json(raw_file, orient='records', lines=True)

        except Exception as e:
            print(f"❌ Error exporting data: {e}")
            print("\nTroubleshooting:")
            print("  - Verify ARIZE_API_KEY and ARIZE_SPACE_KEY are correct")
            print("  - Check that the model_id exists in Arize")
            print("  - Ensure you have access to the Arize space")
            sys.exit(1)

    # Classify and split data
    print(f"\n🔍 Classifying spans...")
    start_time = time.time()
    df['classification'] = df.apply(classify_span, axis=1)

    classification_counts = df['classification'].value_counts()
    print(f"✅ Classification complete:")
    for classification, count in classification_counts.items():
        print(f"  {classification}: {count} records")

    # Save datasets by classification
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

        # Show breakdown of ancillary types
        for classification, count in ancillary_df['classification'].value_counts().items():
            print(f"    - {classification}: {count} records")

    save_duration = time.time() - save_start
    total_duration = time.time() - start_time

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


def main():
    """Main entry point."""
    args = parse_args()
    export_traces(args)


if __name__ == '__main__':
    main()
