#!/usr/bin/env python3
"""
Dev Agent Lens - Unified CLI

Main entry point for all Dev Agent Lens operations.

Usage:
    # Export trace data (auto-detects Arize or Phoenix)
    uv run main.py export

    # Export with specific options
    uv run main.py export --all --format csv

    # Analyze session reconstruction
    uv run main.py analyze <trace_file>

    # Reconstruct sessions
    uv run main.py reconstruct <trace_file>

    # Compare spans to identify duplication patterns
    uv run main.py compare <session_file>
    uv run main.py compare <session_file> --session 1

    # Get help on any command
    uv run main.py export --help
    uv run main.py analyze --help
    uv run main.py compare --help
"""

import sys
import argparse


def main():
    """Main CLI entry point with subcommands."""
    parser = argparse.ArgumentParser(
        description="Dev Agent Lens - LLM trace analysis tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Export subcommand
    export_parser = subparsers.add_parser(
        'export',
        help='Export trace data from Arize or Phoenix'
    )

    # Analyze subcommand
    analyze_parser = subparsers.add_parser(
        'analyze',
        help='Analyze session reconstruction from trace data'
    )

    # Reconstruct subcommand
    reconstruct_parser = subparsers.add_parser(
        'reconstruct',
        help='Reconstruct sessions from trace data'
    )

    # Compare subcommand
    compare_parser = subparsers.add_parser(
        'compare',
        help='Compare spans to identify duplication and accumulation patterns'
    )

    # Parse just the command
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args, remaining = parser.parse_known_args()

    # Delegate to appropriate script
    if args.command == 'export':
        from src.export_traces import main as export_main
        sys.argv = [sys.argv[0]] + remaining
        export_main()

    elif args.command == 'analyze':
        from src.analyze_sessions import main as analyze_main
        sys.argv = [sys.argv[0]] + remaining
        analyze_main()

    elif args.command == 'reconstruct':
        from src.reconstruct_sessions import main as reconstruct_main
        sys.argv = [sys.argv[0]] + remaining
        reconstruct_main()

    elif args.command == 'compare':
        from src.compare_spans import main as compare_main
        sys.argv = [sys.argv[0]] + remaining
        compare_main()

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
