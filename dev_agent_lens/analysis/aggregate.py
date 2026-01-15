"""
Tool Call Aggregator Module

Aggregates tool call statistics from trace spans.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dev_agent_lens.analysis.classify import SpanCategory, classify_span


@dataclass
class ToolStats:
    """Statistics for a single tool type."""

    name: str
    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_duration_ms: float = 0.0
    durations: list[float] = field(default_factory=list)

    @property
    def average_duration_ms(self) -> float:
        """Average duration in milliseconds."""
        if not self.durations:
            return 0.0
        return sum(self.durations) / len(self.durations)

    @property
    def success_rate(self) -> float:
        """Success rate as a percentage (0-100)."""
        if self.total_calls == 0:
            return 0.0
        return (self.success_count / self.total_calls) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "name": self.name,
            "total_calls": self.total_calls,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_rate, 2),
            "average_duration_ms": round(self.average_duration_ms, 2),
            "total_duration_ms": round(self.total_duration_ms, 2),
        }


@dataclass
class AggregateStats:
    """Aggregated statistics for all tools."""

    tools: dict[str, ToolStats] = field(default_factory=dict)
    total_tool_calls: int = 0
    total_successes: int = 0
    total_failures: int = 0

    @property
    def overall_success_rate(self) -> float:
        """Overall success rate as a percentage."""
        if self.total_tool_calls == 0:
            return 0.0
        return (self.total_successes / self.total_tool_calls) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "total_tool_calls": self.total_tool_calls,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "overall_success_rate": round(self.overall_success_rate, 2),
            "tools": {name: stats.to_dict() for name, stats in self.tools.items()},
        }


def _parse_timestamp(ts: str | None) -> datetime | None:
    """Parse a timestamp string to datetime."""
    if not ts:
        return None
    try:
        # Handle various timestamp formats
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(ts[:26], fmt)  # Truncate nanoseconds
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _calculate_duration_ms(span: dict[str, Any]) -> float | None:
    """Calculate span duration in milliseconds."""
    start = _parse_timestamp(span.get("start_time"))
    end = _parse_timestamp(span.get("end_time"))

    if start and end:
        delta = end - start
        return delta.total_seconds() * 1000

    return None


def _extract_tool_name(span: dict[str, Any]) -> str:
    """Extract the tool name from a span."""
    name = span.get("name", "") or ""

    # Handle Claude_Code_Tool_XXX pattern
    if name.startswith("Claude_Code_Tool_"):
        return name.replace("Claude_Code_Tool_", "")

    # Handle other patterns
    if name.startswith("tool_"):
        return name.replace("tool_", "")

    return name


def _is_success(span: dict[str, Any]) -> bool:
    """Check if a span represents a successful operation."""
    status = (span.get("status_code") or "").upper()
    return status in ("OK", "SUCCESS", "")


def aggregate_tools(spans: list[dict[str, Any]]) -> AggregateStats:
    """
    Aggregate tool call statistics from spans.

    Args:
        spans: List of span dictionaries

    Returns:
        AggregateStats with tool-level and overall statistics
    """
    stats = AggregateStats()

    for span in spans:
        # Only process tool spans
        classification = classify_span(span)
        if classification.category != SpanCategory.TOOLS:
            continue

        tool_name = _extract_tool_name(span)
        if not tool_name:
            tool_name = "unknown"

        # Get or create tool stats
        if tool_name not in stats.tools:
            stats.tools[tool_name] = ToolStats(name=tool_name)

        tool_stats = stats.tools[tool_name]
        tool_stats.total_calls += 1
        stats.total_tool_calls += 1

        # Track success/failure
        if _is_success(span):
            tool_stats.success_count += 1
            stats.total_successes += 1
        else:
            tool_stats.failure_count += 1
            stats.total_failures += 1

        # Track duration
        duration = _calculate_duration_ms(span)
        if duration is not None:
            tool_stats.durations.append(duration)
            tool_stats.total_duration_ms += duration

    return stats


def get_top_tools(stats: AggregateStats, n: int = 10) -> list[dict[str, Any]]:
    """
    Get the top N most frequently used tools.

    Args:
        stats: AggregateStats object
        n: Number of top tools to return

    Returns:
        List of tool statistics sorted by call count
    """
    sorted_tools = sorted(
        stats.tools.values(),
        key=lambda t: t.total_calls,
        reverse=True,
    )
    return [t.to_dict() for t in sorted_tools[:n]]


def get_slowest_tools(stats: AggregateStats, n: int = 10) -> list[dict[str, Any]]:
    """
    Get the N slowest tools by average duration.

    Args:
        stats: AggregateStats object
        n: Number of tools to return

    Returns:
        List of tool statistics sorted by average duration
    """
    tools_with_duration = [t for t in stats.tools.values() if t.durations]
    sorted_tools = sorted(
        tools_with_duration,
        key=lambda t: t.average_duration_ms,
        reverse=True,
    )
    return [t.to_dict() for t in sorted_tools[:n]]
