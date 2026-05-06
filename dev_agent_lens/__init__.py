"""
dev_agent_lens - Unified trace analysis toolkit for Claude Code observability.

This package provides tools for fetching, normalizing, querying, and analyzing
trace data from Phoenix and Arize backends.
"""

__version__ = "0.1.0"

from dev_agent_lens.patterns import (
    PatternMatch,
    extract_patterns,
    extract_unique_matches,
    group_matches_by_session,
    group_matches_by_value,
)

__all__ = [
    "PatternMatch",
    "extract_patterns",
    "extract_unique_matches",
    "group_matches_by_session",
    "group_matches_by_value",
]
