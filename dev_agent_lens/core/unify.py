"""
Thread Unification Module

Merges new spans with existing session data, detecting session continuations
and deduplicating by span_id while preserving temporal ordering.

Key Operations:
1. Session detection - Groups spans by session_id
2. Continuation matching - Identifies spans continuing existing sessions
3. Deduplication - Removes duplicate spans by span_id
4. Ordering - Sorts spans by start_time within each session
5. Match reporting - Generates JSON report of unification results
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from dev_agent_lens.core.session import extract_session_id_from_span


def get_default_state_path() -> Path:
    """Get the default state path for match reports."""
    env_path = os.getenv("DAL_DATA_PATH")
    if env_path:
        return Path(env_path).expanduser() / "state"
    return Path.home() / ".dal" / "data" / "state"


@dataclass
class MatchReport:
    """Report of session unification results."""

    timestamp: str
    new_sessions: list[str] = field(default_factory=list)
    continued_sessions: list[str] = field(default_factory=list)
    total_spans_before: int = 0
    total_spans_after: int = 0
    duplicates_removed: int = 0
    spans_added: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "new_sessions": self.new_sessions,
            "continued_sessions": self.continued_sessions,
            "total_spans_before": self.total_spans_before,
            "total_spans_after": self.total_spans_after,
            "duplicates_removed": self.duplicates_removed,
            "spans_added": self.spans_added,
            "summary": {
                "new_session_count": len(self.new_sessions),
                "continued_session_count": len(self.continued_sessions),
            },
        }


def _extract_session_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Add session_id column to DataFrame by extracting from span metadata.

    Session metadata is typically only present on certain span types (e.g.,
    litellm_request, raw_gen_ai_request). This function extracts session IDs
    from spans that have metadata and propagates them to all spans with the
    same trace_id.

    Priority:
    1. Extract explicit session_id from metadata (session_xxx pattern)
    2. Propagate session_id to all spans sharing the same trace_id
    3. Fall back to trace_id for traces without any session metadata

    Agent-Specific Notes:
    ---------------------
    CLAUDE CODE: Session metadata is only present on LLM request spans
    (litellm_request, raw_gen_ai_request), not on tool spans (Claude_Code_Tool_*).
    A single multi-turn conversation generates many trace_ids (one per API call),
    but they all share the same session_id in the metadata. This function
    propagates the session_id from spans that have it to all sibling spans.

    OTHER AGENTS: When adding support for new coding agents (Cursor, Windsurf,
    etc.), check if they have similar patterns where session metadata is only
    on certain span types. The propagation logic should work generically, but
    the extraction in extract_session_id_from_span() may need agent-specific
    handling.
    """
    if df.empty:
        df["session_id"] = pd.Series(dtype=str)
        return df

    df = df.copy()

    # First pass: extract session_id from each span
    df["_raw_session_id"] = df.apply(
        lambda row: extract_session_id_from_span(row.to_dict()), axis=1
    )

    # Build a mapping of trace_id -> proper session_id (from spans that have metadata)
    # A "proper" session_id is one that differs from the trace_id
    trace_to_session = {}
    if "trace_id" in df.columns:
        for _, row in df.iterrows():
            trace_id = row.get("trace_id")
            raw_session = row.get("_raw_session_id")
            # If this span has a proper session_id (not just trace_id fallback)
            if raw_session and trace_id and raw_session != trace_id:
                trace_to_session[trace_id] = raw_session

    # Second pass: propagate proper session_ids to all spans in the same trace
    def get_final_session_id(row):
        trace_id = row.get("trace_id")
        raw_session = row.get("_raw_session_id")

        # If this trace has a known proper session_id, use it
        if trace_id and trace_id in trace_to_session:
            return trace_to_session[trace_id]

        # Otherwise use whatever was extracted (may be trace_id fallback)
        return raw_session

    df["session_id"] = df.apply(get_final_session_id, axis=1)
    df = df.drop(columns=["_raw_session_id"])

    return df


