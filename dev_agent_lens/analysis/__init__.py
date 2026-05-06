"""
Analysis Module

Provides span classification, aggregation, and metrics for trace data analysis.
"""

from dev_agent_lens.analysis.aggregate import (
    AggregateStats,
    ToolStats,
    aggregate_tools,
    get_slowest_tools,
    get_top_tools,
)
from dev_agent_lens.analysis.churn import (
    ChurnMetrics,
    FileChurn,
    detect_churn,
    get_churn_summary,
)
from dev_agent_lens.analysis.classify import (
    ClassificationResult,
    SpanCategory,
    classify_span,
    classify_spans,
    get_classification_summary,
)
from dev_agent_lens.analysis.failures import (
    Failure,
    FailureAnalysis,
    FailureType,
    detect_failures,
    get_failure_summary,
)
from dev_agent_lens.analysis.sessions import (
    SessionMetrics,
    aggregate_session_metrics,
    compute_session_metrics_batch,
    session_metrics,
)
from dev_agent_lens.analysis.subsets import (
    CoverageReport,
    SubsetRelationship,
    analyze_coverage,
    detect_subsets,
    get_deletable_sessions,
)
from dev_agent_lens.analysis.tokens import (
    TokenBreakdown,
    analyze_session_tokens,
    analyze_span_tokens,
    estimate_cost,
)

__all__ = [
    # Classification
    "ClassificationResult",
    "SpanCategory",
    "classify_span",
    "classify_spans",
    "get_classification_summary",
    # Aggregation
    "AggregateStats",
    "ToolStats",
    "aggregate_tools",
    "get_top_tools",
    "get_slowest_tools",
    # Failures
    "Failure",
    "FailureAnalysis",
    "FailureType",
    "detect_failures",
    "get_failure_summary",
    # Sessions
    "SessionMetrics",
    "session_metrics",
    "compute_session_metrics_batch",
    "aggregate_session_metrics",
    # Churn
    "ChurnMetrics",
    "FileChurn",
    "detect_churn",
    "get_churn_summary",
    # Tokens (Story 3.7)
    "TokenBreakdown",
    "analyze_span_tokens",
    "analyze_session_tokens",
    "estimate_cost",
    # Subsets (Story 3.8)
    "SubsetRelationship",
    "detect_subsets",
    "get_deletable_sessions",
    # Coverage (Story 3.9)
    "CoverageReport",
    "analyze_coverage",
]
