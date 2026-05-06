"""
Tests for Session ID Extractor.

These tests verify session ID extraction from various metadata formats:
- Phoenix patterns
- Arize patterns
- Edge cases and malformed data
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from dev_agent_lens.core.session import (
    extract_session_id,
    extract_session_id_from_span,
)


class TestExtractSessionIdPhoenix:
    """Tests for Phoenix session ID patterns."""

    def test_phoenix_user_id_pattern(self):
        """Given Phoenix metadata with user_id containing session_, extracts ID."""
        metadata = {"user_id": "user_session_abc123"}
        result = extract_session_id(metadata)
        assert result == "abc123"

    def test_phoenix_underscore_session_pattern(self):
        """Given Phoenix metadata with _session_ pattern, extracts ID."""
        metadata = {"user_id": "alex_session_def456"}
        result = extract_session_id(metadata)
        assert result == "def456"

    def test_phoenix_simple_session_pattern(self):
        """Given Phoenix metadata with simple session_ prefix, extracts ID."""
        metadata = {"user_id": "session_ghi789"}
        result = extract_session_id(metadata)
        assert result == "ghi789"

    def test_phoenix_string_metadata(self):
        """Given Phoenix string metadata with session pattern, extracts ID."""
        metadata = "user_session_xyz123"
        result = extract_session_id(metadata)
        assert result == "xyz123"

    def test_phoenix_json_string_metadata(self):
        """Given Phoenix JSON string metadata, parses and extracts ID."""
        metadata = json.dumps({"user_id": "session_json123"})
        result = extract_session_id(metadata)
        assert result == "json123"


class TestExtractSessionIdArize:
    """Tests for Arize session ID patterns."""

    def test_arize_user_api_key_pattern(self):
        """Given Arize metadata with user_api_key_end_user_id, extracts ID."""
        metadata = {"user_api_key_end_user_id": "session_arize001"}
        result = extract_session_id(metadata)
        assert result == "arize001"

    def test_arize_requester_metadata_pattern(self):
        """Given Arize requester_metadata.user_id, extracts ID."""
        metadata = {
            "requester_metadata": {"user_id": "session_arize002"}
        }
        result = extract_session_id(metadata)
        assert result == "arize002"

    def test_arize_nested_json_metadata(self):
        """Given Arize nested JSON metadata, extracts ID."""
        metadata = json.dumps({
            "requester_metadata": {"user_id": "session_nested123"}
        })
        result = extract_session_id(metadata)
        assert result == "nested123"


class TestExtractSessionIdNoSession:
    """Tests for spans without session ID."""

    def test_none_metadata(self):
        """Given None metadata, returns None."""
        result = extract_session_id(None)
        assert result is None

    def test_nan_metadata(self):
        """Given NaN metadata, returns None."""
        result = extract_session_id(float("nan"))
        assert result is None

    def test_empty_string_metadata(self):
        """Given empty string metadata, returns None."""
        result = extract_session_id("")
        assert result is None

    def test_empty_dict_metadata(self):
        """Given empty dict metadata, returns None."""
        result = extract_session_id({})
        assert result is None

    def test_no_session_pattern(self):
        """Given metadata without session pattern, returns None."""
        metadata = {"user_id": "just_a_user_id"}
        result = extract_session_id(metadata)
        assert result is None

    def test_invalid_json_string(self):
        """Given invalid JSON string, tries string extraction."""
        metadata = "not valid json {{"
        result = extract_session_id(metadata)
        assert result is None


class TestExtractSessionIdMalformed:
    """Tests for malformed session data."""

    def test_partial_session_prefix(self):
        """Given partial 'session' without underscore, returns None."""
        metadata = {"user_id": "sessionabc123"}  # No underscore
        result = extract_session_id(metadata)
        assert result is None

    def test_session_with_special_chars(self):
        """Given session ID with allowed special chars, extracts correctly."""
        metadata = {"user_id": "session_abc-123_xyz"}
        result = extract_session_id(metadata)
        assert result == "abc-123_xyz"

    def test_multiple_session_patterns(self):
        """Given multiple session patterns, extracts first one."""
        metadata = {"user_id": "session_first_session_second"}
        result = extract_session_id(metadata)
        # Should extract 'first_session_second' as everything after first 'session_'
        assert result == "first_session_second"

    def test_nested_dict_not_requester_metadata(self):
        """Given nested dict that's not requester_metadata, returns None."""
        metadata = {"other_metadata": {"user_id": "session_ignored"}}
        result = extract_session_id(metadata)
        assert result is None


