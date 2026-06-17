"""
Unified Schema Module

Provides a canonical schema for trace spans from Phoenix and Arize backends,
along with normalization functions to convert backend-specific DataFrames
to the unified format.

Field Mappings:
    See SCHEMA.md in the repository root for detailed field mappings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

import pandas as pd

from dev_agent_lens.core.session import (
    extract_account_id_from_span,
    extract_user_id_from_span,
)


class UnifiedSpan(TypedDict, total=False):
    """
    Canonical schema for trace spans.

    All fields are optional (total=False) to handle missing data gracefully.
    Timestamps are normalized to ISO-8601 strings for consistency.

    Core Fields:
        span_id: Unique identifier for this span
        trace_id: Identifier for the trace this span belongs to
        parent_id: ID of the parent span (None for root spans)
        name: Name/label of the span
        span_kind: Type of span (LLM, TOOL, CHAIN, etc.)
        start_time: ISO-8601 timestamp when span started
        end_time: ISO-8601 timestamp when span ended
        status_code: Status of the span (OK, ERROR, etc.)

    Content Fields:
        input_value: Input text/content for this span
        output_value: Output text/content from this span
        input_messages: Structured LLM input messages (JSON string)
        output_messages: Structured LLM output messages (JSON string)

    LLM Fields:
        llm_model_name: Name of the LLM model used
        llm_token_count_prompt: Number of prompt tokens
        llm_token_count_completion: Number of completion tokens
        llm_token_count_total: Total token count

    Metadata:
        backend: Source backend ('phoenix' or 'arize')
        raw_attributes: Original attributes as JSON string
    """

    # Core fields
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    span_kind: str | None
    start_time: str  # ISO-8601
    end_time: str | None  # ISO-8601
    status_code: str | None

    # Content fields
    input_value: str | None
    output_value: str | None
    input_messages: str | None  # JSON string
    output_messages: str | None  # JSON string

    # LLM fields
    llm_model_name: str | None
    llm_token_count_prompt: int | None
    llm_token_count_completion: int | None
    llm_token_count_total: int | None

    # Attribution (canonical user identity; see SCHEMA.md)
    user_id: str | None  # user hash from LiteLLM end-user string
    account_id: str | None  # account UUID from LiteLLM end-user string

    # Metadata
    backend: str
    raw_attributes: str | None  # JSON string of all original attributes


# Ordered list of columns in the unified schema
UNIFIED_COLUMNS = [
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
    "user_id",
    "account_id",
    "backend",
    "raw_attributes",
]


class UnifiedAnnotation(TypedDict, total=False):
    """
    Canonical schema for span annotations.

    All fields are optional (total=False) to handle missing data gracefully.

    Fields:
        annotation_id: Unique identifier for this annotation
        span_id: ID of the span this annotation refers to
        name: Name/type of the annotation (e.g., "helpfulness", "relevance")
        annotator_kind: Who created the annotation ("HUMAN", "LLM", "CODE")
        label: Categorical label (e.g., "good", "bad", "helpful")
        score: Numeric score (typically 0.0 to 1.0)
        explanation: Text explanation of the annotation
        metadata: Additional annotation metadata as JSON string
        created_at: ISO-8601 timestamp when annotation was created
        updated_at: ISO-8601 timestamp when annotation was updated
        source: Where annotation came from ("API" or "APP")
        user_id: ID of user who created annotation (if applicable)
        backend: Source backend ('phoenix' or 'arize')
    """

    annotation_id: str
    span_id: str
    name: str
    annotator_kind: str | None  # HUMAN, LLM, CODE
    label: str | None
    score: float | None
    explanation: str | None
    metadata: str | None  # JSON string
    created_at: str  # ISO-8601
    updated_at: str | None  # ISO-8601
    source: str | None  # API or APP
    user_id: str | None
    backend: str


# Ordered list of columns in the unified annotation schema
ANNOTATION_COLUMNS = [
    "annotation_id",
    "span_id",
    "name",
    "annotator_kind",
    "label",
    "score",
    "explanation",
    "metadata",
    "created_at",
    "updated_at",
    "source",
    "user_id",
    "backend",
]


# Import numpy at module level for type checking
import numpy as np


def _is_missing(value: Any) -> bool:
    """Check if a value should be considered missing/null.

    Handles scalars, arrays, and numpy types properly.
    """
    if value is None:
        return True

    # Handle numpy arrays and lists - they're not "missing", they're values
    if isinstance(value, (list, np.ndarray)):
        return len(value) == 0

    # For scalars, use pd.isna which handles NaN, NaT, None
    try:
        return bool(pd.isna(value))
    except (ValueError, TypeError):
        # If pd.isna fails, assume it's a valid value
        return False


def _to_iso8601(value: Any) -> str | None:
    """Convert a timestamp value to ISO-8601 string."""
    if _is_missing(value):
        return None

    if isinstance(value, str):
        # Already a string, try to parse and normalize
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.isoformat()
        except ValueError:
            return value

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, (int, float)):
        # Assume milliseconds timestamp
        try:
            dt = datetime.fromtimestamp(value / 1000)
            return dt.isoformat()
        except (ValueError, OSError):
            return None

    # Try pandas Timestamp
    try:
        ts = pd.Timestamp(value)
        if _is_missing(ts):
            return None
        return ts.isoformat()
    except (ValueError, TypeError):
        return None


def _safe_str(value: Any) -> str | None:
    """Convert value to string, returning None for missing values."""
    if _is_missing(value):
        return None
    return str(value)


def _safe_int(value: Any) -> int | None:
    """Convert value to int, returning None for missing values."""
    if _is_missing(value):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _get_column(row: pd.Series, *column_names: str) -> Any:
    """Get first available column value from a list of possible names."""
    for name in column_names:
        if name in row.index:
            val = row[name]
            if not _is_missing(val):
                return val
    return None


def normalize_phoenix(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert Phoenix DataFrame to unified schema.

    Phoenix Column Mappings:
        context.span_id → span_id
        context.trace_id → trace_id
        parent_id → parent_id
        name → name
        span_kind → span_kind
        start_time → start_time (converted to ISO-8601)
        end_time → end_time (converted to ISO-8601)
        status_code → status_code
        attributes.input.value → input_value
        attributes.output.value → output_value
        attributes.llm.input_messages → input_messages
        attributes.llm.output_messages → output_messages
        attributes.llm.model_name → llm_model_name
        attributes.llm.token_count.prompt → llm_token_count_prompt
        attributes.llm.token_count.completion → llm_token_count_completion
        attributes.llm.token_count.total → llm_token_count_total

    Args:
        df: DataFrame from PhoenixClient.get_spans_dataframe()

    Returns:
        DataFrame with unified schema columns.
    """
    if df.empty:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)

    rows = []
    for _, row in df.iterrows():
        raw_dict = row.to_dict()  # Preserve all original columns for metadata extraction
        identity_span = {"raw_attributes": raw_dict}
        unified = {
            "span_id": _safe_str(_get_column(row, "context.span_id")),
            "trace_id": _safe_str(_get_column(row, "context.trace_id")),
            "parent_id": _safe_str(_get_column(row, "parent_id")),
            "name": _safe_str(_get_column(row, "name")),
            "span_kind": _safe_str(
                _get_column(row, "span_kind", "attributes.openinference.span.kind")
            ),
            "start_time": _to_iso8601(_get_column(row, "start_time")),
            "end_time": _to_iso8601(_get_column(row, "end_time")),
            "status_code": _safe_str(_get_column(row, "status_code", "status")),
            "input_value": _safe_str(_get_column(row, "attributes.input.value")),
            "output_value": _safe_str(_get_column(row, "attributes.output.value")),
            "input_messages": _safe_str(_get_column(row, "attributes.llm.input_messages")),
            "output_messages": _safe_str(_get_column(row, "attributes.llm.output_messages")),
            "llm_model_name": _safe_str(_get_column(row, "attributes.llm.model_name")),
            "llm_token_count_prompt": _safe_int(
                _get_column(row, "attributes.llm.token_count.prompt")
            ),
            "llm_token_count_completion": _safe_int(
                _get_column(row, "attributes.llm.token_count.completion")
            ),
            "llm_token_count_total": _safe_int(
                _get_column(row, "attributes.llm.token_count.total")
            ),
            "user_id": extract_user_id_from_span(identity_span),
            "account_id": extract_account_id_from_span(identity_span),
            "backend": "phoenix",
            "raw_attributes": raw_dict,
        }
        rows.append(unified)

    return pd.DataFrame(rows, columns=UNIFIED_COLUMNS)


