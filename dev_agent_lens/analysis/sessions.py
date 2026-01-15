"""
Session Metrics Module

Computes session-level statistics from trace spans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dev_agent_lens.analysis.classify import SpanCategory, classify_span


@dataclass
class SessionMetrics:
    """Metrics for a single session."""

    session_id: str | None
    turn_count: int
    total_duration_seconds: float
    token_count_prompt: int
    token_count_completion: int
    token_count_total: int
    tool_call_count: int
    failure_count: int
    first_activity: datetime | None
    last_activity: datetime | None
    span_count: int
    main_model_calls: int
    haiku_calls: int

    @property
    def duration_minutes(self) -> float:
        """Duration in minutes."""
        return self.total_duration_seconds / 60.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "session_id": self.session_id,
            "turn_count": self.turn_count,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "duration_minutes": round(self.duration_minutes, 2),
            "token_count_prompt": self.token_count_prompt,
            "token_count_completion": self.token_count_completion,
            "token_count_total": self.token_count_total,
            "tool_call_count": self.tool_call_count,
            "failure_count": self.failure_count,
            "first_activity": self.first_activity.isoformat() if self.first_activity else None,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "span_count": self.span_count,
            "main_model_calls": self.main_model_calls,
            "haiku_calls": self.haiku_calls,
        }


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse a timestamp string to datetime."""
    if not ts:
        return None
    try:
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(ts[:26], fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _get_token_count(span: dict[str, Any], field: str) -> int:
    """Get token count from span, handling various formats."""
    value = span.get(field)
    if value is None:
        return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _is_user_turn(span: dict[str, Any]) -> bool:
    """
    Check if span represents a user turn.

    A user turn is typically an LLM call that processes user input.
    """
    name = span.get("name", "") or ""
    classification = classify_span(span)

    # Main model calls typically represent turns
    if classification.category == SpanCategory.MAIN:
        return True

    # Haiku calls for lightweight operations are not full turns
    if classification.category == SpanCategory.HAIKU_HOLDOVER:
        return False

    # Internal prompts may indicate turn boundaries
    if "Internal_Prompt" in name or "Final_Output" in name:
        return True

    return False


def _is_failure(span: dict[str, Any]) -> bool:
    """Check if span represents a failure."""
    status = (span.get("status_code") or "").upper()
    return status in ("ERROR", "FAILED", "FAILURE")


def session_metrics(
    spans: list[dict[str, Any]],
    session_id: str | None = None,
) -> SessionMetrics:
    """
    Compute metrics for a session.

    Args:
        spans: List of span dictionaries for the session
        session_id: Optional session identifier

    Returns:
        SessionMetrics with computed statistics
    """
    if not spans:
        return SessionMetrics(
            session_id=session_id,
            turn_count=0,
            total_duration_seconds=0.0,
            token_count_prompt=0,
            token_count_completion=0,
            token_count_total=0,
            tool_call_count=0,
            failure_count=0,
            first_activity=None,
            last_activity=None,
            span_count=0,
            main_model_calls=0,
            haiku_calls=0,
        )

    # Collect timestamps
    timestamps: list[datetime] = []
    for span in spans:
        start_ts = _parse_timestamp(span.get("start_time"))
        end_ts = _parse_timestamp(span.get("end_time"))
        if start_ts:
            timestamps.append(start_ts)
        if end_ts:
            timestamps.append(end_ts)

    # Calculate time range
    first_activity = min(timestamps) if timestamps else None
    last_activity = max(timestamps) if timestamps else None
    total_duration = 0.0
    if first_activity and last_activity:
        total_duration = (last_activity - first_activity).total_seconds()

    # Count metrics
    turn_count = 0
    tool_call_count = 0
    failure_count = 0
    main_model_calls = 0
    haiku_calls = 0
    token_prompt = 0
    token_completion = 0
    token_total = 0

    for span in spans:
        classification = classify_span(span)

        # Count turns
        if _is_user_turn(span):
            turn_count += 1

        # Count tool calls
        if classification.category == SpanCategory.TOOLS:
            tool_call_count += 1

        # Count failures
        if _is_failure(span):
            failure_count += 1

        # Count model calls
        if classification.category == SpanCategory.MAIN:
            main_model_calls += 1
        elif classification.category == SpanCategory.HAIKU_HOLDOVER:
            haiku_calls += 1

        # Sum tokens
        token_prompt += _get_token_count(span, "llm_token_count_prompt")
        token_completion += _get_token_count(span, "llm_token_count_completion")
        token_total += _get_token_count(span, "llm_token_count_total")

    # If total wasn't recorded, calculate from parts
    if token_total == 0:
        token_total = token_prompt + token_completion

    return SessionMetrics(
        session_id=session_id,
        turn_count=turn_count,
        total_duration_seconds=total_duration,
        token_count_prompt=token_prompt,
        token_count_completion=token_completion,
        token_count_total=token_total,
        tool_call_count=tool_call_count,
        failure_count=failure_count,
        first_activity=first_activity,
        last_activity=last_activity,
        span_count=len(spans),
        main_model_calls=main_model_calls,
        haiku_calls=haiku_calls,
    )


def compute_session_metrics_batch(
    sessions: list[dict[str, Any]],
) -> list[SessionMetrics]:
    """
    Compute metrics for multiple sessions.

    Args:
        sessions: List of session dictionaries with 'session_id' and 'spans' keys

    Returns:
        List of SessionMetrics for each session
    """
    results = []
    for session in sessions:
        session_id = session.get("session_id")
        spans = session.get("spans", [])
        metrics = session_metrics(spans, session_id)
        results.append(metrics)
    return results


def aggregate_session_metrics(metrics_list: list[SessionMetrics]) -> dict[str, Any]:
    """
    Aggregate metrics across multiple sessions.

    Args:
        metrics_list: List of SessionMetrics objects

    Returns:
        Dictionary with aggregated statistics
    """
    if not metrics_list:
        return {
            "session_count": 0,
            "total_turns": 0,
            "total_tool_calls": 0,
            "total_failures": 0,
            "total_tokens": 0,
            "avg_turns_per_session": 0.0,
            "avg_duration_minutes": 0.0,
        }

    total_turns = sum(m.turn_count for m in metrics_list)
    total_tool_calls = sum(m.tool_call_count for m in metrics_list)
    total_failures = sum(m.failure_count for m in metrics_list)
    total_tokens = sum(m.token_count_total for m in metrics_list)
    total_duration = sum(m.total_duration_seconds for m in metrics_list)
    session_count = len(metrics_list)

    return {
        "session_count": session_count,
        "total_turns": total_turns,
        "total_tool_calls": total_tool_calls,
        "total_failures": total_failures,
        "total_tokens": total_tokens,
        "avg_turns_per_session": round(total_turns / session_count, 2),
        "avg_duration_minutes": round((total_duration / session_count) / 60, 2),
        "avg_tokens_per_session": round(total_tokens / session_count, 2),
    }
