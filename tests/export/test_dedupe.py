"""Tests for deduplication and cleaning functions."""

import json
import math
import tempfile
from pathlib import Path

import pytest

from dev_agent_lens.export.dedupe import (
    DUPLICATED_FIELDS,
    KEEP_FIELDS,
    calculate_savings,
    clean_session,
    clean_sessions_file,
    deduplicate_raw_attributes,
    deduplicate_session,
    deduplicate_span,
    is_empty,
    strip_empty_values,
)


class TestIsEmpty:
    """Tests for is_empty function."""

    def test_none_is_empty(self):
        assert is_empty(None) is True

    def test_empty_string_is_empty(self):
        assert is_empty("") is True

    def test_empty_list_is_empty(self):
        assert is_empty([]) is True

    def test_empty_dict_is_empty(self):
        assert is_empty({}) is True

    def test_nan_float_is_empty(self):
        assert is_empty(float("nan")) is True

    def test_nan_string_is_empty(self):
        assert is_empty("nan") is True

    def test_non_empty_string_is_not_empty(self):
        assert is_empty("hello") is False

    def test_non_empty_list_is_not_empty(self):
        assert is_empty([1, 2, 3]) is False

    def test_non_empty_dict_is_not_empty(self):
        assert is_empty({"key": "value"}) is False

    def test_zero_is_not_empty(self):
        assert is_empty(0) is False

    def test_false_is_not_empty(self):
        assert is_empty(False) is False


class TestStripEmptyValues:
    """Tests for strip_empty_values function."""

    def test_strips_none_values(self):
        data = {"a": 1, "b": None, "c": 3}
        result = strip_empty_values(data)
        assert result == {"a": 1, "c": 3}

    def test_strips_empty_strings(self):
        data = {"a": "hello", "b": "", "c": "world"}
        result = strip_empty_values(data)
        assert result == {"a": "hello", "c": "world"}

    def test_strips_empty_lists(self):
        data = {"a": [1, 2], "b": [], "c": [3]}
        result = strip_empty_values(data)
        assert result == {"a": [1, 2], "c": [3]}

    def test_strips_empty_dicts(self):
        data = {"a": {"x": 1}, "b": {}, "c": {"y": 2}}
        result = strip_empty_values(data)
        assert result == {"a": {"x": 1}, "c": {"y": 2}}

    def test_strips_nan_values(self):
        data = {"a": 1, "b": float("nan"), "c": 3}
        result = strip_empty_values(data)
        assert result == {"a": 1, "c": 3}

    def test_recursive_stripping(self):
        data = {
            "a": 1,
            "nested": {
                "b": None,
                "c": "value",
                "deep": {
                    "d": "",
                    "e": 5,
                }
            }
        }
        result = strip_empty_values(data)
        assert result == {
            "a": 1,
            "nested": {
                "c": "value",
                "deep": {
                    "e": 5,
                }
            }
        }

    def test_removes_nested_dict_if_all_empty(self):
        data = {
            "a": 1,
            "nested": {
                "b": None,
                "c": "",
            }
        }
        result = strip_empty_values(data)
        assert result == {"a": 1}

    def test_preserves_non_empty_values(self):
        data = {"a": 0, "b": False, "c": ""}
        result = strip_empty_values(data)
        # 0 and False are not empty, only "" is
        assert result == {"a": 0, "b": False}


class TestDeduplicateRawAttributes:
    """Tests for deduplicate_raw_attributes function."""

    def test_removes_duplicated_fields(self):
        raw_attrs = {
            "context.span_id": "abc123",
            "context.trace_id": "trace456",
            "name": "test_span",
            "unique_field": "keep_me",
        }
        result = deduplicate_raw_attributes(raw_attrs)
        assert "context.span_id" not in result
        assert "context.trace_id" not in result
        assert "name" not in result
        assert result["unique_field"] == "keep_me"

    def test_removes_llm_duplicates(self):
        raw_attrs = {
            "attributes.llm.model_name": "claude-3",
            "attributes.llm.input_messages": [{"role": "user"}],
            "attributes.llm.output_messages": [{"role": "assistant"}],
            "attributes.llm.token_count.prompt": 100,
            "attributes.llm.token_count.completion": 50,
            "attributes.llm.token_count.total": 150,
            "attributes.llm.invocation_parameters": {"max_tokens": 1000},
        }
        result = deduplicate_raw_attributes(raw_attrs)
        # LLM duplicates should be removed
        assert "attributes.llm.model_name" not in result
        assert "attributes.llm.input_messages" not in result
        assert "attributes.llm.output_messages" not in result
        assert "attributes.llm.token_count.prompt" not in result
        # But invocation_parameters should be kept
        assert result["attributes.llm.invocation_parameters"] == {"max_tokens": 1000}

    def test_keeps_unique_fields(self):
        raw_attrs = {
            "context.span_id": "abc123",  # duplicated
            "events": [{"name": "error"}],  # unique
            "attributes.metadata": {"key": "value"},  # unique
            "attributes.llm.provider": "anthropic",  # unique
        }
        result = deduplicate_raw_attributes(raw_attrs)
        assert "context.span_id" not in result
        assert result["events"] == [{"name": "error"}]
        assert result["attributes.metadata"] == {"key": "value"}
        assert result["attributes.llm.provider"] == "anthropic"

    def test_empty_input(self):
        result = deduplicate_raw_attributes({})
        assert result == {}


