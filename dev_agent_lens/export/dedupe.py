"""
Deduplication and cleaning functions for unified session data.

This module provides functions to reduce the size of session data by:
1. Removing duplicate fields from raw_attributes that exist in normalized form
2. Stripping null/empty/NaN values from raw_attributes
3. Preserving only unique metadata in raw_attributes

The deduplication happens at export time, preserving full data locally.
"""

from __future__ import annotations

import json
import math
from typing import Any


# Fields in raw_attributes that are duplicated in normalized span fields
# These can be safely removed since the data exists elsewhere
DUPLICATED_FIELDS = {
    # Direct duplicates with normalized fields
    "context.span_id",
    "context.trace_id",
    "name",
    "span_kind",
    "start_time",
    "end_time",
    "status_code",
    "status_message",
    "parent_id",
    # Input/output value duplicates
    "attributes.input.value",
    "attributes.output.value",
    # LLM-specific duplicates
    "attributes.llm.model_name",
    "attributes.llm.input_messages",
    "attributes.llm.output_messages",
    "attributes.llm.token_count.prompt",
    "attributes.llm.token_count.completion",
    "attributes.llm.token_count.total",
    # OpenInference duplicates
    "attributes.openinference.span.kind",
}

# Fields in raw_attributes that contain unique data worth preserving
# These should NOT be removed during deduplication
KEEP_FIELDS = {
    # LLM configuration and parameters
    "attributes.llm.invocation_parameters",
    "attributes.llm",  # Nested LLM config (when not None-prefixed)
    # Events and errors
    "events",
    # Metadata
    "attributes.metadata",
    # MIME types
    "attributes.input.mime_type",
    "attributes.output.mime_type",
    # Provider info
    "attributes.llm.provider",
    "attributes.llm.system",
    # Tool usage
    "attributes.claude_code_tool_name",
    # Request details
    "attributes.llm.request.type",
    "attributes.llm.request.max_tokens",
    "attributes.llm.request.temperature",
    "attributes.llm.is_streaming",
    # Response details
    "attributes.llm.response.id",
    "attributes.llm.response.model",
    # Timing
    "time",
    "latency_ms",
}


def is_empty(value: Any) -> bool:
    """Check if a value is empty/null/NaN.

    Args:
        value: Any value to check.

    Returns:
        True if the value is considered empty.
    """
    if value is None:
        return True
    if value == "":
        return True
    if value == []:
        return True
    if value == {}:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if str(value) == "nan":
        return True
    return False


