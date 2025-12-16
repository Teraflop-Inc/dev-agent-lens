"""
Query Module

Provides search and filtering capabilities for trace data.
"""

from dev_agent_lens.query.query import (
    QueryResult,
    query,
    query_file,
)
from dev_agent_lens.query.regex import (
    RegexSearchError,
    SearchMatch,
    search,
    search_file,
)

__all__ = [
    # Query API
    "QueryResult",
    "query",
    "query_file",
    # Regex search
    "RegexSearchError",
    "SearchMatch",
    "search",
    "search_file",
]