def _sort_by_time(df: pd.DataFrame) -> pd.DataFrame:
    """Sort DataFrame by start_time, handling various formats."""
    if df.empty or "start_time" not in df.columns:
        return df

    df = df.copy()

    # Convert start_time to datetime for sorting
    # Always return timezone-naive UTC for consistent comparison
    def parse_time(val: Any) -> datetime | None:
        if pd.isna(val):
            return None
        if isinstance(val, datetime):
            # Convert to naive UTC if timezone-aware
            if val.tzinfo is not None:
                return val.replace(tzinfo=None)
            return val
        if isinstance(val, str):
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                # Convert to naive UTC for comparison
                if dt.tzinfo is not None:
                    return dt.replace(tzinfo=None)
                return dt
            except (ValueError, AttributeError):
                return None
        return None

    df["_sort_time"] = df["start_time"].apply(parse_time)
    df = df.sort_values("_sort_time", na_position="last")
    df = df.drop(columns=["_sort_time"])
    return df


def _deduplicate_spans(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """
    Deduplicate spans by span_id, keeping the latest version.

    Returns:
        Tuple of (deduplicated DataFrame, count of duplicates removed)
    """
    if df.empty:
        return df, 0

    original_count = len(df)

    if "span_id" not in df.columns:
        return df, 0

    # Keep last occurrence (newest data wins)
    df = df.drop_duplicates(subset=["span_id"], keep="last")

    duplicates_removed = original_count - len(df)
    return df, duplicates_removed


def read_sessions_file(file_path: Path) -> pd.DataFrame:
    """
    Read a JSONL sessions file into a DataFrame.

    Args:
        file_path: Path to the JSONL file.

    Returns:
        DataFrame containing the sessions data.
    """
    if not file_path.exists():
        return pd.DataFrame()

    if file_path.stat().st_size == 0:
        return pd.DataFrame()

    records = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records)


