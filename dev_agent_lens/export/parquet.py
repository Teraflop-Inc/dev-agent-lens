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

        # Write sessions as a single file (with dedup against existing)
        sessions_dir = output_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_path = sessions_dir / f"source={source}.parquet"

        if session_rows:
            new_sessions_table = pa.Table.from_pylist(session_rows)

            if sessions_path.exists():
                existing_sessions = pq.read_table(sessions_path)
                existing_ids = set(
                    existing_sessions.column("session_id").to_pylist()
                )
                # Filter to only new sessions
                new_ids = [
                    r["session_id"] for r in session_rows
                    if r["session_id"] not in existing_ids
                ]
                if new_ids:
                    mask = [
                        r["session_id"] not in existing_ids
                        for r in session_rows
                    ]
                    filtered = pa.Table.from_pylist(
                        [r for r, m in zip(session_rows, mask) if m]
                    )
                    new_sessions_table = pa.concat_tables(
                        [existing_sessions, filtered],
                        promote_options="permissive",
                    )
                else:
                    new_sessions_table = existing_sessions

            pq.write_table(
                new_sessions_table,
                sessions_path,
                compression=self.compression,
            )
            stats["sessions_parquet_bytes"] = sessions_path.stat().st_size

        # Write spans partitioned by week, with dedup against existing parts
        total_span_bytes = 0
        total_part_files = 0
        stats["spans_skipped"] = 0

        for week_key in sorted(week_spans.keys()):
            rows = week_spans[week_key]
            week_dir = output_dir / "spans" / f"source={source}" / f"week={week_key}"
            week_dir.mkdir(parents=True, exist_ok=True)

            # Check for existing part files and collect their span_ids
            existing_parts = sorted(week_dir.glob("part-*.parquet"))
            existing_span_ids: set[str] = set()
            last_part_size = 0

            for ep in existing_parts:
                pf = pq.ParquetFile(ep)
                for rg_idx in range(pf.metadata.num_row_groups):
                    rg = pf.read_row_group(rg_idx, columns=["span_id"])
                    existing_span_ids.update(rg.column("span_id").to_pylist())
                last_part_size = ep.stat().st_size

            # Filter out spans that already exist
            if existing_span_ids:
                original_count = len(rows)
                rows = [r for r in rows if r.get("span_id") not in existing_span_ids]
                skipped = original_count - len(rows)
                stats["spans_skipped"] += skipped
                if skipped:
                    logger.debug(
                        "  %s: skipped %d existing spans, %d new",
                        week_key, skipped, len(rows),
                    )

            if not rows:
                # All spans already exist, count existing files in totals
                for ep in existing_parts:
                    total_span_bytes += ep.stat().st_size
                    total_part_files += 1
                continue

            table = pa.Table.from_pylist(rows)

            # Build dictionary encoding columns
            if self.use_dictionary:
                column_names = [field.name for field in table.schema]
                dict_columns = [
                    col for col in column_names if col in self.DICTIONARY_COLUMNS
                ]
            else:
                dict_columns = False

            # Determine starting part index (append after existing parts)
            if existing_parts:
                # Find the last part index
                last_part_name = existing_parts[-1].stem  # e.g. "part-00003"
                last_part_idx = int(last_part_name.split("-")[1])
                # Append to last part if under size limit, else start new
                if last_part_size < self.MAX_PART_SIZE_BYTES:
                    # Read existing last part, append new rows, rewrite
                    last_pf = pq.ParquetFile(existing_parts[-1])
                    last_part_table = last_pf.read().cast(table.schema)
                    table = pa.concat_tables(
                        [last_part_table, table],
                        promote_options="permissive",
                    )
                    part_idx = last_part_idx
                    # Don't count existing parts except the one we're rewriting
                    for ep in existing_parts[:-1]:
                        total_span_bytes += ep.stat().st_size
                        total_part_files += 1
                else:
                    part_idx = last_part_idx + 1
                    # Count all existing parts
                    for ep in existing_parts:
                        total_span_bytes += ep.stat().st_size
                        total_part_files += 1
            else:
                part_idx = 0

            # Write part files
            row_offset = 0
            num_rows = table.num_rows

            while row_offset < num_rows:
                part_path = week_dir / f"part-{part_idx:05d}.parquet"

                if row_offset == 0 and num_rows <= 100_000:
                    chunk = table
                    row_offset = num_rows
                else:
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

                if part_size >= self.MAX_PART_SIZE_BYTES:
                    part_idx += 1

            logger.debug(
                "  %s: %d new spans -> part files in %s",
                week_key,
                len(rows),
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


    def export_raw_to_partitioned(
        self,
        source: str,
        raw_dir: str | Path,
        output_dir: str | Path,
        chunk_size: int = 50,
        progress_callback: callable | None = None,
    ) -> dict[str, Any]:
        """Export raw sync files directly to partitioned Parquet.

        Skips the unified JSONL intermediate step. Processes raw files in
        chunks, groups by session, flattens spans, and writes directly to
        partitioned parquet with span_id dedup against existing parts.

        Args:
            source: The source name.
            raw_dir: Directory containing raw sync JSONL files.
            output_dir: Base output directory for partitioned parquet.
            chunk_size: Number of raw files to process per chunk.
            progress_callback: Optional callback(chunk_number, total_chunks).

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

        raw_dir = Path(raw_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        raw_files = sorted(raw_dir.glob("sync_*.jsonl")) + sorted(raw_dir.glob("sync_gap_*.jsonl"))
        if not raw_files:
            return {"sessions": 0, "spans": 0, "spans_skipped": 0, "part_files": 0, "weeks": 0}

        total_chunks = (len(raw_files) + chunk_size - 1) // chunk_size
        input_bytes = sum(f.stat().st_size for f in raw_files)

        # Load existing span_ids from parquet once (for dedup)
        spans_base = output_dir / "spans" / f"source={source}"
        existing_span_ids: set[str] = set()
        if spans_base.exists():
            for part_file in spans_base.rglob("part-*.parquet"):
                pf = pq.ParquetFile(part_file)
                for rg_idx in range(pf.metadata.num_row_groups):
                    rg = pf.read_row_group(rg_idx, columns=["span_id"])
                    existing_span_ids.update(rg.column("span_id").to_pylist())
            logger.info("Loaded %d existing span_ids for dedup", len(existing_span_ids))

        # Track sessions across chunks (only metadata, not full spans)
        all_session_ids: set[str] = set()
        # Session metadata rows (small — one row per session)
        session_rows: list[dict[str, Any]] = []

        stats: dict[str, Any] = {
            "sessions": 0,
            "spans": 0,
            "spans_skipped": 0,
            "input_bytes": input_bytes,
        }

        # Per-week writers that persist across chunks
        week_writers: dict[str, tuple[pq.ParquetWriter, Path, int]] = {}
        week_part_idx: dict[str, int] = {}

        # Initialize part indices from existing files
        if spans_base.exists():
            for week_dir in spans_base.iterdir():
                if not week_dir.is_dir():
                    continue
                week_key = week_dir.name.replace("week=", "")
                parts = sorted(week_dir.glob("part-*.parquet"))
                if parts:
                    last_idx = int(parts[-1].stem.split("-")[1])
                    last_size = parts[-1].stat().st_size
                    if last_size < self.MAX_PART_SIZE_BYTES:
                        week_part_idx[week_key] = last_idx  # Will append to this
                    else:
                        week_part_idx[week_key] = last_idx + 1

        for chunk_idx in range(0, len(raw_files), chunk_size):
            chunk_files = raw_files[chunk_idx:chunk_idx + chunk_size]
            chunk_num = chunk_idx // chunk_size + 1

            if progress_callback:
                progress_callback(chunk_num, total_chunks)

            # Read raw spans from this chunk
            chunk_spans = []
            for raw_file in chunk_files:
                with open(raw_file) as f:
                    for line in f:
                        if line.strip():
                            try:
                                chunk_spans.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue

            if not chunk_spans:
                continue

            # Import session grouping
            from dev_agent_lens.query.query import _group_by_session
            chunk_sessions = _group_by_session(chunk_spans)
            del chunk_spans

            # Process each session: extract metadata, flatten + dedup spans
            week_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)

            for session in chunk_sessions:
                session_id = session.get("session_id", "")

                # Collect session metadata (only if new)
                if session_id not in all_session_ids:
                    all_session_ids.add(session_id)
                    if self.dedupe or self.strip_nulls:
                        session = clean_session(
                            session, dedupe=self.dedupe, strip_nulls=self.strip_nulls,
                        )
                    session_rows.append(_extract_session_metadata(session, source))
                    stats["sessions"] += 1

                # Flatten spans, dedup, group by week
                for span in session.get("spans", []):
                    span_id = span.get("span_id")
                    if span_id in existing_span_ids:
                        stats["spans_skipped"] += 1
                        continue
                    existing_span_ids.add(span_id)

                    if self.dedupe or self.strip_nulls:
                        # Clean individual span's raw_attributes
                        pass  # clean_session already handled this above for new sessions

                    span_row = _flatten_span(span, session_id, source)
                    week_key = self._iso_week(span_row.get("start_time"))
                    week_rows[week_key].append(span_row)
                    stats["spans"] += 1

            del chunk_sessions

            # Write this chunk's spans to partitioned parquet
            for week_key, rows in week_rows.items():
                if not rows:
                    continue

                week_dir = output_dir / "spans" / f"source={source}" / f"week={week_key}"
                week_dir.mkdir(parents=True, exist_ok=True)

                table = pa.Table.from_pylist(rows)

                if self.use_dictionary:
                    column_names = [field.name for field in table.schema]
                    dict_columns = [
                        col for col in column_names if col in self.DICTIONARY_COLUMNS
                    ]
                else:
                    dict_columns = False

                part_idx = week_part_idx.get(week_key, 0)
                part_path = week_dir / f"part-{part_idx:05d}.parquet"

                # If appending to existing part, read and concat
                if part_path.exists() and part_path.stat().st_size < self.MAX_PART_SIZE_BYTES:
                    existing_pf = pq.ParquetFile(part_path)
                    existing_table = existing_pf.read()
                    # Normalize timestamp columns to tz-naive to avoid
                    # "Cannot merge timestamp with timezone and without" errors
                    for col_name in ["start_time", "end_time"]:
                        for tbl_ref in ("existing_table", "table"):
                            tbl = existing_table if tbl_ref == "existing_table" else table
                            if col_name in tbl.schema.names:
                                col_type = tbl.schema.field(col_name).type
                                if hasattr(col_type, "tz") and col_type.tz is not None:
                                    idx = tbl.schema.get_field_index(col_name)
                                    col = tbl.column(col_name).cast(pa.timestamp("us"))
                                    if tbl_ref == "existing_table":
                                        existing_table = existing_table.set_column(idx, col_name, col)
                                    else:
                                        table = table.set_column(idx, col_name, col)
                    table = pa.concat_tables(
                        [existing_table, table], promote_options="permissive",
                    )

                pq.write_table(
                    table, part_path,
                    compression=self.compression,
                    use_dictionary=dict_columns,
                )

                # Check if we need to roll to a new part
                if part_path.stat().st_size >= self.MAX_PART_SIZE_BYTES:
                    week_part_idx[week_key] = part_idx + 1
                else:
                    week_part_idx[week_key] = part_idx

            del week_rows
            logger.debug("Chunk %d/%d: %d spans written", chunk_num, total_chunks, stats["spans"])

        # Write sessions
        sessions_dir = output_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_path = sessions_dir / f"source={source}.parquet"

        if session_rows:
            new_sessions_table = pa.Table.from_pylist(session_rows)
            if sessions_path.exists():
                existing_sessions = pq.read_table(sessions_path)
                existing_ids = set(existing_sessions.column("session_id").to_pylist())
                filtered = [r for r in session_rows if r["session_id"] not in existing_ids]
                if filtered:
                    new_sessions_table = pa.concat_tables(
                        [existing_sessions, pa.Table.from_pylist(filtered)],
                        promote_options="permissive",
                    )
                else:
                    new_sessions_table = existing_sessions
            pq.write_table(new_sessions_table, sessions_path, compression=self.compression)

        # Compute output size
        total_span_bytes = sum(
            f.stat().st_size for f in (output_dir / "spans").rglob("*.parquet")
        ) if (output_dir / "spans").exists() else 0
        sessions_bytes = sessions_path.stat().st_size if sessions_path.exists() else 0

        stats["output_bytes"] = total_span_bytes + sessions_bytes
        stats["sessions_parquet_bytes"] = sessions_bytes
        stats["spans_parquet_bytes"] = total_span_bytes
        stats["part_files"] = sum(1 for _ in (output_dir / "spans").rglob("*.parquet")) if (output_dir / "spans").exists() else 0
        stats["weeks"] = len(set(
            d.name for d in (output_dir / "spans" / f"source={source}").iterdir()
        )) if (output_dir / "spans" / f"source={source}").exists() else 0
        stats["savings_bytes"] = stats["input_bytes"] - stats["output_bytes"]
        stats["savings_percent"] = (
            (stats["savings_bytes"] / stats["input_bytes"] * 100)
            if stats["input_bytes"] > 0 else 0
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