class TestDeduplicateSpan:
    """Tests for deduplicate_span function."""

    def test_deduplicates_raw_attributes(self):
        span = {
            "span_id": "span123",
            "trace_id": "trace456",
            "name": "test_span",
            "raw_attributes": {
                "context.span_id": "span123",
                "context.trace_id": "trace456",
                "name": "test_span",
                "unique_field": "value",
            }
        }
        result = deduplicate_span(span)
        assert result["span_id"] == "span123"
        assert "context.span_id" not in result["raw_attributes"]
        assert result["raw_attributes"]["unique_field"] == "value"

    def test_strips_empty_values(self):
        span = {
            "span_id": "span123",
            "raw_attributes": {
                "field_a": "value",
                "field_b": None,
                "field_c": "",
            }
        }
        result = deduplicate_span(span)
        assert result["raw_attributes"] == {"field_a": "value"}

    def test_handles_missing_raw_attributes(self):
        span = {"span_id": "span123", "name": "test"}
        result = deduplicate_span(span)
        assert result == span

    def test_handles_non_dict_raw_attributes(self):
        span = {"span_id": "span123", "raw_attributes": "invalid"}
        result = deduplicate_span(span)
        assert result["raw_attributes"] == "invalid"


class TestDeduplicateSession:
    """Tests for deduplicate_session function."""

    def test_deduplicates_all_spans(self):
        session = {
            "session_id": "session123",
            "spans": [
                {
                    "span_id": "span1",
                    "raw_attributes": {
                        "context.span_id": "span1",
                        "unique": "value1",
                    }
                },
                {
                    "span_id": "span2",
                    "raw_attributes": {
                        "context.span_id": "span2",
                        "unique": "value2",
                    }
                },
            ]
        }
        result = deduplicate_session(session)
        assert result["session_id"] == "session123"
        assert len(result["spans"]) == 2
        for span in result["spans"]:
            assert "context.span_id" not in span["raw_attributes"]
            assert "unique" in span["raw_attributes"]

    def test_handles_empty_spans(self):
        session = {"session_id": "session123", "spans": []}
        result = deduplicate_session(session)
        assert result == session

    def test_handles_missing_spans(self):
        session = {"session_id": "session123"}
        result = deduplicate_session(session)
        assert result == session


class TestCleanSession:
    """Tests for clean_session function."""

    def test_dedupe_only(self):
        session = {
            "session_id": "s1",
            "spans": [{
                "span_id": "sp1",
                "raw_attributes": {
                    "context.span_id": "sp1",
                    "field": "value",
                    "empty": None,
                }
            }]
        }
        result = clean_session(session, dedupe=True, strip_nulls=False)
        # context.span_id removed, but None kept
        assert "context.span_id" not in result["spans"][0]["raw_attributes"]
        assert result["spans"][0]["raw_attributes"]["empty"] is None

    def test_strip_nulls_only(self):
        session = {
            "session_id": "s1",
            "spans": [{
                "span_id": "sp1",
                "raw_attributes": {
                    "context.span_id": "sp1",
                    "field": "value",
                    "empty": None,
                }
            }]
        }
        result = clean_session(session, dedupe=False, strip_nulls=True)
        # context.span_id kept, but None removed
        assert result["spans"][0]["raw_attributes"]["context.span_id"] == "sp1"
        assert "empty" not in result["spans"][0]["raw_attributes"]

    def test_both_enabled(self):
        session = {
            "session_id": "s1",
            "spans": [{
                "span_id": "sp1",
                "raw_attributes": {
                    "context.span_id": "sp1",
                    "field": "value",
                    "empty": None,
                }
            }]
        }
        result = clean_session(session, dedupe=True, strip_nulls=True)
        assert "context.span_id" not in result["spans"][0]["raw_attributes"]
        assert "empty" not in result["spans"][0]["raw_attributes"]
        assert result["spans"][0]["raw_attributes"]["field"] == "value"

    def test_neither_enabled(self):
        session = {
            "session_id": "s1",
            "spans": [{
                "span_id": "sp1",
                "raw_attributes": {
                    "context.span_id": "sp1",
                    "empty": None,
                }
            }]
        }
        result = clean_session(session, dedupe=False, strip_nulls=False)
        assert result["spans"][0]["raw_attributes"]["context.span_id"] == "sp1"
        assert result["spans"][0]["raw_attributes"]["empty"] is None


