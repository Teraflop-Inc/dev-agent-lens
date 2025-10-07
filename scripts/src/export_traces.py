#!/usr/bin/env python3
"""
Unified Trace Data Export Script

Auto-detects and exports from either Arize or Phoenix based on environment variables.
- If PHOENIX_URL or PHOENIX_PROJECT is set → uses Phoenix
- If ARIZE_API_KEY is set → uses Arize
- Otherwise → errors

Usage Examples:
    # Export from Phoenix (if PHOENIX_URL/PHOENIX_PROJECT is set)
    uv run export_traces.py

    # Export from Arize (if ARIZE_API_KEY is set)
    uv run export_traces.py --all

    # Force specific backend
    uv run export_traces.py --backend phoenix
    uv run export_traces.py --backend arize
"""

import argparse
import os
import sys
from pathlib import Path

# Determine which backend modules we need
try:
    from dotenv import load_dotenv
    import pandas as pd
except ImportError as e:
    print(f"❌ Missing required package: {e}")
    print("\nPlease install required packages:")
    print("  cd scripts && uv sync")
    sys.exit(1)


def load_environment():
    """Load environment variables and detect backend."""
    script_env = Path(__file__).parent / '.env'
    root_env = Path(__file__).parent.parent / '.env'

    if script_env.exists():
        load_dotenv(script_env)
    elif root_env.exists():
        load_dotenv(root_env)

    # Detect backend
    has_phoenix = bool(os.getenv('PHOENIX_URL') or os.getenv('PHOENIX_PROJECT'))
    has_arize = bool(os.getenv('ARIZE_API_KEY'))

    return has_phoenix, has_arize


def main():
    """Main entry point - delegates to appropriate backend."""
    parser = argparse.ArgumentParser(
        description="Export trace data from Arize or Phoenix (auto-detected)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        '--backend',
        type=str,
        choices=['arize', 'phoenix', 'auto'],
        default='auto',
        help='Force specific backend (default: auto-detect)'
    )

    # Parse just the backend arg first
    args, remaining = parser.parse_known_args()

    # Detect backend
    has_phoenix, has_arize = load_environment()

    backend = args.backend
    if backend == 'auto':
        if has_phoenix:
            backend = 'phoenix'
        elif has_arize:
            backend = 'arize'
        else:
            print("❌ No observability backend configured!")
            print("\nPlease set environment variables in .env:")
            print("\nFor Phoenix:")
            print("  PHOENIX_URL=http://localhost:6006")
            print("  PHOENIX_PROJECT=claude-code-myproject")
            print("\nFor Arize:")
            print("  ARIZE_API_KEY=your-api-key")
            print("  ARIZE_SPACE_KEY=your-space-key")
            sys.exit(1)

    # Import and run appropriate backend
    print(f"🔍 Using backend: {backend.upper()}")

    # Set default output path based on backend if not specified
    if '--output' not in remaining:
        if backend == 'phoenix':
            remaining.extend(['--output', 'phoenix/phoenix_traces.jsonl'])
        else:
            remaining.extend(['--output', 'arize/arize_traces.jsonl'])

    if backend == 'phoenix':
        from src.export_phoenix import main as phoenix_main
        # Update sys.argv to pass remaining args
        sys.argv = [sys.argv[0]] + remaining
        phoenix_main()
    else:  # arize
        from src.export_arize import main as arize_main
        sys.argv = [sys.argv[0]] + remaining
        arize_main()


if __name__ == '__main__':
    main()