def write_sessions_file(df: pd.DataFrame, file_path: Path) -> None:
    """
    Write a DataFrame to a JSONL file.

    Args:
        df: DataFrame to write.
        file_path: Path to the output file.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "w") as f:
        if not df.empty:
            for record in df.to_dict(orient="records"):
                json.dump(record, f, default=str)
                f.write("\n")


def save_match_report(report: MatchReport, state_path: Path | None = None) -> Path:
    """
    Save match report to state directory.

    Args:
        report: The MatchReport to save.
        state_path: Path to state directory. Defaults to ~/.dal/data/state.

    Returns:
        Path to the saved report file.
    """
    if state_path is None:
        state_path = get_default_state_path()

    state_path.mkdir(parents=True, exist_ok=True)
    report_file = state_path / "match_report.json"

    with open(report_file, "w") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)

    return report_file


def unify_sessions(
    new_spans: pd.DataFrame | list[dict[str, Any]],
    existing_file: Path | None = None,
    output_file: Path | None = None,
    state_path: Path | None = None,
) -> tuple[pd.DataFrame, MatchReport]:
    """
    Unify new spans with existing session data.

    Merges new spans into existing sessions, detecting continuations (same session_id),
    deduplicating by span_id, and preserving temporal ordering within each session.

    Args:
        new_spans: New spans as DataFrame or list of dicts.
        existing_file: Path to existing sessions JSONL file. If None or doesn't exist,
            treats all spans as new sessions.
        output_file: Path to write unified output. If None, doesn't write to file.
        state_path: Path to state directory for match report. Defaults to ~/.dal/data/state.

    Returns:
        Tuple of (unified DataFrame, MatchReport with unification details)

    Example:
        >>> new_df = pd.DataFrame([{"span_id": "1", "session_id": "abc"}])
        >>> unified, report = unify_sessions(new_df, Path("existing.jsonl"))
        >>> print(f"Added {report.spans_added} new spans")
    """
    # Convert input to DataFrame
    if isinstance(new_spans, list):
        new_df = pd.DataFrame(new_spans) if new_spans else pd.DataFrame()
    else:
        new_df = new_spans.copy() if not new_spans.empty else pd.DataFrame()

    # Extract session IDs for new spans
    new_df = _extract_session_ids(new_df)

    # Load existing data
    if existing_file and existing_file.exists():
        existing_df = read_sessions_file(existing_file)
        existing_df = _extract_session_ids(existing_df)
    else:
        existing_df = pd.DataFrame()

    # Get existing session IDs
    existing_session_ids = set()
    if not existing_df.empty and "session_id" in existing_df.columns:
        existing_session_ids = set(
            existing_df["session_id"].dropna().unique().tolist()
        )

    # Get new session IDs
    new_session_ids = set()
    if not new_df.empty and "session_id" in new_df.columns:
        new_session_ids = set(new_df["session_id"].dropna().unique().tolist())

    # Classify sessions
    continued_sessions = list(existing_session_ids & new_session_ids)
    new_sessions = list(new_session_ids - existing_session_ids)

    # Create report
    report = MatchReport(
        timestamp=datetime.now().isoformat(),
        new_sessions=sorted(new_sessions),
        continued_sessions=sorted(continued_sessions),
        total_spans_before=len(existing_df) + len(new_df),
    )

    # Merge dataframes
    if not existing_df.empty and not new_df.empty:
        # Ensure consistent columns
        all_columns = list(set(existing_df.columns) | set(new_df.columns))
        for col in all_columns:
            if col not in existing_df.columns:
                existing_df[col] = None
            if col not in new_df.columns:
                new_df[col] = None

        unified_df = pd.concat([existing_df, new_df], ignore_index=True)
    elif not new_df.empty:
        unified_df = new_df.copy()
    elif not existing_df.empty:
        unified_df = existing_df.copy()
    else:
        unified_df = pd.DataFrame()

    # Calculate spans added before dedup
    report.spans_added = len(new_df)

    # Deduplicate
    unified_df, duplicates_removed = _deduplicate_spans(unified_df)
    report.duplicates_removed = duplicates_removed

    # Sort by time
    unified_df = _sort_by_time(unified_df)

    report.total_spans_after = len(unified_df)

    # Save match report
    save_match_report(report, state_path)

    # Write output file if specified
    if output_file:
        write_sessions_file(unified_df, output_file)

    return unified_df, report


def get_session_spans(
    df: pd.DataFrame, session_id: str
) -> pd.DataFrame:
    """
    Get all spans for a specific session.

    Args:
        df: DataFrame containing spans.
        session_id: The session ID to filter by.

    Returns:
        DataFrame containing only spans from the specified session,
        sorted by start_time.
    """
    if df.empty:
        return pd.DataFrame()

    df = _extract_session_ids(df)

    if "session_id" not in df.columns:
        return pd.DataFrame()

    session_df = df[df["session_id"] == session_id].copy()
    return _sort_by_time(session_df)


def list_sessions(df: pd.DataFrame) -> list[dict[str, Any]]:
    """
    List all sessions in a DataFrame with summary info.

    Args:
        df: DataFrame containing spans.

    Returns:
        List of session summaries with id, span_count, and time range.
    """
    if df.empty:
        return []

    df = _extract_session_ids(df)

    if "session_id" not in df.columns:
        return []

    sessions = []
    for session_id in df["session_id"].dropna().unique():
        session_df = df[df["session_id"] == session_id]
        start_time = None
        end_time = None

        if "start_time" in session_df.columns:
            start_times = session_df["start_time"].dropna()
            if len(start_times) > 0:
                start_time = str(start_times.min())

        if "end_time" in session_df.columns:
            end_times = session_df["end_time"].dropna()
            if len(end_times) > 0:
                end_time = str(end_times.max())

        sessions.append({
            "session_id": session_id,
            "span_count": len(session_df),
            "start_time": start_time,
            "end_time": end_time,
        })

    return sessions
