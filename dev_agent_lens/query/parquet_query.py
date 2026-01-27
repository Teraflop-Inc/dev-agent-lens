"""
Parquet Query Backend

Provides high-performance query capabilities for Parquet-formatted trace data.
Uses DuckDB for SQL-based queries with predicate pushdown and columnar filtering.

This backend offers 10-100x performance improvement over JSONL for large datasets
by leveraging columnar storage, compression, and SQL query optimization.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from dev_agent_lens.query.query import QueryResult


def _check_duckdb_available() -> bool:
    """Check if DuckDB is available for import."""
    try:
        import duckdb  # noqa: F401

        return True
    except ImportError:
        return False


def _parse_datetime(value: datetime | str | None) -> str | None:
    """Convert datetime to ISO string for SQL comparison."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def extract_skill_name_from_span(span: dict[str, Any]) -> str | None:
    """
    Extract skill name from a span if it's a Claude_Code_Tool_Skill span.

    Handles the 2-level JSON parsing:
    1. raw_attributes_json/raw_attributes → dict
    2. attributes.input.value → tool_use dict
    3. input.skill → skill name

    Returns:
        Skill name string or None if not a Skill span
    """
    name = span.get("name", "")
    if name != "Claude_Code_Tool_Skill":
        return None

    raw_attrs = span.get("raw_attributes") or span.get("raw_attributes_json")
    if not raw_attrs:
        return None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        input_value = attrs.get("attributes", {}).get("input", {}).get("value", "")
        if isinstance(input_value, str) and input_value:
            input_data = json.loads(input_value)
        else:
            input_data = input_value

        if isinstance(input_data, dict):
            tool_input = input_data.get("input", {})
            return tool_input.get("skill")
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return None


def _build_filter_sql(
    session_id: str | None = None,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    status_code: str | None = None,
    model_name: str | None = None,
    skill_name: str | None = None,
) -> tuple[str, list[Any]]:
    """
    Build SQL WHERE clause from filter parameters.

    Returns:
        Tuple of (WHERE clause string, list of parameter values)
    """
    conditions = []
    params = []

    if session_id is not None:
        conditions.append("session_id = ?")
        params.append(session_id)

    if start_time is not None:
        conditions.append("start_time >= ?")
        params.append(_parse_datetime(start_time))

    if end_time is not None:
        conditions.append("start_time <= ?")
        params.append(_parse_datetime(end_time))

    if status_code is not None:
        conditions.append("status_code = ?")
        params.append(status_code)

    if model_name is not None:
        # Case-insensitive partial match
        conditions.append("LOWER(llm_model_name) LIKE ?")
        params.append(f"%{model_name.lower()}%")

    # For skill_name filter, we pre-filter to Skill tool spans at SQL level
    # The actual skill name matching is done in memory after parsing raw_attributes
    if skill_name is not None:
        conditions.append("name = ?")
        params.append("Claude_Code_Tool_Skill")

    if conditions:
        return "WHERE " + " AND ".join(conditions), params
    return "", []