class TestExtractSessionIdConsistency:
    """Tests ensuring consistent extraction across backends."""

    def test_same_session_phoenix_format(self):
        """Given same session in Phoenix format, extracts consistently."""
        phoenix_meta = {"user_id": "alex_session_consistent123"}
        result = extract_session_id(phoenix_meta)
        assert result == "consistent123"

    def test_same_session_arize_format(self):
        """Given same session in Arize format, extracts same ID."""
        arize_meta = {"user_api_key_end_user_id": "session_consistent123"}
        result = extract_session_id(arize_meta)
        assert result == "consistent123"

    def test_both_formats_same_result(self):
        """Given same session in both formats, produces same result."""
        session_id = "shared_session_abc"

        phoenix_meta = {"user_id": f"user_session_{session_id}"}
        arize_meta = {"user_api_key_end_user_id": f"session_{session_id}"}

        phoenix_result = extract_session_id(phoenix_meta)
        arize_result = extract_session_id(arize_meta)

        assert phoenix_result == arize_result


class TestExtractSessionIdFromSpan:
    """Tests for extract_session_id_from_span helper."""

    def test_span_with_metadata_dict(self):
        """Given span dict with metadata field, extracts session ID."""
        span = {
            "span_id": "span1",
            "metadata": {"user_id": "session_fromspan123"},
        }
        result = extract_session_id_from_span(span)
        assert result == "fromspan123"

    def test_span_with_attributes_metadata(self):
        """Given span with attributes.metadata field, extracts session ID."""
        span = {
            "span_id": "span1",
            "attributes.metadata": {"user_id": "session_attrs456"},
        }
        result = extract_session_id_from_span(span)
        assert result == "attrs456"

    def test_span_as_pandas_series(self):
        """Given span as pandas Series, extracts session ID."""
        span = pd.Series({
            "span_id": "span1",
            "metadata": {"user_id": "session_pandas789"},
        })
        result = extract_session_id_from_span(span)
        assert result == "pandas789"

    def test_span_without_session_or_trace_id(self):
        """Given span without session info or trace_id, returns None."""
        span = {"span_id": "span1", "name": "test"}
        result = extract_session_id_from_span(span)
        assert result is None

    def test_span_with_trace_id_fallback(self):
        """Given span without session pattern but with trace_id, uses trace_id."""
        span = {
            "span_id": "span1",
            "trace_id": "trace-abc-123-def",
            "name": "test",
        }
        result = extract_session_id_from_span(span)
        assert result == "trace-abc-123-def"

    def test_span_session_pattern_takes_priority_over_trace_id(self):
        """Given span with both session pattern and trace_id, session pattern wins."""
        span = {
            "span_id": "span1",
            "trace_id": "trace-abc-123",
            "metadata": {"user_id": "session_explicit456"},
        }
        result = extract_session_id_from_span(span)
        assert result == "explicit456"

    def test_span_with_nan_trace_id(self):
        """Given span with NaN trace_id, returns None."""
        span = {"span_id": "span1", "trace_id": float("nan")}
        result = extract_session_id_from_span(span)
        assert result is None

    def test_span_with_session_in_input_value(self):
        """Given span with session in input_value, extracts session ID."""
        span = {
            "span_id": "span1",
            "input_value": "Processing for session_input123",
        }
        result = extract_session_id_from_span(span)
        assert result == "input123"
