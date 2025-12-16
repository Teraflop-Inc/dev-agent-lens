"""
Session ID Extractor Module

Provides functions for extracting session IDs from trace spans.
Handles both Phoenix and Arize metadata formats.

Session ID Patterns:
    - Phoenix: metadata.user_id with `_session_<id>` or `session_<id>` suffix
    - Arize: user_api_key_end_user_id or requester_metadata.user_id with `session_<id>`
"""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

# Pattern to match session ID in various formats
SESSION_PATTERN = re.compile(r"session_([a-zA-Z0-9_-]+)")


def extract_session_id(metadata: Any) -> str | None:
    """
    Extract session ID from span metadata.

    Handles multiple metadata formats from Phoenix and Arize:
    - String format: "user_session_abc123" → "abc123"
    - Dict with user_id: {"user_id": "session_abc123"} → "abc123"
    - Dict with user_api_key_end_user_id: {"user_api_key_end_user_id": "session_abc123"}
    - Dict with requester_metadata: {"requester_metadata": {"user_id": "session_abc123"}}

    Args:
        metadata: The metadata field from a span. Can be a string, dict, or JSON string.

    Returns:
        The extracted session ID string, or None if no session ID found.
    """
    if metadata is None or (isinstance(metadata, float) and pd.isna(metadata)):
        return None

    # Handle string metadata (may be raw string or JSON)
    if isinstance(metadata, str):
        # Try to parse as JSON first
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict):
                return _extract_from_dict(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try to extract from string pattern
        return _extract_from_string(metadata)

    # Handle dict metadata
    if isinstance(metadata, dict):
        return _extract_from_dict(metadata)

    return None


def _extract_from_string(value: str) -> str | None:
    """Extract session ID from a string value."""
    if not value:
        return None

    match = SESSION_PATTERN.search(value)
    if match:
        return match.group(1)

    return None


def _extract_from_dict(metadata: dict) -> str | None:
    """Extract session ID from a dict metadata structure."""
    # Try Phoenix format: metadata.user_id with session_ pattern
    user_id = metadata.get("user_id")
    if user_id:
        session_id = _extract_from_string(str(user_id))
        if session_id:
            return session_id

    # Try Arize format: user_api_key_end_user_id
    user_id = metadata.get("user_api_key_end_user_id")
    if user_id:
        session_id = _extract_from_string(str(user_id))
        if session_id:
            return session_id

    # Try Arize format: requester_metadata.user_id
    req_meta = metadata.get("requester_metadata")
    if isinstance(req_meta, dict):
        user_id = req_meta.get("user_id")
        if user_id:
            session_id = _extract_from_string(str(user_id))
            if session_id:
                return session_id

    # Try string representation if nothing else worked
    return None


def extract_session_id_from_span(span: dict | pd.Series) -> str | None:
    """
    Extract session ID from a unified span.

    This is a convenience function that handles both dict and pandas Series
    representations of spans, looking in common metadata fields.

    Args:
        span: A unified span as a dict or pandas Series.

    Returns:
        The extracted session ID string, or None if no session ID found.
    """
    if isinstance(span, pd.Series):
        span = span.to_dict()

    # Try various metadata field names
    metadata_fields = [
        "metadata",
        "attributes.metadata",
        "raw_attributes",
    ]

    for field in metadata_fields:
        if field in span:
            metadata = span[field]
            session_id = extract_session_id(metadata)
            if session_id:
                return session_id

    # Also check input_value for session patterns (sometimes embedded in prompts)
    input_value = span.get("input_value")
    if input_value:
        session_id = _extract_from_string(str(input_value))
        if session_id:
            return session_id

    return None
