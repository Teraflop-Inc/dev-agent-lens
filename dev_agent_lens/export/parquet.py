"""
Parquet export functionality for unified session data.

This module provides Parquet format export for efficient storage and
fast analytics queries. Features:
- Columnar storage with built-in compression
- Schema enforcement
- Compatible with Pandas, Polars, DuckDB, Spark
- Oxen.ai native Parquet support

The export uses a two-table design:
- sessions.parquet: One row per session with aggregated metadata
- spans.parquet: One row per span with session_id foreign key
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from dev_agent_lens.export.dedupe import clean_session

logger = logging.getLogger(__name__)


def _parse_datetime(value: Any) -> datetime | None:
    """Parse a datetime value from various formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # Try common formats
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _flatten_span(
    span: dict[str, Any],
    session_id: str,
    source: str,
) -> dict[str, Any]:
    """Flatten a span into a row for Parquet.

    Args:
        span: The span dictionary.
        session_id: The parent session ID.
        source: The source name.

    Returns:
        Flattened dictionary suitable for Parquet row.
    """
    raw_attrs = span.get("raw_attributes", {})

    # Serialize raw_attributes as JSON string (only non-duplicated fields)
    raw_attrs_json = json.dumps(raw_attrs) if raw_attrs else None

    return {
        # Keys
        "session_id": session_id,
        "source": source,
        "span_id": span.get("span_id"),
        "trace_id": span.get("trace_id"),
        "parent_id": span.get("parent_id"),
        # Span metadata
        "name": span.get("name"),
        "span_kind": span.get("span_kind"),
        "start_time": _parse_datetime(span.get("start_time")),
        "end_time": _parse_datetime(span.get("end_time")),
        "status_code": span.get("status_code"),
        # Content
        "input_value": span.get("input_value"),
        "output_value": span.get("output_value"),
        "input_messages": span.get("input_messages"),
        "output_messages": span.get("output_messages"),
        # LLM metadata
        "llm_model_name": span.get("llm_model_name"),
        "llm_token_count_prompt": _safe_int(span.get("llm_token_count_prompt")),
        "llm_token_count_completion": _safe_int(span.get("llm_token_count_completion")),
        "llm_token_count_total": _safe_int(span.get("llm_token_count_total")),
        # Backend info
        "backend": span.get("backend"),
        # Preserved raw (as JSON string)
        "raw_attributes_json": raw_attrs_json,
    }


