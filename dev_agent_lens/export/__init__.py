"""
Export module for DAL.

Provides functionality for exporting unified session data with optional
deduplication, null-stripping, and format conversion (JSONL, Parquet, Markdown).
"""

from dev_agent_lens.export.dedupe import (
    deduplicate_session,
    strip_empty_values,
    clean_session,
    DUPLICATED_FIELDS,
    KEEP_FIELDS,
)
from dev_agent_lens.export.parquet import (
    export_to_parquet,
    ParquetExporter,
)
from dev_agent_lens.export.markdown import (
    export_session_to_markdown,
    export_to_files,
    MarkdownExport,
    SessionMessage,
    parse_jsonl_file,
)

__all__ = [
    "deduplicate_session",
    "strip_empty_values",
    "clean_session",
    "DUPLICATED_FIELDS",
    "KEEP_FIELDS",
    "export_to_parquet",
    "ParquetExporter",
    # Markdown export
    "export_session_to_markdown",
    "export_to_files",
    "MarkdownExport",
    "SessionMessage",
    "parse_jsonl_file",
]
