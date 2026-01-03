"""
Unified Query API

Provides a unified interface for querying trace spans with support for
pattern matching, session filtering, time ranges, and multiple output formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from dev_agent_lens.core.session import extract_session_id_from_span
from dev_agent_lens.core.unify import read_sessions_file
from dev_agent_lens.query.regex import search, search_file


@dataclass
class QueryResult:
    """
    Result of a query operation.

    Attributes:
        sessions: List of session dictionaries, each containing:
            - session_id: The session identifier
            - spans: List of spans in this session
            - span_count: Number of spans in the session
            - start_time: First span start time
            - end_time: Last span end time
        total_spans: Total number of spans across all sessions
        total_sessions: Number of sessions returned
        query_params: The parameters used for the query
    """

    sessions: list[dict[str, Any]] = field(default_factory=list)
    total_spans: int = 0
    total_sessions: int = 0
    query_params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "sessions": self.sessions,
            "total_spans": self.total_spans,
            "total_sessions": self.total_sessions,
            "query_params": self.query_params,
        }

    def to_dataframe(self) -> pd.DataFrame:
        """Convert all spans to a flat DataFrame."""
        all_spans = []
        for session in self.sessions:
            all_spans.extend(session.get("spans", []))
        return pd.DataFrame(all_spans) if all_spans else pd.DataFrame()


def _extract_session_id(span: dict[str, Any]) -> str | None:
    """Extract session ID from a span dictionary."""
    return extract_session_id_from_span(span)


def _group_by_session(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Group spans by session ID.

    Args:
        spans: List of span dictionaries

    Returns:
        List of session dictionaries with session_id, spans, span_count, time range
    """
    # Group spans by session_id
    sessions_map: dict[str | None, list[dict[str, Any]]] = {}

    for span in spans:
        session_id = _extract_session_id(span)
        if session_id not in sessions_map:
            sessions_map[session_id] = []
        sessions_map[session_id].append(span)

    # Convert to list of session dicts
    sessions = []
    for session_id, session_spans in sessions_map.items():
        # Sort spans by start_time
        session_spans.sort(key=lambda s: s.get("start_time") or "")

        # Get time range
        start_times = [s.get("start_time") for s in session_spans if s.get("start_time")]
        end_times = [s.get("end_time") for s in session_spans if s.get("end_time")]

        sessions.append({
            "session_id": session_id,
            "spans": session_spans,
            "span_count": len(session_spans),
            "start_time": min(start_times) if start_times else None,
            "end_time": max(end_times) if end_times else None,
        })

    # Sort sessions by most recent first
    sessions.sort(key=lambda s: s.get("end_time") or s.get("start_time") or "", reverse=True)

    return sessions


def _filter_by_session_id(
    spans: list[dict[str, Any]], session_id: str
) -> list[dict[str, Any]]:
    """Filter spans to only those matching the given session_id."""
    return [s for s in spans if _extract_session_id(s) == session_id]