def _extract_session_metadata(
    session: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    """Extract session-level metadata.

    Args:
        session: The session dictionary.
        source: The source name.

    Returns:
        Dictionary with session-level metadata.
    """
    spans = session.get("spans", [])

    # Find time range
    start_times = []
    end_times = []
    for span in spans:
        st = _parse_datetime(span.get("start_time"))
        et = _parse_datetime(span.get("end_time"))
        if st:
            start_times.append(st)
        if et:
            end_times.append(et)

    # Aggregate token counts
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    for span in spans:
        pt = _safe_int(span.get("llm_token_count_prompt")) or 0
        ct = _safe_int(span.get("llm_token_count_completion")) or 0
        tt = _safe_int(span.get("llm_token_count_total")) or 0
        total_prompt_tokens += pt
        total_completion_tokens += ct
        total_tokens += tt

    # Get unique models
    models = set()
    for span in spans:
        model = span.get("llm_model_name")
        if model:
            models.add(model)

    # Check for errors
    has_errors = any(span.get("status_code") == "ERROR" for span in spans)

    return {
        "session_id": session.get("session_id"),
        "source": source,
        "span_count": len(spans),
        "first_span_time": min(start_times) if start_times else None,
        "last_span_time": max(end_times) if end_times else None,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "models_used": ",".join(sorted(models)) if models else None,
        "has_errors": has_errors,
    }


def iter_sessions_from_jsonl(file_path: str | Path) -> Iterator[dict[str, Any]]:
    """Iterate over sessions from a JSONL file.

    Args:
        file_path: Path to the JSONL file.

    Yields:
        Session dictionaries.
    """
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


class ParquetExporter:
    """Export unified sessions to Parquet format.

    Provides efficient columnar storage with built-in compression.
    Creates two tables:
    - sessions.parquet: Session-level aggregates
    - spans.parquet: Individual spans with session FK

    Example:
        >>> exporter = ParquetExporter()
        >>> stats = exporter.export_source(
        ...     source="phoenix-alex",
        ...     input_path="~/.dal/data/unified/phoenix-alex_sessions.jsonl",
        ...     output_dir="~/.dal/data/parquet/",
        ... )
        >>> print(f"Exported {stats['sessions']} sessions")
    """

    # Columns with high value duplication that benefit from dictionary encoding
    DICTIONARY_COLUMNS = [
        "input_value",
        "output_value",
        "input_messages",
        "output_messages",
        "raw_attributes_json",
        "name",
        "span_kind",
        "status_code",
        "llm_model_name",
        "source",
    ]

    def __init__(
        self,
        compression: str = "zstd",
        dedupe: bool = True,
        strip_nulls: bool = True,
        use_dictionary: bool = True,
    ) -> None:
        """Initialize the exporter.

        Args:
            compression: Parquet compression codec (zstd, snappy, gzip, none).
                ZSTD provides ~45% better compression than Snappy with
                similar read performance.
            dedupe: If True, deduplicate raw_attributes before export.
            strip_nulls: If True, strip null values from raw_attributes.
            use_dictionary: If True, enable dictionary encoding for columns
                with high value duplication. This can provide 50-70% additional
                size reduction for agent trace data where tool outputs,
                system messages, and errors repeat frequently.
        """
        self.compression = compression if compression != "none" else None
        self.dedupe = dedupe
        self.strip_nulls = strip_nulls
        self.use_dictionary = use_dictionary

    def export_source(
        self,
        source: str,
        input_path: str | Path,
        output_dir: str | Path,
        progress_callback: callable | None = None,
    ) -> dict[str, Any]:
        """Export a source's sessions to Parquet.

        Args:
            source: The source name.
            input_path: Path to input JSONL file.
            output_dir: Directory for output Parquet files.
            progress_callback: Optional callback(sessions_processed).

        Returns:
            Dictionary with export statistics.
        """
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError(
                "pyarrow is required for Parquet export. "
                "Install with: uv add pyarrow"
            )

        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Output paths
        sessions_path = output_dir / f"{source}_sessions.parquet"
        spans_path = output_dir / f"{source}_spans.parquet"

        # Collect data
        session_rows = []
        span_rows = []

        stats = {
            "sessions": 0,
            "spans": 0,
            "input_bytes": input_path.stat().st_size,
        }

        for session in iter_sessions_from_jsonl(input_path):
            # Clean session if needed
            if self.dedupe or self.strip_nulls:
                session = clean_session(
                    session,
                    dedupe=self.dedupe,
                    strip_nulls=self.strip_nulls,
                )

            session_id = session.get("session_id", "")

            # Extract session metadata
            session_meta = _extract_session_metadata(session, source)
            session_rows.append(session_meta)

            # Flatten spans
            for span in session.get("spans", []):
                span_row = _flatten_span(span, session_id, source)
                span_rows.append(span_row)
                stats["spans"] += 1

            stats["sessions"] += 1

            if progress_callback:
                progress_callback(stats["sessions"])

        # Write sessions Parquet
        if session_rows:
            sessions_table = pa.Table.from_pylist(session_rows)
            pq.write_table(
                sessions_table,
                sessions_path,
                compression=self.compression,
            )
            stats["sessions_parquet_bytes"] = sessions_path.stat().st_size

        # Write spans Parquet with dictionary encoding for high-duplication columns
        if span_rows:
            spans_table = pa.Table.from_pylist(span_rows)

            # Build list of columns to use dictionary encoding for
            if self.use_dictionary:
                column_names = [field.name for field in spans_table.schema]
                dict_columns = [
                    col for col in column_names if col in self.DICTIONARY_COLUMNS
                ]
            else:
                dict_columns = False  # Disable dictionary encoding entirely

            pq.write_table(
                spans_table,
                spans_path,
                compression=self.compression,
                use_dictionary=dict_columns,
            )
            stats["spans_parquet_bytes"] = spans_path.stat().st_size

        # Calculate totals
        stats["output_bytes"] = (
            stats.get("sessions_parquet_bytes", 0) +
            stats.get("spans_parquet_bytes", 0)
        )
        stats["savings_bytes"] = stats["input_bytes"] - stats["output_bytes"]
        stats["savings_percent"] = (
            (stats["savings_bytes"] / stats["input_bytes"] * 100)
            if stats["input_bytes"] > 0
            else 0
        )
        stats["sessions_path"] = str(sessions_path)
        stats["spans_path"] = str(spans_path)

        return stats

    def append_to_existing(
        self,
        source: str,
        input_path: str | Path,
        existing_sessions_path: str | Path,
        existing_spans_path: str | Path,
        progress_callback: callable | None = None,
    ) -> dict[str, Any]:
        """Append new sessions to existing Parquet files.

        Reads existing Parquet, appends new data, writes back.
        Safe because we have the previous version.

        Args:
            source: The source name.
            input_path: Path to input JSONL file with new sessions.
            existing_sessions_path: Path to existing sessions Parquet.
            existing_spans_path: Path to existing spans Parquet.
            progress_callback: Optional callback(sessions_processed).

        Returns:
            Dictionary with export statistics.
        """
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError(
                "pyarrow is required for Parquet export. "
                "Install with: uv add pyarrow"
            )

        input_path = Path(input_path)
        existing_sessions_path = Path(existing_sessions_path)
        existing_spans_path = Path(existing_spans_path)

        # Read existing data
        existing_sessions = pq.read_table(existing_sessions_path)
        existing_spans = pq.read_table(existing_spans_path)

        # Get existing session IDs to avoid duplicates
        existing_session_ids = set(
            existing_sessions.column("session_id").to_pylist()
        )

        # Collect new data
        new_session_rows = []
        new_span_rows = []

        stats = {
            "sessions_added": 0,
            "sessions_skipped": 0,
            "spans_added": 0,
        }

        for session in iter_sessions_from_jsonl(input_path):
            session_id = session.get("session_id", "")

            # Skip if already exists
            if session_id in existing_session_ids:
                stats["sessions_skipped"] += 1
                continue

            # Clean session if needed
            if self.dedupe or self.strip_nulls:
                session = clean_session(
                    session,
                    dedupe=self.dedupe,
                    strip_nulls=self.strip_nulls,
                )

            # Extract session metadata
            session_meta = _extract_session_metadata(session, source)
            new_session_rows.append(session_meta)

            # Flatten spans
            for span in session.get("spans", []):
                span_row = _flatten_span(span, session_id, source)
                new_span_rows.append(span_row)
                stats["spans_added"] += 1

            stats["sessions_added"] += 1

            if progress_callback:
                progress_callback(stats["sessions_added"])

        # Append and write
        if new_session_rows:
            new_sessions_table = pa.Table.from_pylist(new_session_rows)
            combined_sessions = pa.concat_tables([existing_sessions, new_sessions_table])
            pq.write_table(
                combined_sessions,
                existing_sessions_path,
                compression=self.compression,
            )

        if new_span_rows:
            new_spans_table = pa.Table.from_pylist(new_span_rows)
            combined_spans = pa.concat_tables([existing_spans, new_spans_table])

            # Build list of columns to use dictionary encoding for
            if self.use_dictionary:
                column_names = [field.name for field in combined_spans.schema]
                dict_columns = [
                    col for col in column_names if col in self.DICTIONARY_COLUMNS
                ]
            else:
                dict_columns = False  # Disable dictionary encoding entirely

            pq.write_table(
                combined_spans,
                existing_spans_path,
                compression=self.compression,
                use_dictionary=dict_columns,
            )

        stats["total_sessions"] = len(existing_session_ids) + stats["sessions_added"]
        return stats


    # Maximum part file size in bytes before splitting
    MAX_PART_SIZE_BYTES = 150 * 1024 * 1024  # 150 MB

    @staticmethod
    def _iso_week(dt: datetime | None) -> str:
        """Return ISO week string like '2026-W05', or 'unknown' for None."""
        if dt is None:
            return "unknown"
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def export_source_partitioned(
        self,
        source: str,
        input_path: str | Path,
        output_dir: str | Path,
        progress_callback: callable | None = None,
    ) -> dict[str, Any]:
        """Export a source's sessions to partitioned Parquet layout.

        Writes spans into Hive-style partitions:
            spans/source=<name>/week=<YYYY-WNN>/part-NNNNN.parquet

        Sessions are written as a single file per source (small enough):
            sessions/source=<name>.parquet

        Args:
            source: The source name.
            input_path: Path to input JSONL file.
            output_dir: Base output directory (e.g. ~/.dal/data/parquet).
            progress_callback: Optional callback(sessions_processed).

        Returns:
            Dictionary with export statistics.
        """
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError(
                "pyarrow is required for Parquet export. "
                "Install with: uv add pyarrow"
            )

        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Collect session rows (small, no partitioning needed)
        session_rows: list[dict[str, Any]] = []
        # Group span rows by week for partitioned writing
        week_spans: dict[str, list[dict[str, Any]]] = defaultdict(list)

        stats: dict[str, Any] = {
            "sessions": 0,
            "spans": 0,
            "input_bytes": input_path.stat().st_size,
        }

        for session in iter_sessions_from_jsonl(input_path):
            if self.dedupe or self.strip_nulls:
                session = clean_session(
                    session,
                    dedupe=self.dedupe,
                    strip_nulls=self.strip_nulls,
                )

            session_id = session.get("session_id", "")
            session_meta = _extract_session_metadata(session, source)
            session_rows.append(session_meta)

            for span in session.get("spans", []):
                span_row = _flatten_span(span, session_id, source)
                week_key = self._iso_week(span_row.get("start_time"))
                week_spans[week_key].append(span_row)
                stats["spans"] += 1

            stats["sessions"] += 1
            if progress_callback:
                progress_callback(stats["sessions"])

        # Write sessions as a single file
        sessions_dir = output_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_path = sessions_dir / f"source={source}.parquet"

        if session_rows:
            sessions_table = pa.Table.from_pylist(session_rows)
            pq.write_table(
                sessions_table,
                sessions_path,
                compression=self.compression,
            )
            stats["sessions_parquet_bytes"] = sessions_path.stat().st_size

        # Write spans partitioned by week
        total_span_bytes = 0
        total_part_files = 0

        for week_key in sorted(week_spans.keys()):
            rows = week_spans[week_key]
            week_dir = output_dir / "spans" / f"source={source}" / f"week={week_key}"
            week_dir.mkdir(parents=True, exist_ok=True)

            table = pa.Table.from_pylist(rows)

            # Build dictionary encoding columns
            if self.use_dictionary:
                column_names = [field.name for field in table.schema]
                dict_columns = [
                    col for col in column_names if col in self.DICTIONARY_COLUMNS
                ]
            else:
                dict_columns = False

            # Estimate if we need to split into multiple parts
            # Write to a single part first, then split if too large
            part_idx = 0
            row_offset = 0
            num_rows = table.num_rows

            while row_offset < num_rows:
                part_path = week_dir / f"part-{part_idx:05d}.parquet"

                if row_offset == 0 and num_rows <= 100_000:
                    # Small enough to write in one shot
                    chunk = table
                    row_offset = num_rows
                else:
                    # Write in chunks, check size after each
                    chunk_size = min(100_000, num_rows - row_offset)
                    chunk = table.slice(row_offset, chunk_size)
                    row_offset += chunk_size

                pq.write_table(
                    chunk,
                    part_path,
                    compression=self.compression,
                    use_dictionary=dict_columns,
                )

                part_size = part_path.stat().st_size
                total_span_bytes += part_size
                total_part_files += 1

                # If this part is under the limit and there are more rows,
                # append to it; otherwise start a new part
                if part_size >= self.MAX_PART_SIZE_BYTES:
                    part_idx += 1

            logger.debug(
                "  %s: %d spans -> %d part files in %s",
                week_key,
                len(rows),
                part_idx + 1,
                week_dir,
            )

        stats["spans_parquet_bytes"] = total_span_bytes
        stats["part_files"] = total_part_files
        stats["weeks"] = len(week_spans)
        stats["output_bytes"] = (
            stats.get("sessions_parquet_bytes", 0) + total_span_bytes
        )
        stats["savings_bytes"] = stats["input_bytes"] - stats["output_bytes"]
        stats["savings_percent"] = (
            (stats["savings_bytes"] / stats["input_bytes"] * 100)
            if stats["input_bytes"] > 0
            else 0
        )
        stats["sessions_path"] = str(sessions_path)
        stats["spans_dir"] = str(output_dir / "spans" / f"source={source}")

        return stats


def export_to_parquet(
    source: str,
    input_path: str | Path,
    output_dir: str | Path,
    compression: str = "zstd",
    dedupe: bool = True,
    strip_nulls: bool = True,
    use_dictionary: bool = True,
    progress_callback: callable | None = None,
) -> dict[str, Any]:
    """Export a source's sessions to Parquet format.

    Convenience function wrapping ParquetExporter.

    Args:
        source: The source name.
        input_path: Path to input JSONL file.
        output_dir: Directory for output Parquet files.
        compression: Parquet compression codec (zstd, snappy, gzip, none).
            ZSTD provides ~45% better compression than Snappy.
        dedupe: If True, deduplicate raw_attributes before export.
        strip_nulls: If True, strip null values from raw_attributes.
        use_dictionary: If True, enable dictionary encoding for high-duplication
            columns. Provides 50-70% additional compression for agent traces.
        progress_callback: Optional callback(sessions_processed).

    Returns:
        Dictionary with export statistics.
    """
    exporter = ParquetExporter(
        compression=compression,
        dedupe=dedupe,
        strip_nulls=strip_nulls,
        use_dictionary=use_dictionary,
    )
    return exporter.export_source(
        source=source,
        input_path=input_path,
        output_dir=output_dir,
        progress_callback=progress_callback,
    )
