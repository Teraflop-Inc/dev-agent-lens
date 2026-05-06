"""
Failure Detector Module

Detects and analyzes failures in trace spans.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from dev_agent_lens.analysis.classify import classify_span


def _safe_str(value: Any) -> str:
    """Convert value to string, handling NaN and None gracefully."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value) if value else ""


class FailureType(str, Enum):
    """Types of failures that can be detected."""

    ERROR = "error"
    ABORT = "abort"
    BACK_TO_BACK = "back_to_back"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"


@dataclass
class Failure:
    """Represents a detected failure."""

    failure_type: FailureType
    span: dict[str, Any]
    context: list[dict[str, Any]]  # Previous spans for context
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "failure_type": self.failure_type.value,
            "span_id": self.span.get("span_id"),
            "span_name": self.span.get("name"),
            "status_code": self.span.get("status_code"),
            "reason": self.reason,
            "context_span_ids": [s.get("span_id") for s in self.context],
        }


@dataclass
class FailureAnalysis:
    """Complete failure analysis for a set of spans."""

    failures: list[Failure] = field(default_factory=list)
    error_count: int = 0
    abort_count: int = 0
    back_to_back_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0

    @property
    def total_failures(self) -> int:
        """Total number of failures."""
        return len(self.failures)

    def by_type(self, failure_type: FailureType) -> list[Failure]:
        """Get failures of a specific type."""
        return [f for f in self.failures if f.failure_type == failure_type]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "total_failures": self.total_failures,
            "error_count": self.error_count,
            "abort_count": self.abort_count,
            "back_to_back_count": self.back_to_back_count,
            "timeout_count": self.timeout_count,
            "rate_limit_count": self.rate_limit_count,
            "failures": [f.to_dict() for f in self.failures],
        }


def _is_error_status(span: dict[str, Any]) -> bool:
    """Check if span has an error status."""
    status = _safe_str(span.get("status_code")).upper()
    return status in ("ERROR", "FAILED", "FAILURE")


def _is_abort(span: dict[str, Any]) -> bool:
    """Check if span indicates a user abort."""
    name = _safe_str(span.get("name")).lower()
    status_message = _safe_str(span.get("status_message")).lower()

    abort_indicators = ["abort", "cancel", "interrupt", "sigint", "sigterm"]
    combined = f"{name} {status_message}"

    return any(indicator in combined for indicator in abort_indicators)


def _is_timeout(span: dict[str, Any]) -> bool:
    """Check if span indicates a timeout."""
    status_message = _safe_str(span.get("status_message")).lower()
    name = _safe_str(span.get("name")).lower()

    timeout_indicators = ["timeout", "timed out", "deadline exceeded"]
    combined = f"{name} {status_message}"

    return any(indicator in combined for indicator in timeout_indicators)


def _is_rate_limit(span: dict[str, Any]) -> bool:
    """Check if span indicates rate limiting."""
    status_message = _safe_str(span.get("status_message")).lower()
    name = _safe_str(span.get("name")).lower()

    rate_limit_indicators = ["rate limit", "ratelimit", "429", "quota", "throttle"]
    combined = f"{name} {status_message}"

    return any(indicator in combined for indicator in rate_limit_indicators)


def _are_back_to_back(span1: dict[str, Any], span2: dict[str, Any]) -> bool:
    """Check if two spans represent back-to-back identical calls."""
    # Same name
    if span1.get("name") != span2.get("name"):
        return False

    # Same input value (if present)
    input1 = span1.get("input_value")
    input2 = span2.get("input_value")
    if input1 and input2 and input1 == input2:
        return True

    # Same tool with same arguments
    classification1 = classify_span(span1)
    classification2 = classify_span(span2)
    if classification1.category == classification2.category:
        # Check raw attributes for identical arguments
        attrs1 = span1.get("raw_attributes", {})
        attrs2 = span2.get("raw_attributes", {})
        if attrs1 and attrs2 and attrs1 == attrs2:
            return True

    return False


def _get_context(spans: list[dict[str, Any]], index: int, context_size: int = 3) -> list[dict[str, Any]]:
    """Get context spans before the given index."""
    start = max(0, index - context_size)
    return spans[start:index]


def detect_failures(
    spans: list[dict[str, Any]],
    context_size: int = 3,
) -> FailureAnalysis:
    """
    Detect failures in a list of spans.

    Args:
        spans: List of span dictionaries (should be in chronological order)
        context_size: Number of previous spans to include as context

    Returns:
        FailureAnalysis with detected failures and counts
    """
    analysis = FailureAnalysis()

    for i, span in enumerate(spans):
        context = _get_context(spans, i, context_size)

        # Check for error status
        if _is_error_status(span):
            # Determine specific error type
            if _is_timeout(span):
                failure = Failure(
                    failure_type=FailureType.TIMEOUT,
                    span=span,
                    context=context,
                    reason="Timeout detected",
                )
                analysis.failures.append(failure)
                analysis.timeout_count += 1
            elif _is_rate_limit(span):
                failure = Failure(
                    failure_type=FailureType.RATE_LIMIT,
                    span=span,
                    context=context,
                    reason="Rate limit detected",
                )
                analysis.failures.append(failure)
                analysis.rate_limit_count += 1
            else:
                failure = Failure(
                    failure_type=FailureType.ERROR,
                    span=span,
                    context=context,
                    reason=f"Error status: {span.get('status_code')}",
                )
                analysis.failures.append(failure)
                analysis.error_count += 1

        # Check for abort patterns
        elif _is_abort(span):
            failure = Failure(
                failure_type=FailureType.ABORT,
                span=span,
                context=context,
                reason="User abort detected",
            )
            analysis.failures.append(failure)
            analysis.abort_count += 1

        # Check for back-to-back identical calls
        if i > 0 and _are_back_to_back(spans[i - 1], span):
            failure = Failure(
                failure_type=FailureType.BACK_TO_BACK,
                span=span,
                context=context,
                reason=f"Repeated call: {span.get('name')}",
            )
            analysis.failures.append(failure)
            analysis.back_to_back_count += 1

    return analysis


def get_failure_summary(analysis: FailureAnalysis) -> dict[str, Any]:
    """
    Get a summary of failures.

    Args:
        analysis: FailureAnalysis object

    Returns:
        Dictionary with failure summary
    """
    return {
        "total_failures": analysis.total_failures,
        "by_type": {
            "errors": analysis.error_count,
            "aborts": analysis.abort_count,
            "back_to_back": analysis.back_to_back_count,
            "timeouts": analysis.timeout_count,
            "rate_limits": analysis.rate_limit_count,
        },
    }
