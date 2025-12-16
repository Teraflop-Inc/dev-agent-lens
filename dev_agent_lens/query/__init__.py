"""
Query Module

Provides search and filtering capabilities for trace data.
"""

from dev_agent_lens.query.export import (
    ExportFormat,
    export,
    export_csv,
    export_json,
    export_markdown,
)
from dev_agent_lens.query.query import (
    QueryResult,
    query,
    query_file,
)
from dev_agent_lens.query.regex import (
    DEFAULT_SEARCH_FIELDS,
    VALID_SEARCH_FIELDS,
    InvalidFieldError,
    RegexSearchError,
    SearchMatch,
    search,
    search_file,
    validate_fields,
)

__all__ = [
    # Query API
    "QueryResult",
    "query",
    "query_file",
    # Export formats
    "ExportFormat",
    "export",
    "export_csv",
    "export_json",
    "export_markdown",
    # Regex search
    "DEFAULT_SEARCH_FIELDS",
    "VALID_SEARCH_FIELDS",
    "InvalidFieldError",
    "RegexSearchError",
    "SearchMatch",
    "search",
    "search_file",
    "validate_fields",
]