def _filter_by_time_range(
    spans: list[dict[str, Any]],
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Filter spans by time range."""
    if start_time is None and end_time is None:
        return spans

    # Convert to ISO strings for comparison
    if isinstance(start_time, datetime):
        start_time = start_time.isoformat()
    if isinstance(end_time, datetime):
        end_time = end_time.isoformat()

    filtered = []
    for span in spans:
        span_start = span.get("start_time")
        if span_start is None:
            continue

        if start_time and span_start < start_time:
            continue
        if end_time and span_start > end_time:
            continue

        filtered.append(span)

    return filtered


def _filter_by_status(
    spans: list[dict[str, Any]], status_code: str
) -> list[dict[str, Any]]:
    """Filter spans by status code."""
    return [s for s in spans if s.get("status_code") == status_code]


def _filter_by_model(
    spans: list[dict[str, Any]], model_name: str
) -> list[dict[str, Any]]:
    """Filter spans by LLM model name (case-insensitive partial match)."""
    model_lower = model_name.lower()
    return [
        s for s in spans
        if s.get("llm_model_name") and model_lower in s["llm_model_name"].lower()
    ]


def query(
    pattern: str | None = None,
    session_id: str | None = None,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    status_code: str | None = None,
    model_name: str | None = None,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
    flat: bool = False,
    spans: list[dict[str, Any]] | pd.DataFrame | None = None,
    file_path: str | Path | None = None,
) -> QueryResult:
    """
    Query trace spans with flexible filtering options.

    All filters are combined with AND logic - spans must match all provided filters.

    Args:
        pattern: Regex pattern to search for in span content
        session_id: Filter to specific session ID
        start_time: Filter spans starting after this time
        end_time: Filter spans starting before this time
        status_code: Filter by status code (e.g., "OK", "ERROR")
        model_name: Filter by LLM model name (case-insensitive partial match)
        fields: List of fields to search when using pattern (default: all string fields)
        case_insensitive: Whether pattern matching is case-insensitive
        flat: If True, returns spans ungrouped; if False, groups by session
        spans: Span data as list of dicts or DataFrame. If None, reads from file_path
        file_path: Path to JSONL file to query. Used if spans is None

    Returns:
        QueryResult containing matching sessions (or flat spans if flat=True)

    Raises:
        ValueError: If neither spans nor file_path is provided

    Example:
        >>> # Query by pattern
        >>> result = query(pattern=r"ENG2-\\d+", file_path="sessions.jsonl")
        >>> print(f"Found {result.total_spans} matching spans")

        >>> # Query specific session
        >>> result = query(session_id="abc123", file_path="sessions.jsonl")

        >>> # Combined filters
        >>> result = query(
        ...     pattern="error",
        ...     session_id="abc123",
        ...     status_code="ERROR",
        ...     case_insensitive=True,
        ... )
    """
    # Build query params for result
    query_params = {
        "pattern": pattern,
        "session_id": session_id,
        "start_time": str(start_time) if start_time else None,
        "end_time": str(end_time) if end_time else None,
        "status_code": status_code,
        "model_name": model_name,
        "fields": fields,
        "case_insensitive": case_insensitive,
        "flat": flat,
    }

    # Load data
    if spans is not None:
        if isinstance(spans, pd.DataFrame):
            spans_list = spans.to_dict("records")
        else:
            spans_list = list(spans)
    elif file_path is not None:
        file_path = Path(file_path)
        if not file_path.exists():
            # Return empty result for non-existent file
            return QueryResult(query_params=query_params)
        df = read_sessions_file(file_path)
        spans_list = df.to_dict("records") if not df.empty else []
    else:
        raise ValueError("Either 'spans' or 'file_path' must be provided")

    if not spans_list:
        return QueryResult(query_params=query_params)

    # Check if data is already pre-grouped sessions (has 'spans' field with list)
    is_pregrouped = (
        spans_list
        and "spans" in spans_list[0]
        and isinstance(spans_list[0].get("spans"), list)
    )

    if is_pregrouped and not flat:
        # Data is already grouped into sessions, return as-is (with filtering if needed)
        sessions = []
        for s in spans_list:
            session_spans = s.get("spans", [])

            # Apply session filter
            if session_id is not None:
                if s.get("session_id") != session_id:
                    continue

            # Apply status filter to spans
            if status_code is not None:
                session_spans = [sp for sp in session_spans if sp.get("status_code") == status_code]
                if not session_spans:
                    continue

            # Apply model filter to spans
            if model_name is not None:
                model_lower = model_name.lower()
                session_spans = [
                    sp for sp in session_spans
                    if model_lower in (sp.get("llm_model_name") or "").lower()
                ]
                if not session_spans:
                    continue

            # Get time range
            start_times = [sp.get("start_time") for sp in session_spans if sp.get("start_time")]
            end_times = [sp.get("end_time") for sp in session_spans if sp.get("end_time")]

            sessions.append({
                "session_id": s.get("session_id"),
                "spans": session_spans,
                "span_count": len(session_spans),
                "start_time": min(start_times) if start_times else s.get("start_time"),
                "end_time": max(end_times) if end_times else s.get("end_time"),
            })

        return QueryResult(
            sessions=sessions,
            total_spans=sum(s["span_count"] for s in sessions),
            total_sessions=len(sessions),
            query_params=query_params,
        )

    # Apply filters in order (most selective first for efficiency)

    # Session filter (usually very selective)
    if session_id is not None:
        spans_list = _filter_by_session_id(spans_list, session_id)
        if not spans_list:
            return QueryResult(query_params=query_params)

    # Status filter
    if status_code is not None:
        spans_list = _filter_by_status(spans_list, status_code)
        if not spans_list:
            return QueryResult(query_params=query_params)

    # Model filter
    if model_name is not None:
        spans_list = _filter_by_model(spans_list, model_name)
        if not spans_list:
            return QueryResult(query_params=query_params)

    # Time range filter
    if start_time is not None or end_time is not None:
        spans_list = _filter_by_time_range(spans_list, start_time, end_time)
        if not spans_list:
            return QueryResult(query_params=query_params)

    # Pattern filter (most expensive, do last)
    if pattern is not None:
        matches = search(pattern, spans_list, fields=fields, case_insensitive=case_insensitive)
        if not matches:
            return QueryResult(query_params=query_params)
        # Get unique spans that matched
        matched_span_ids = set()
        matched_spans_map = {}
        for match in matches:
            span_id = match.span.get("span_id")
            if span_id and span_id not in matched_span_ids:
                matched_span_ids.add(span_id)
                matched_spans_map[span_id] = match.span

        # Preserve order from original list
        spans_list = [s for s in spans_list if s.get("span_id") in matched_span_ids]

    # Build result
    if flat:
        # Return as single "session" with all spans
        return QueryResult(
            sessions=[{
                "session_id": None,
                "spans": spans_list,
                "span_count": len(spans_list),
                "start_time": None,
                "end_time": None,
            }],
            total_spans=len(spans_list),
            total_sessions=1 if spans_list else 0,
            query_params=query_params,
        )
    else:
        # Group by session
        sessions = _group_by_session(spans_list)
        total_spans = sum(s["span_count"] for s in sessions)

        return QueryResult(
            sessions=sessions,
            total_spans=total_spans,
            total_sessions=len(sessions),
            query_params=query_params,
        )


def query_file(
    file_path: str | Path,
    pattern: str | None = None,
    session_id: str | None = None,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    status_code: str | None = None,
    model_name: str | None = None,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
    flat: bool = False,
) -> QueryResult:
    """
    Convenience function to query a JSONL file.

    See query() for full parameter documentation.
    """
    return query(
        pattern=pattern,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        status_code=status_code,
        model_name=model_name,
        fields=fields,
        case_insensitive=case_insensitive,
        flat=flat,
        file_path=file_path,
    )


def query_sessions(
    storage_path: str | Path | None = None,
    search: str | None = None,
    session_id: str | None = None,
    start_time: datetime | str | None = None,
    end_time: datetime | str | None = None,
    source: str | None = None,
    prefer_parquet: bool = True,
) -> list[dict]:
    """
    Query sessions from storage with automatic backend detection.

    This is a high-level convenience function for business-level queries.
    It automatically uses the configured storage path and returns
    session dictionaries with their spans.

    The function will automatically use Parquet backend (10-100x faster) when:
    - A source is specified and Parquet files exist for that source
    - prefer_parquet=True (default)

    Falls back to JSONL when Parquet is not available.

    Args:
        storage_path: Optional storage path override. Uses default if None.
        search: Search string to filter sessions (searches input/output values)
        session_id: Filter to specific session ID
        start_time: Filter sessions starting after this time
        end_time: Filter sessions starting before this time
        source: Source name to query (e.g., "my-project"). When provided,
            will look for Parquet files first.
        prefer_parquet: If True (default), prefer Parquet backend when available.
            Set to False to force JSONL backend.

    Returns:
        List of session dictionaries, each with:
            - session_id: Session identifier
            - spans: List of span dictionaries
            - span_count: Number of spans
            - start_time: Session start time
            - end_time: Session end time

    Example:
        >>> # Query from specific source (uses Parquet if available)
        >>> sessions = query_sessions(source="my-project")

        >>> # Find sessions mentioning a ticket
        >>> sessions = query_sessions(source="my-project", search="TICKET-123")

        >>> # Get specific session
        >>> sessions = query_sessions(session_id="abc123")

        >>> # Force JSONL backend
        >>> sessions = query_sessions(source="my-project", prefer_parquet=False)
    """
    from dev_agent_lens.storage import get_storage_path

    if storage_path is None:
        storage_path = get_storage_path()

    storage_path = Path(storage_path)

    # Try Parquet backend first if source is specified and prefer_parquet=True
    if source and prefer_parquet:
        try:
            from dev_agent_lens.query.parquet_query import find_parquet_files, query_parquet

            parquet_files = find_parquet_files(source=source, data_path=storage_path)
            if source in parquet_files and "spans" in parquet_files[source]:
                spans_path = parquet_files[source]["spans"]
                sessions_path = parquet_files[source].get("sessions")

                result = query_parquet(
                    spans_path=spans_path,
                    sessions_path=sessions_path,
                    pattern=search,
                    session_id=session_id,
                    start_time=start_time,
                    end_time=end_time,
                    case_insensitive=True,
                )
                return result.sessions
        except ImportError:
            # DuckDB not available, fall through to JSONL
            pass
        except Exception:
            # Parquet query failed, fall through to JSONL
            pass

    # Fall back to JSONL
    # Find sessions file
    sessions_dir = storage_path / "sessions"
    if not sessions_dir.exists():
        return []

    # Try current sessions file first
    sessions_file = sessions_dir / "sessions_current.jsonl"
    if not sessions_file.exists():
        # Find most recent sessions file
        session_files = list(sessions_dir.glob("sessions_*.jsonl"))
        if not session_files:
            return []
        sessions_file = max(session_files, key=lambda p: p.stat().st_mtime)

    # Use pattern search if search string provided
    result = query(
        pattern=search,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        file_path=sessions_file,
        case_insensitive=True,
    )

    return result.sessions