def strip_empty_values(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively strip empty/null/NaN values from a dictionary.

    Args:
        data: Dictionary to clean.

    Returns:
        New dictionary with empty values removed.
    """
    result = {}
    for key, value in data.items():
        if is_empty(value):
            continue
        if isinstance(value, dict):
            cleaned = strip_empty_values(value)
            if cleaned:  # Only include non-empty dicts
                result[key] = cleaned
        else:
            result[key] = value
    return result


def deduplicate_raw_attributes(raw_attributes: dict[str, Any]) -> dict[str, Any]:
    """Remove duplicated fields from raw_attributes.

    Removes fields that are duplicated in normalized span fields,
    keeping only unique metadata.

    Args:
        raw_attributes: The raw_attributes dict from a span.

    Returns:
        New dictionary with duplicated fields removed.
    """
    result = {}
    for key, value in raw_attributes.items():
        # Skip known duplicated fields
        if key in DUPLICATED_FIELDS:
            continue
        result[key] = value
    return result


def deduplicate_span(span: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate a single span's raw_attributes.

    Args:
        span: A span dictionary with raw_attributes.

    Returns:
        New span dictionary with deduplicated raw_attributes.
    """
    result = span.copy()

    if "raw_attributes" in result and isinstance(result["raw_attributes"], dict):
        # First deduplicate, then strip empty values
        deduped = deduplicate_raw_attributes(result["raw_attributes"])
        cleaned = strip_empty_values(deduped)
        result["raw_attributes"] = cleaned

    return result


def deduplicate_session(session: dict[str, Any]) -> dict[str, Any]:
    """Deduplicate a session by cleaning all spans' raw_attributes.

    Args:
        session: A session dictionary with spans array.

    Returns:
        New session dictionary with deduplicated spans.
    """
    result = session.copy()

    if "spans" in result and isinstance(result["spans"], list):
        result["spans"] = [deduplicate_span(span) for span in result["spans"]]

    return result


def clean_session(
    session: dict[str, Any],
    dedupe: bool = True,
    strip_nulls: bool = True,
) -> dict[str, Any]:
    """Clean a session by deduplicating and stripping empty values.

    This is the main entry point for session cleaning. It applies
    both deduplication and null-stripping based on flags.

    Args:
        session: A session dictionary with spans array.
        dedupe: If True, remove duplicated fields from raw_attributes.
        strip_nulls: If True, strip null/empty values from raw_attributes.

    Returns:
        New session dictionary with cleaned spans.
    """
    result = session.copy()

    if "spans" not in result or not isinstance(result["spans"], list):
        return result

    cleaned_spans = []
    for span in result["spans"]:
        cleaned_span = span.copy()

        if "raw_attributes" in cleaned_span and isinstance(cleaned_span["raw_attributes"], dict):
            raw_attrs = cleaned_span["raw_attributes"]

            if dedupe:
                raw_attrs = deduplicate_raw_attributes(raw_attrs)

            if strip_nulls:
                raw_attrs = strip_empty_values(raw_attrs)

            cleaned_span["raw_attributes"] = raw_attrs

        cleaned_spans.append(cleaned_span)

    result["spans"] = cleaned_spans
    return result


def calculate_savings(original: dict[str, Any], cleaned: dict[str, Any]) -> dict[str, Any]:
    """Calculate size savings from cleaning a session.

    Args:
        original: Original session dictionary.
        cleaned: Cleaned session dictionary.

    Returns:
        Dictionary with size metrics.
    """
    original_size = len(json.dumps(original))
    cleaned_size = len(json.dumps(cleaned))
    savings = original_size - cleaned_size

    return {
        "original_bytes": original_size,
        "cleaned_bytes": cleaned_size,
        "savings_bytes": savings,
        "savings_percent": (savings / original_size * 100) if original_size > 0 else 0,
    }


def clean_sessions_file(
    input_path: str,
    output_path: str,
    dedupe: bool = True,
    strip_nulls: bool = True,
    progress_callback: callable | None = None,
) -> dict[str, Any]:
    """Clean a JSONL sessions file.

    Args:
        input_path: Path to input JSONL file.
        output_path: Path to output JSONL file.
        dedupe: If True, remove duplicated fields from raw_attributes.
        strip_nulls: If True, strip null/empty values from raw_attributes.
        progress_callback: Optional callback(sessions_processed, total_bytes_saved).

    Returns:
        Dictionary with cleaning statistics.
    """
    stats = {
        "sessions_processed": 0,
        "spans_processed": 0,
        "original_bytes": 0,
        "cleaned_bytes": 0,
        "savings_bytes": 0,
    }

    with open(input_path, "r") as infile, open(output_path, "w") as outfile:
        for line in infile:
            line = line.strip()
            if not line:
                continue

            session = json.loads(line)
            cleaned = clean_session(session, dedupe=dedupe, strip_nulls=strip_nulls)

            original_size = len(line)
            cleaned_line = json.dumps(cleaned)
            cleaned_size = len(cleaned_line)

            outfile.write(cleaned_line + "\n")

            stats["sessions_processed"] += 1
            stats["spans_processed"] += len(session.get("spans", []))
            stats["original_bytes"] += original_size
            stats["cleaned_bytes"] += cleaned_size
            stats["savings_bytes"] += original_size - cleaned_size

            if progress_callback:
                progress_callback(stats["sessions_processed"], stats["savings_bytes"])

    stats["savings_percent"] = (
        (stats["savings_bytes"] / stats["original_bytes"] * 100)
        if stats["original_bytes"] > 0
        else 0
    )

    return stats
