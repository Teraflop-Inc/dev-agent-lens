"""
Query Module

Provides search and filtering capabilities for trace data.

Supports two backends:
- JSONL: Original format, works with any Python environment
- Parquet: High-performance columnar format using DuckDB (10-100x faster)

The `query_source()` function auto-selects the best backend based on
available data files.
"""

from dev_agent_lens.query.export import (
    ExportFormat,
    export,
    export_csv,
    export_json,
    export_markdown,
)
from dev_agent_lens.query.parquet_query import (
    find_parquet_files,
    get_parquet_stats,
    query_parquet,
    query_source,
    search_parquet,
)
from dev_agent_lens.query.query import (
    QueryResult,
    query,
    query_file,
    query_sessions,
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
    "query_sessions",
    # Parquet query API (high-performance)
    "query_parquet",
    "query_source",
    "search_parquet",
    "find_parquet_files",
    "get_parquet_stats",
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