def normalize_arize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert Arize DataFrame to unified schema.

    Arize Column Mappings:
        context.span_id → span_id
        context.trace_id → trace_id
        parent_id → parent_id
        name → name
        attributes.openinference.span.kind → span_kind
        start_time → start_time (converted from ms to ISO-8601)
        end_time → end_time (converted from ms to ISO-8601)
        status_code → status_code
        attributes.input.value → input_value
        attributes.output.value → output_value
        attributes.llm.input_messages → input_messages
        attributes.llm.output_messages → output_messages
        attributes.llm.model_name → llm_model_name
        attributes.llm.token_count.prompt → llm_token_count_prompt
        attributes.llm.token_count.completion → llm_token_count_completion
        attributes.llm.token_count.total → llm_token_count_total

    Args:
        df: DataFrame from ArizeClient.get_spans_dataframe()

    Returns:
        DataFrame with unified schema columns.
    """
    if df.empty:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)

    rows = []
    for _, row in df.iterrows():
        raw_dict = row.to_dict()  # Preserve all original columns for metadata extraction
        identity_span = {"raw_attributes": raw_dict}
        unified = {
            "span_id": _safe_str(_get_column(row, "context.span_id")),
            "trace_id": _safe_str(_get_column(row, "context.trace_id")),
            "parent_id": _safe_str(_get_column(row, "parent_id")),
            "name": _safe_str(_get_column(row, "name")),
            "span_kind": _safe_str(_get_column(row, "attributes.openinference.span.kind")),
            "start_time": _to_iso8601(_get_column(row, "start_time")),
            "end_time": _to_iso8601(_get_column(row, "end_time")),
            "status_code": _safe_str(_get_column(row, "status_code", "status")),
            "input_value": _safe_str(_get_column(row, "attributes.input.value")),
            "output_value": _safe_str(_get_column(row, "attributes.output.value")),
            "input_messages": _safe_str(_get_column(row, "attributes.llm.input_messages")),
            "output_messages": _safe_str(_get_column(row, "attributes.llm.output_messages")),
            "llm_model_name": _safe_str(_get_column(row, "attributes.llm.model_name")),
            "llm_token_count_prompt": _safe_int(
                _get_column(row, "attributes.llm.token_count.prompt")
            ),
            "llm_token_count_completion": _safe_int(
                _get_column(row, "attributes.llm.token_count.completion")
            ),
            "llm_token_count_total": _safe_int(
                _get_column(row, "attributes.llm.token_count.total")
            ),
            "user_id": extract_user_id_from_span(identity_span),
            "account_id": extract_account_id_from_span(identity_span),
            "backend": "arize",
            "raw_attributes": raw_dict,
        }
        rows.append(unified)

    return pd.DataFrame(rows, columns=UNIFIED_COLUMNS)


def _safe_float(value: Any) -> float | None:
    """Convert value to float, returning None for missing values."""
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def normalize_phoenix_annotations(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert Phoenix annotations DataFrame to unified schema.

    Phoenix Annotation Column Mappings:
        id → annotation_id
        span_id → span_id
        name → name
        annotator_kind → annotator_kind
        result.label → label
        result.score → score
        result.explanation → explanation
        metadata → metadata (as JSON string)
        created_at → created_at (converted to ISO-8601)
        updated_at → updated_at (converted to ISO-8601)
        source → source
        user_id → user_id

    Args:
        df: DataFrame from PhoenixClient.get_span_annotations_dataframe()

    Returns:
        DataFrame with unified annotation schema columns.
    """
    if df.empty:
        return pd.DataFrame(columns=ANNOTATION_COLUMNS)

    rows = []
    for _, row in df.iterrows():
        # Handle nested result field (may be dict or separate columns)
        label = None
        score = None
        explanation = None

        result = _get_column(row, "result")
        if isinstance(result, dict):
            label = result.get("label")
            score = result.get("score")
            explanation = result.get("explanation")
        else:
            label = _get_column(row, "result.label", "label")
            score = _get_column(row, "result.score", "score")
            explanation = _get_column(row, "result.explanation", "explanation")

        # Handle metadata - convert dict to JSON string
        metadata = _get_column(row, "metadata")
        if isinstance(metadata, dict):
            import json
            metadata = json.dumps(metadata)
        else:
            metadata = _safe_str(metadata)

        unified = {
            "annotation_id": _safe_str(_get_column(row, "id", "annotation_id")),
            "span_id": _safe_str(_get_column(row, "span_id")),
            "name": _safe_str(_get_column(row, "name")),
            "annotator_kind": _safe_str(_get_column(row, "annotator_kind")),
            "label": _safe_str(label),
            "score": _safe_float(score),
            "explanation": _safe_str(explanation),
            "metadata": metadata,
            "created_at": _to_iso8601(_get_column(row, "created_at")),
            "updated_at": _to_iso8601(_get_column(row, "updated_at")),
            "source": _safe_str(_get_column(row, "source")),
            "user_id": _safe_str(_get_column(row, "user_id")),
            "backend": "phoenix",
        }
        rows.append(unified)

    return pd.DataFrame(rows, columns=ANNOTATION_COLUMNS)
