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
    # Export data for Oct 1, 2025 (default)
    uv run python scripts/export_arize_data.py

    # Export all available data
    uv run python scripts/export_arize_data.py --all

    # Export data for a custom date range
    uv run python scripts/export_arize_data.py --start-date 2025-10-01 --end-date 2025-10-06

    # Export to a specific file
    uv run python scripts/export_arize_data.py --output traces_oct.csv

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
        help='Start date in ISO format (YYYY-MM-DD). Default: 2025-10-01'
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
        default='arize_traces.csv',
        help='Output file path (default: arize_traces.csv)'
    )

    parser.add_argument(
        '--format',
        type=str,
        choices=['csv', 'parquet'],
        default='csv',
        help='Output format (default: csv)'
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


def export_traces(args):
    """Export trace data from Arize."""
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
            # Default to Oct 1, 2025
            start_date = datetime(2025, 10, 1)

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

        # Save to file
        output_path = Path(args.output)
        save_start = time.time()

        if args.format == 'parquet':
            df.to_parquet(output_path, index=False)
        else:
            df.to_csv(output_path, index=False)

        save_duration = time.time() - save_start
        total_duration = time.time() - start_time

        print(f"💾 Exported data to: {output_path.absolute()}")
        print(f"⏱️  Save time: {save_duration:.2f}s | Total time: {total_duration:.2f}s")

        # Print summary
        print(f"\n📊 Data Summary:")
        print(f"  Total records: {len(df)}")
        print(f"  Columns: {', '.join(df.columns.tolist()[:5])}{'...' if len(df.columns) > 5 else ''}")
        print(f"  File size: {format_file_size(output_path.stat().st_size)}")

    except Exception as e:
        print(f"❌ Error exporting data: {e}")
        print("\nTroubleshooting:")
        print("  - Verify ARIZE_API_KEY and ARIZE_SPACE_KEY are correct")
        print("  - Check that the model_id exists in Arize")
        print("  - Ensure you have access to the Arize space")
        sys.exit(1)


def main():
    """Main entry point."""
    args = parse_args()
    export_traces(args)


if __name__ == '__main__':
    main()
