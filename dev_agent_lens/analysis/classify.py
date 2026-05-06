"""
Span Classifier Module

Classifies trace spans into categories for analysis and aggregation.

Categories:
- main: Primary LLM response spans (Sonnet, Opus models)
- tools: Tool execution spans (Read, Write, Bash, etc.)
- haiku_holdover: Haiku model spans (lightweight operations)
- internal: Internal processing spans (prompts, outputs)
- quota: Rate limiting or quota-related spans
- unknown: Unrecognized span types
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


def _safe_str(value: Any) -> str:
    """Convert value to string, handling NaN and None gracefully."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value) if value else ""


class SpanCategory(str, Enum):
    """Categories for span classification."""

    MAIN = "main"
    TOOLS = "tools"
    HAIKU_HOLDOVER = "haiku_holdover"
    INTERNAL = "internal"
    QUOTA = "quota"
    UNKNOWN = "unknown"


@dataclass
class ClassificationResult:
    """Result of span classification."""

    category: SpanCategory
    confidence: float  # 0.0 to 1.0
    reason: str  # Explanation for classification


# Model patterns for classification
MAIN_MODEL_PATTERNS = [
    r"claude-.*sonnet",
    r"claude-.*opus",
    r"claude-4",
    r"claude-sonnet",
    r"claude-opus",
]

HAIKU_MODEL_PATTERNS = [
    r"claude-.*haiku",
    r"claude-3-haiku",
    r"claude-haiku",
]

# Tool span name patterns
TOOL_NAME_PATTERNS = [
    r"Claude_Code_Tool_.*",
    r"tool_.*",
    r".*_tool$",
]

# Internal span name patterns
INTERNAL_NAME_PATTERNS = [
    r"Claude_Code_Internal_.*",
    r"Claude_Code_Final_Output.*",
    r"raw_gen_ai_request",
    r"litellm_request",
]

# Quota/rate limit patterns
QUOTA_PATTERNS = [
    r"quota",
    r"rate_limit",
    r"throttle",
]


def _matches_any_pattern(value: str | None, patterns: list[str]) -> bool:
    """Check if value matches any of the regex patterns."""
    if not value:
        return False
    for pattern in patterns:
        if re.search(pattern, value, re.IGNORECASE):
            return True
    return False


def classify_span(span: dict[str, Any]) -> ClassificationResult:
    """
    Classify a span into a category.

    Classification priority:
    1. Tool spans (by span_kind or name pattern)
    2. Haiku model spans
    3. Main model spans (Sonnet/Opus)
    4. Internal spans
    5. Quota spans
    6. Unknown

    Args:
        span: A span dictionary with fields like name, span_kind, llm_model_name

    Returns:
        ClassificationResult with category, confidence, and reason
    """
    name = _safe_str(span.get("name"))
    span_kind = _safe_str(span.get("span_kind"))
    model_name = _safe_str(span.get("llm_model_name"))
    status_code = _safe_str(span.get("status_code"))

    # 1. Check for tool spans first (most common in Claude Code traces)
    if span_kind.upper() == "TOOL":
        return ClassificationResult(
            category=SpanCategory.TOOLS,
            confidence=1.0,
            reason=f"span_kind is TOOL",
        )

    if _matches_any_pattern(name, TOOL_NAME_PATTERNS):
        return ClassificationResult(
            category=SpanCategory.TOOLS,
            confidence=0.95,
            reason=f"name matches tool pattern: {name}",
        )

    # 2. Check for Haiku model spans
    if _matches_any_pattern(model_name, HAIKU_MODEL_PATTERNS):
        return ClassificationResult(
            category=SpanCategory.HAIKU_HOLDOVER,
            confidence=1.0,
            reason=f"model is Haiku variant: {model_name}",
        )

    # 3. Check for main model spans (Sonnet/Opus)
    if _matches_any_pattern(model_name, MAIN_MODEL_PATTERNS):
        return ClassificationResult(
            category=SpanCategory.MAIN,
            confidence=1.0,
            reason=f"model is main variant: {model_name}",
        )

    # 4. Check for LLM spans without specific model (likely main)
    if span_kind.upper() == "LLM":
        # LLM span without recognized model - classify by name
        if _matches_any_pattern(name, INTERNAL_NAME_PATTERNS):
            return ClassificationResult(
                category=SpanCategory.INTERNAL,
                confidence=0.8,
                reason=f"LLM span with internal name pattern: {name}",
            )
        return ClassificationResult(
            category=SpanCategory.MAIN,
            confidence=0.7,
            reason=f"LLM span_kind without specific model",
        )

    # 5. Check for internal spans by name
    if _matches_any_pattern(name, INTERNAL_NAME_PATTERNS):
        return ClassificationResult(
            category=SpanCategory.INTERNAL,
            confidence=0.9,
            reason=f"name matches internal pattern: {name}",
        )

    # 6. Check for quota spans
    if _matches_any_pattern(name, QUOTA_PATTERNS) or _matches_any_pattern(
        status_code, QUOTA_PATTERNS
    ):
        return ClassificationResult(
            category=SpanCategory.QUOTA,
            confidence=0.9,
            reason=f"matches quota pattern",
        )

    # 7. Unknown - couldn't classify
    return ClassificationResult(
        category=SpanCategory.UNKNOWN,
        confidence=0.5,
        reason=f"no matching classification pattern (name={name}, kind={span_kind})",
    )


def classify_spans(spans: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Classify multiple spans and group by category.

    Args:
        spans: List of span dictionaries

    Returns:
        Dictionary mapping category names to lists of spans
    """
    result: dict[str, list[dict[str, Any]]] = {
        category.value: [] for category in SpanCategory
    }

    for span in spans:
        classification = classify_span(span)
        result[classification.category.value].append(span)

    return result


def get_classification_summary(spans: list[dict[str, Any]]) -> dict[str, int]:
    """
    Get a summary count of spans by category.

    Args:
        spans: List of span dictionaries

    Returns:
        Dictionary mapping category names to counts
    """
    classified = classify_spans(spans)
    return {category: len(spans_list) for category, spans_list in classified.items()}
