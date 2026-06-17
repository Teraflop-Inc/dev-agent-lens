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

from dev_agent_lens.core.session import (
    extract_account_id,
    extract_account_id_from_span,
    extract_session_id,
    extract_session_id_from_span,
    extract_user_id,
    extract_user_id_from_span,
)

# Canonical LiteLLM end-user string: user_<hash>_account_<uuid>_session_<uuid>
LITELLM_USER_STRING = (
    "user_abc123def_account_11111111-1111-1111-1111-111111111111"
    "_session_22222222-2222-2222-2222-222222222222"
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


class TestExtractUserId:
    """Tests for user hash extraction from the LiteLLM end-user string."""

    def test_extract_user_id_from_full_string(self):
        """Given the canonical user string, extracts the user hash."""
        assert extract_user_id(LITELLM_USER_STRING) == "abc123def"

    def test_extract_user_id_from_dict_end_user_id(self):
        """Given dict with user_api_key_end_user_id, extracts user hash."""
        metadata = {"user_api_key_end_user_id": LITELLM_USER_STRING}
        assert extract_user_id(metadata) == "abc123def"

    def test_extract_user_id_from_requester_metadata(self):
        """Given requester_metadata.user_id, extracts user hash."""
        metadata = {"requester_metadata": {"user_id": LITELLM_USER_STRING}}
        assert extract_user_id(metadata) == "abc123def"

    def test_extract_user_id_none_when_no_user_prefix(self):
        """Given a session-only string, returns None (no user_ prefix)."""
        assert extract_user_id("session_22222222-2222-2222-2222-222222222222") is None

    def test_extract_user_id_none_metadata(self):
        """Given None metadata, returns None."""
        assert extract_user_id(None) is None


class TestExtractAccountId:
    """Tests for account UUID extraction from the LiteLLM end-user string."""

    def test_extract_account_id_from_full_string(self):
        """Given the canonical user string, extracts the account UUID."""
        assert extract_account_id(LITELLM_USER_STRING) == (
            "11111111-1111-1111-1111-111111111111"
        )

    def test_extract_account_id_from_dict_end_user_id(self):
        """Given dict with user_api_key_end_user_id, extracts account UUID."""
        metadata = {"user_api_key_end_user_id": LITELLM_USER_STRING}
        assert extract_account_id(metadata) == (
            "11111111-1111-1111-1111-111111111111"
        )

    def test_extract_account_id_none_when_absent(self):
        """Given a string without an account segment, returns None."""
        assert extract_account_id("user_abc_session_xyz") is None


class TestExtractUserAttributionFromSpan:
    """Tests for span-level user/account extraction across metadata layouts."""

    def test_user_id_from_flat_metadata(self):
        """Given span.metadata with end_user_id, extracts user hash."""
        span = {
            "span_id": "s1",
            "metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING},
        }
        assert extract_user_id_from_span(span) == "abc123def"

    def test_user_id_from_nested_phoenix_attributes(self):
        """Given real Phoenix nested attributes.metadata, extracts user hash."""
        span = {
            "span_id": "s1",
            "raw_attributes": {
                "attributes": {
                    "metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING}
                }
            },
        }
        assert extract_user_id_from_span(span) == "abc123def"

    def test_user_id_from_dotted_lambda2_attributes(self):
        """Given lambda2 dotted attributes.metadata (JSON string), extracts hash."""
        span = {
            "span_id": "s1",
            "raw_attributes": {
                "attributes.metadata": json.dumps(
                    {"requester_metadata": {"user_id": LITELLM_USER_STRING}}
                )
            },
        }
        assert extract_user_id_from_span(span) == "abc123def"

    def test_account_id_from_nested_phoenix_attributes(self):
        """Given nested attributes.metadata, extracts account UUID."""
        span = {
            "span_id": "s1",
            "raw_attributes": {
                "attributes": {
                    "metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING}
                }
            },
        }
        assert extract_account_id_from_span(span) == (
            "11111111-1111-1111-1111-111111111111"
        )

    def test_user_id_does_not_fall_back_to_trace_id(self):
        """Given no user metadata, user extraction returns None (unlike session)."""
        span = {"span_id": "s1", "trace_id": "trace-abc-123"}
        assert extract_user_id_from_span(span) is None
        assert extract_account_id_from_span(span) is None

    def test_pandas_series_input(self):
        """Given a pandas Series span, extracts user hash."""
        span = pd.Series(
            {
                "span_id": "s1",
                "metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING},
            }
        )
        assert extract_user_id_from_span(span) == "abc123def"


# Current LiteLLM end-user identity: a JSON OBJECT, not the underscore string.
LITELLM_USER_JSON = (
    '{"device_id":"70d31d6ecd411c21","account_uuid":'
    '"63ddef2f-7fea-429f-91ba-026ea296f6a4",'
    '"session_id":"e66a526e-3684-49ff-9284-749b56d9664a"}'
)


class TestJsonObjectIdentity:
    """The live LiteLLM proxy emits a JSON object {device_id, account_uuid,
    session_id}, NOT the underscore string. Regression for ENG2-1312/1319."""

    def test_session_id_from_json_object(self):
        """session_id is read from the key, not regex-matched off the string."""
        assert extract_session_id(LITELLM_USER_JSON) == "e66a526e-3684-49ff-9284-749b56d9664a"

    def test_session_id_is_not_literal_id(self):
        """Regression: SESSION_PATTERN over a JSON string used to yield 'id'."""
        assert extract_session_id(LITELLM_USER_JSON) != "id"

    def test_user_id_is_device_id(self):
        assert extract_user_id(LITELLM_USER_JSON) == "70d31d6ecd411c21"

    def test_account_id_from_account_uuid_key(self):
        assert extract_account_id(LITELLM_USER_JSON) == "63ddef2f-7fea-429f-91ba-026ea296f6a4"

    def test_real_span_shape_wrapper(self):
        """The real span carries the JSON string under user_api_key_end_user_id."""
        span = {"metadata": {"user_api_key_end_user_id": LITELLM_USER_JSON}}
        assert extract_session_id_from_span(span) == "e66a526e-3684-49ff-9284-749b56d9664a"
        assert extract_user_id_from_span(span) == "70d31d6ecd411c21"
        assert extract_account_id_from_span(span) == "63ddef2f-7fea-429f-91ba-026ea296f6a4"

    def test_account_uuid_only(self):
        """JSON object with only account_uuid yields account, no user/session."""
        obj = '{"account_uuid":"63ddef2f-7fea-429f-91ba-026ea296f6a4"}'
        assert extract_account_id(obj) == "63ddef2f-7fea-429f-91ba-026ea296f6a4"
        assert extract_user_id(obj) is None