def query_parquet(
    spans_path: str | Path,
    sessions_path: str | Path | None = None,
    pattern: str | None = None,
    session_id: str | None = None,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    status_code: str | None = None,
    model_name: str | None = None,
    skill_name: str | None = None,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
    flat: bool = False,
    limit: int | None = None,
) -> QueryResult:
    """
    Query Parquet files using DuckDB for high-performance filtering.

    This function provides 10-100x performance improvement over JSONL queries
    by leveraging DuckDB's columnar query engine with predicate pushdown.

    Args:
        spans_path: Path to the spans Parquet file
        sessions_path: Optional path to sessions Parquet file (for session metadata)
        pattern: Regex pattern to search for in span content
        session_id: Filter to specific session ID
        start_time: Filter spans starting after this time
        end_time: Filter spans starting before this time
        status_code: Filter by status code (e.g., "OK", "ERROR")
        model_name: Filter by LLM model name (case-insensitive partial match)
        skill_name: Filter to Skill tool spans with this skill name (e.g., "draft-project")
        fields: List of fields to search when using pattern
        case_insensitive: Whether pattern matching is case-insensitive
        flat: If True, returns spans ungrouped; if False, groups by session
        limit: Maximum number of rows to return (for performance)

    Returns:
        QueryResult containing matching sessions (or flat spans if flat=True)

    Raises:
        ImportError: If DuckDB is not installed
        FileNotFoundError: If the Parquet file doesn't exist

    Example:
        >>> result = query_parquet(
        ...     spans_path="~/.dal/data/parquet/phoenix-alex_spans.parquet",
        ...     session_id="abc123",
        ... )
        >>> print(f"Found {result.total_spans} spans")
    """
    if not _check_duckdb_available():
        raise ImportError(
            "DuckDB is required for Parquet queries. Install with: uv add duckdb"
        )

    import duckdb

    spans_path = Path(spans_path).expanduser()
    if not spans_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {spans_path}")

    # Build query params for result
    query_params = {
        "pattern": pattern,
        "session_id": session_id,
        "start_time": str(start_time) if start_time else None,
        "end_time": str(end_time) if end_time else None,
        "status_code": status_code,
        "model_name": model_name,
        "skill_name": skill_name,
        "fields": fields,
        "case_insensitive": case_insensitive,
        "flat": flat,
        "backend": "parquet",
    }

    # Build SQL query
    where_clause, params = _build_filter_sql(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        status_code=status_code,
        model_name=model_name,
        skill_name=skill_name,
    )

    # Build SELECT columns - get all needed columns
    select_columns = [
        "session_id",
        "source",
        "span_id",
        "trace_id",
        "parent_id",
        "name",
        "span_kind",
        "start_time",
        "end_time",
        "status_code",
        "input_value",
        "output_value",
        "input_messages",
        "output_messages",
        "llm_model_name",
        "llm_token_count_prompt",
        "llm_token_count_completion",
        "llm_token_count_total",
        "backend",
        "raw_attributes_json",
    ]

    select_sql = ", ".join(select_columns)
    limit_clause = f"LIMIT {limit}" if limit else ""

    sql = f"""
        SELECT {select_sql}
        FROM read_parquet('{spans_path}')
        {where_clause}
        ORDER BY session_id, start_time
        {limit_clause}
    """

    # Execute query
    con = duckdb.connect()
    try:
        result_df = con.execute(sql, params).df()
    finally:
        con.close()

    if result_df.empty:
        return QueryResult(query_params=query_params)

    # Convert DataFrame to list of dicts
    spans_list = result_df.to_dict("records")

    # Parse raw_attributes_json back to dict if present and extract skill_name
    for span in spans_list:
        if span.get("raw_attributes_json"):
            try:
                span["raw_attributes"] = json.loads(span["raw_attributes_json"])
            except (json.JSONDecodeError, TypeError):
                span["raw_attributes"] = {}
        else:
            span["raw_attributes"] = {}
        # Remove the JSON string version
        span.pop("raw_attributes_json", None)

        # Extract skill_name for Skill tool spans
        span["skill_name"] = extract_skill_name_from_span(span)

    # Apply skill_name filter in memory (SQL pre-filtered to Skill spans)
    if skill_name is not None:
        spans_list = [s for s in spans_list if s.get("skill_name") == skill_name]
        if not spans_list:
            return QueryResult(query_params=query_params)

    # Apply regex pattern filter if provided (done in-memory after SQL filtering)
    if pattern is not None:
        from dev_agent_lens.query.regex import search as regex_search

        matches = regex_search(
            pattern, spans_list, fields=fields, case_insensitive=case_insensitive
        )
        if not matches:
            return QueryResult(query_params=query_params)

        # Get unique spans that matched
        matched_span_ids = set()
        for match in matches:
            span_id = match.span.get("span_id")
            if span_id:
                matched_span_ids.add(span_id)

        spans_list = [s for s in spans_list if s.get("span_id") in matched_span_ids]

    if not spans_list:
        return QueryResult(query_params=query_params)

    # Build result
    if flat:
        return QueryResult(
            sessions=[
                {
                    "session_id": None,
                    "spans": spans_list,
                    "span_count": len(spans_list),
                    "start_time": None,
                    "end_time": None,
                }
            ],
            total_spans=len(spans_list),
            total_sessions=1,
            query_params=query_params,
        )
    else:
        # Group by session
        sessions = _group_spans_by_session(spans_list)
        total_spans = sum(s["span_count"] for s in sessions)

        return QueryResult(
            sessions=sessions,
            total_spans=total_spans,
            total_sessions=len(sessions),
            query_params=query_params,
        )