class TestCalculateSavings:
    """Tests for calculate_savings function."""

    def test_calculates_correct_savings(self):
        original = {"a": 1, "b": 2, "c": 3}
        cleaned = {"a": 1}
        result = calculate_savings(original, cleaned)

        assert result["original_bytes"] == len(json.dumps(original))
        assert result["cleaned_bytes"] == len(json.dumps(cleaned))
        assert result["savings_bytes"] > 0
        assert 0 < result["savings_percent"] < 100

    def test_no_savings(self):
        original = {"a": 1}
        cleaned = {"a": 1}
        result = calculate_savings(original, cleaned)

        assert result["savings_bytes"] == 0
        assert result["savings_percent"] == 0.0

    def test_empty_original(self):
        result = calculate_savings({}, {})
        assert result["savings_percent"] == 0.0


class TestCleanSessionsFile:
    """Tests for clean_sessions_file function."""

    def test_cleans_file(self):
        sessions = [
            {
                "session_id": "s1",
                "spans": [{
                    "span_id": "sp1",
                    "raw_attributes": {
                        "context.span_id": "sp1",
                        "field": "value",
                        "empty": None,
                    }
                }]
            },
            {
                "session_id": "s2",
                "spans": [{
                    "span_id": "sp2",
                    "raw_attributes": {
                        "context.span_id": "sp2",
                        "other": "data",
                    }
                }]
            },
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for session in sessions:
                f.write(json.dumps(session) + "\n")
            input_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            output_path = f.name

        try:
            stats = clean_sessions_file(input_path, output_path)

            assert stats["sessions_processed"] == 2
            assert stats["spans_processed"] == 2
            assert stats["savings_bytes"] > 0
            assert stats["savings_percent"] > 0

            # Verify output
            with open(output_path, "r") as f:
                lines = f.readlines()
            assert len(lines) == 2

            for line in lines:
                session = json.loads(line)
                for span in session["spans"]:
                    assert "context.span_id" not in span["raw_attributes"]
                    assert "empty" not in span.get("raw_attributes", {})

        finally:
            Path(input_path).unlink()
            Path(output_path).unlink()

    def test_progress_callback(self):
        sessions = [{"session_id": f"s{i}", "spans": []} for i in range(5)]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for session in sessions:
                f.write(json.dumps(session) + "\n")
            input_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            output_path = f.name

        callback_calls = []

        def callback(n, saved):
            callback_calls.append((n, saved))

        try:
            clean_sessions_file(input_path, output_path, progress_callback=callback)
            assert len(callback_calls) == 5  # Called for each session
        finally:
            Path(input_path).unlink()
            Path(output_path).unlink()


class TestDuplicatedFieldsConstant:
    """Tests for DUPLICATED_FIELDS constant."""

    def test_contains_expected_fields(self):
        expected = [
            "context.span_id",
            "context.trace_id",
            "name",
            "span_kind",
            "attributes.llm.model_name",
            "attributes.llm.input_messages",
            "attributes.llm.output_messages",
        ]
        for field in expected:
            assert field in DUPLICATED_FIELDS, f"{field} should be in DUPLICATED_FIELDS"


class TestKeepFieldsConstant:
    """Tests for KEEP_FIELDS constant."""

    def test_contains_expected_fields(self):
        expected = [
            "attributes.llm.invocation_parameters",
            "events",
            "attributes.metadata",
            "attributes.llm.provider",
        ]
        for field in expected:
            assert field in KEEP_FIELDS, f"{field} should be in KEEP_FIELDS"
