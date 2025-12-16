"""
Query Module

Provides search and filtering capabilities for trace data.
"""

from dev_agent_lens.query.regex import (
    RegexSearchError,
    SearchMatch,
    search,
    search_file,
)

__all__ = [
    "RegexSearchError",
    "SearchMatch",
    "search",
    "search_file",
]