def _group_spans_by_session(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group spans by session ID."""
    sessions_map: dict[str | None, list[dict[str, Any]]] = {}

    for span in spans:
        session_id = span.get("session_id")
        if session_id not in sessions_map:
            sessions_map[session_id] = []
        sessions_map[session_id].append(span)

    sessions = []
    for session_id, session_spans in sessions_map.items():
        # Spans should already be sorted by start_time from SQL
        start_times = [
            s.get("start_time") for s in session_spans if s.get("start_time")
        ]
        end_times = [s.get("end_time") for s in session_spans if s.get("end_time")]

        sessions.append(
            {
                "session_id": session_id,
                "spans": session_spans,
                "span_count": len(session_spans),
                "start_time": min(start_times) if start_times else None,
                "end_time": max(end_times) if end_times else None,
            }
        )

    # Sort sessions by most recent first
    sessions.sort(
        key=lambda s: s.get("end_time") or s.get("start_time") or "", reverse=True
    )

    return sessions


def search_parquet(
    spans_path: str | Path,
    pattern: str,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    Search Parquet file for regex pattern using DuckDB.

    This function uses DuckDB's regexp_matches for efficient pattern matching
    directly on columnar data without loading everything into memory.

    Args:
        spans_path: Path to the spans Parquet file
        pattern: Regex pattern to search for
        fields: List of fields to search (default: text fields)
        case_insensitive: Whether to use case-insensitive matching
        limit: Maximum number of matching rows to return

    Returns:
        List of span dictionaries that match the pattern

    Example:
        >>> spans = search_parquet(
        ...     "~/.dal/data/parquet/phoenix-alex_spans.parquet",
        ...     pattern=r"ENG2-\\d+",
        ...     case_insensitive=True,
        ... )
    """
    if not _check_duckdb_available():
        raise ImportError(
            "DuckDB is required for Parquet queries. Install with: uv add duckdb"
        )

    import duckdb

    spans_path = Path(spans_path).expanduser()
    if not spans_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {spans_path}")

    # Default fields to search
    if fields is None:
        fields = [
            "name",
            "input_value",
            "output_value",
            "input_messages",
            "output_messages",
            "llm_model_name",
            "raw_attributes_json",
        ]

    # Build regex conditions for each field
    # DuckDB uses regexp_matches for regex
    flag = "'i'" if case_insensitive else "''"
    conditions = []
    for field in fields:
        # Use COALESCE to handle NULL values
        conditions.append(f"regexp_matches(COALESCE({field}, ''), '{pattern}', {flag})")

    where_clause = "WHERE " + " OR ".join(conditions)

    sql = f"""
        SELECT *
        FROM read_parquet('{spans_path}')
        {where_clause}
        LIMIT {limit}
    """

    con = duckdb.connect()
    try:
        result_df = con.execute(sql).df()
    finally:
        con.close()

    if result_df.empty:
        return []

    spans = result_df.to_dict("records")

    # Parse raw_attributes_json
    for span in spans:
        if span.get("raw_attributes_json"):
            try:
                span["raw_attributes"] = json.loads(span["raw_attributes_json"])
            except (json.JSONDecodeError, TypeError):
                span["raw_attributes"] = {}
        span.pop("raw_attributes_json", None)

    return spans


def get_parquet_stats(spans_path: str | Path) -> dict[str, Any]:
    """
    Get statistics about a Parquet file without loading all data.

    Args:
        spans_path: Path to the spans Parquet file

    Returns:
        Dictionary with file statistics including row count, size, columns
    """
    if not _check_duckdb_available():
        raise ImportError("DuckDB is required. Install with: uv add duckdb")

    import duckdb

    spans_path = Path(spans_path).expanduser()
    if not spans_path.exists():
        raise FileNotFoundError(f"Parquet file not found: {spans_path}")

    con = duckdb.connect()
    try:
        # Get row count
        row_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{spans_path}')"
        ).fetchone()[0]

        # Get unique sessions count
        session_count = con.execute(
            f"SELECT COUNT(DISTINCT session_id) FROM read_parquet('{spans_path}')"
        ).fetchone()[0]

        # Get column info
        columns = con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{spans_path}')"
        ).df()

    finally:
        con.close()

    return {
        "file_path": str(spans_path),
        "file_size_bytes": spans_path.stat().st_size,
        "row_count": row_count,
        "session_count": session_count,
        "columns": columns["column_name"].tolist() if not columns.empty else [],
    }


def find_parquet_files(
    source: str | None = None,
    data_path: str | Path | None = None,
) -> dict[str, dict[str, Path]]:
    """
    Find available Parquet files for sources.

    Args:
        source: Optional source name to filter to
        data_path: Base data path (defaults to ~/.dal/data)

    Returns:
        Dictionary mapping source names to their Parquet file paths:
        {
            "phoenix-alex": {
                "sessions": Path("...sessions.parquet"),
                "spans": Path("...spans.parquet"),
            },
            ...
        }
    """
    from dev_agent_lens.storage.oxen_store import get_default_data_path

    if data_path is None:
        data_path = get_default_data_path()
    data_path = Path(data_path).expanduser()

    parquet_dir = data_path / "parquet"
    if not parquet_dir.exists():
        return {}

    sources = {}

    for file in parquet_dir.glob("*_spans.parquet"):
        source_name = file.stem.replace("_spans", "")
        if source is not None and source_name != source:
            continue

        sessions_file = parquet_dir / f"{source_name}_sessions.parquet"

        sources[source_name] = {
            "spans": file,
            "sessions": sessions_file if sessions_file.exists() else None,
        }

    return sources


def query_source(
    source: str,
    pattern: str | None = None,
    session_id: str | None = None,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    status_code: str | None = None,
    model_name: str | None = None,
    skill_name: str | None = None,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
    flat: bool = False,
    prefer_parquet: bool = True,
    data_path: str | Path | None = None,
) -> QueryResult:
    """
    Query a source with automatic Parquet/JSONL backend selection.

    This function automatically uses Parquet if available for the source,
    falling back to JSONL if not. This provides optimal performance while
    maintaining backward compatibility.

    Args:
        source: Source name (e.g., "phoenix-alex", "arize-sightline")
        pattern: Regex pattern to search for
        session_id: Filter to specific session ID
        start_time: Filter spans starting after this time
        end_time: Filter spans starting before this time
        status_code: Filter by status code
        model_name: Filter by LLM model name
        skill_name: Filter to Skill tool spans with this skill name (e.g., "draft-project")
        fields: List of fields to search when using pattern
        case_insensitive: Whether pattern matching is case-insensitive
        flat: If True, returns spans ungrouped
        prefer_parquet: If True, use Parquet when available (default: True)
        data_path: Base data path (defaults to ~/.dal/data)

    Returns:
        QueryResult containing matching sessions

    Example:
        >>> result = query_source(
        ...     source="phoenix-alex",
        ...     pattern=r"ENG2-\\d+",
        ...     case_insensitive=True,
        ... )
    """
    from dev_agent_lens.storage.oxen_store import get_default_data_path

    if data_path is None:
        data_path = get_default_data_path()
    data_path = Path(data_path).expanduser()

    # Check for Parquet files first if preferred
    if prefer_parquet and _check_duckdb_available():
        parquet_files = find_parquet_files(source=source, data_path=data_path)
        if source in parquet_files:
            spans_path = parquet_files[source]["spans"]
            sessions_path = parquet_files[source].get("sessions")
            return query_parquet(
                spans_path=spans_path,
                sessions_path=sessions_path,
                pattern=pattern,
                session_id=session_id,
                start_time=start_time,
                end_time=end_time,
                status_code=status_code,
                model_name=model_name,
                skill_name=skill_name,
                fields=fields,
                case_insensitive=case_insensitive,
                flat=flat,
            )

    # Fall back to JSONL
    from dev_agent_lens.query.query import query as jsonl_query

    unified_file = data_path / "unified" / f"{source}_sessions.jsonl"
    if not unified_file.exists():
        # Try legacy location
        sessions_dir = data_path / "sessions" / source
        if sessions_dir.exists():
            current = sessions_dir / "sessions_current.jsonl"
            if current.exists():
                unified_file = current
            else:
                session_files = list(sessions_dir.glob("sessions_*.jsonl"))
                if session_files:
                    unified_file = max(session_files, key=lambda p: p.stat().st_mtime)
                else:
                    # Return empty result
                    return QueryResult(
                        query_params={
                            "source": source,
                            "pattern": pattern,
                            "session_id": session_id,
                            "backend": "jsonl",
                            "error": "No data files found",
                        }
                    )

    return jsonl_query(
        pattern=pattern,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        status_code=status_code,
        model_name=model_name,
        fields=fields,
        case_insensitive=case_insensitive,
        flat=flat,
        file_path=unified_file,
    )
