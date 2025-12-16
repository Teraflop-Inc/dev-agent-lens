"""
Tests for the regex search engine.

Test Cases from Story 2.1:
1. Basic Match: Given pattern "ENG2-123", finds spans containing it
2. Regex: Given pattern "ENG2-\\d+", finds all ticket references
3. No Match: Given pattern with no matches, returns empty list
4. Case Insensitive: With flag, "error" matches "Error"
5. Large File: Handles 100k+ line JSONL efficiently
6. Invalid Regex: Given invalid pattern, raises descriptive error
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from dev_agent_lens.query.regex import (
    DEFAULT_SEARCH_FIELDS,
    VALID_SEARCH_FIELDS,
    InvalidFieldError,
    RegexSearchError,
    SearchMatch,
    search,
    search_dataframe,
    search_file,
    validate_fields,
)


class TestSearchMatch:
    """Tests for SearchMatch dataclass."""

    def test_search_match_creation(self):
        """Given valid arguments, SearchMatch is created correctly."""
        span = {"span_id": "abc123", "name": "test"}
        match = SearchMatch(
            span=span,
            field="name",
            match_start=0,
            match_end=4,
            matched_text="test",
            line_number=1,
        )

        assert match.span == span
        assert match.field == "name"
        assert match.match_start == 0
        assert match.match_end == 4
        assert match.matched_text == "test"
        assert match.line_number == 1

    def test_search_match_to_dict(self):
        """Given a SearchMatch, to_dict returns serializable dict."""
        span = {"span_id": "abc123", "name": "test"}
        match = SearchMatch(
            span=span,
            field="name",
            match_start=0,
            match_end=4,
            matched_text="test",
            line_number=1,
        )

        result = match.to_dict()

        assert result["span"] == span
        assert result["field"] == "name"
        assert result["matched_text"] == "test"
        assert result["line_number"] == 1


class TestBasicMatch:
    """Test Case 1: Basic string matching."""

    def test_given_pattern_eng2_123_finds_spans_containing_it(self):
        """Given pattern 'ENG2-123', finds spans containing it."""
        spans = [
            {"span_id": "1", "name": "Working on ENG2-123", "input_value": "hello"},
            {"span_id": "2", "name": "Other task", "input_value": "world"},
            {"span_id": "3", "name": "test", "input_value": "ENG2-123 is done"},
        ]

        matches = search("ENG2-123", spans)

        assert len(matches) == 2
        assert matches[0].span["span_id"] == "1"
        assert matches[0].field == "name"
        assert matches[0].matched_text == "ENG2-123"
        assert matches[1].span["span_id"] == "3"
        assert matches[1].field == "input_value"

    def test_searches_multiple_fields_by_default(self):
        """Search includes all default string fields."""
        spans = [
            {
                "span_id": "1",
                "name": "match1",
                "input_value": "match2",
                "output_value": "match3",
                "input_messages": "match4",
                "output_messages": "match5",
                "llm_model_name": "match6",
                "status_code": "match7",
            }
        ]

        matches = search("match", spans)

        # Should find matches in all 7 searchable fields
        assert len(matches) == 7
        matched_fields = {m.field for m in matches}
        assert "name" in matched_fields
        assert "input_value" in matched_fields
        assert "output_value" in matched_fields

    def test_searches_specific_fields_when_provided(self):
        """Given specific fields, only searches those fields."""
        spans = [
            {"span_id": "1", "name": "match", "input_value": "match", "output_value": "match"}
        ]

        matches = search("match", spans, fields=["name"])

        assert len(matches) == 1
        assert matches[0].field == "name"


class TestRegexPatterns:
    """Test Case 2: Regex pattern support."""

    def test_given_pattern_eng2_digit_finds_all_ticket_references(self):
        """Given pattern 'ENG2-\\d+', finds all ticket references."""
        spans = [
            {"span_id": "1", "name": "Working on ENG2-123 and ENG2-456"},
            {"span_id": "2", "name": "ENG2-789 is complete"},
            {"span_id": "3", "name": "No tickets here"},
        ]

        matches = search(r"ENG2-\d+", spans)

        assert len(matches) == 3
        matched_texts = [m.matched_text for m in matches]
        assert "ENG2-123" in matched_texts
        assert "ENG2-456" in matched_texts
        assert "ENG2-789" in matched_texts

    def test_supports_complex_regex_patterns(self):
        """Supports complex regex patterns like character classes and groups."""
        spans = [
            {"span_id": "1", "input_value": "email@example.com"},
            {"span_id": "2", "input_value": "test@test.org"},
            {"span_id": "3", "input_value": "not an email"},
        ]

        # Simple email-like pattern
        matches = search(r"\w+@\w+\.\w+", spans)

        assert len(matches) == 2
        assert matches[0].matched_text == "email@example.com"
        assert matches[1].matched_text == "test@test.org"

    def test_supports_word_boundary_patterns(self):
        """Supports word boundary patterns."""
        spans = [
            {"span_id": "1", "name": "error in processing"},
            {"span_id": "2", "name": "no errors here"},
            {"span_id": "3", "name": "errorhandling"},
        ]

        # Match "error" as a whole word
        matches = search(r"\berror\b", spans)

        assert len(matches) == 1
        assert matches[0].span["span_id"] == "1"


class TestNoMatch:
    """Test Case 3: No matches behavior."""

    def test_given_pattern_with_no_matches_returns_empty_list(self):
        """Given pattern with no matches, returns empty list."""
        spans = [
            {"span_id": "1", "name": "hello world"},
            {"span_id": "2", "name": "foo bar"},
        ]

        matches = search("xyz123", spans)

        assert matches == []

    def test_empty_spans_list_returns_empty_matches(self):
        """Given empty spans list, returns empty matches."""
        matches = search("test", [])

        assert matches == []

    def test_spans_with_none_values_handled_gracefully(self):
        """Spans with None field values are handled gracefully."""
        spans = [
            {"span_id": "1", "name": None, "input_value": None},
            {"span_id": "2", "name": "test", "input_value": None},
        ]

        matches = search("test", spans)

        assert len(matches) == 1
        assert matches[0].span["span_id"] == "2"


class TestCaseInsensitive:
    """Test Case 4: Case-insensitive matching."""

    def test_with_flag_error_matches_Error(self):
        """With case_insensitive flag, 'error' matches 'Error'."""
        spans = [
            {"span_id": "1", "name": "Error occurred"},
            {"span_id": "2", "name": "ERROR: something failed"},
            {"span_id": "3", "name": "An error happened"},
            {"span_id": "4", "name": "Success"},
        ]

        matches = search("error", spans, case_insensitive=True)

        assert len(matches) == 3
        matched_ids = {m.span["span_id"] for m in matches}
        assert matched_ids == {"1", "2", "3"}

    def test_case_sensitive_by_default(self):
        """Without flag, search is case-sensitive."""
        spans = [
            {"span_id": "1", "name": "Error"},
            {"span_id": "2", "name": "error"},
        ]

        matches = search("error", spans, case_insensitive=False)

        assert len(matches) == 1
        assert matches[0].span["span_id"] == "2"


class TestLargeFile:
    """Test Case 5: Large file handling."""

    def test_handles_100k_line_jsonl_efficiently(self, tmp_path):
        """Handles 100k+ line JSONL efficiently (under 10 seconds)."""
        # Create a large JSONL file
        file_path = tmp_path / "large.jsonl"

        with open(file_path, "w") as f:
            for i in range(100_000):
                span = {
                    "span_id": str(i),
                    "name": f"span_{i}",
                    "input_value": f"input for span {i}",
                }
                # Add a match every 10000 spans
                if i % 10000 == 0:
                    span["name"] = f"ENG2-{i} special span"
                f.write(json.dumps(span) + "\n")

        import time

        start = time.time()
        matches = search_file(r"ENG2-\d+", file_path)
        elapsed = time.time() - start

        # Should find 10 matches (at 0, 10000, 20000, ..., 90000)
        assert len(matches) == 10
        # Should complete in reasonable time
        assert elapsed < 30  # 30 seconds max for CI environments

    def test_search_file_processes_line_by_line(self, tmp_path):
        """search_file processes file line by line without loading all into memory."""
        file_path = tmp_path / "test.jsonl"

        with open(file_path, "w") as f:
            f.write(json.dumps({"span_id": "1", "name": "first"}) + "\n")
            f.write(json.dumps({"span_id": "2", "name": "match here"}) + "\n")
            f.write(json.dumps({"span_id": "3", "name": "third"}) + "\n")

        matches = search_file("match", file_path)

        assert len(matches) == 1
        assert matches[0].line_number == 2
        assert matches[0].span["span_id"] == "2"


class TestInvalidRegex:
    """Test Case 6: Invalid regex handling."""

    def test_given_invalid_pattern_raises_descriptive_error(self):
        """Given invalid pattern, raises descriptive error."""
        spans = [{"span_id": "1", "name": "test"}]

        with pytest.raises(RegexSearchError) as exc_info:
            search("[invalid", spans)

        assert "Invalid regex pattern" in str(exc_info.value)
        assert "[invalid" in str(exc_info.value)

    def test_error_includes_original_regex_error(self):
        """Error message includes the original regex error details."""
        spans = [{"span_id": "1", "name": "test"}]

        with pytest.raises(RegexSearchError) as exc_info:
            search("(?P<name>unclosed", spans)

        error_msg = str(exc_info.value)
        assert "Invalid regex pattern" in error_msg


class TestSearchFile:
    """Tests for search_file function."""

    def test_file_not_found_raises_error(self):
        """Given non-existent file, raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError) as exc_info:
            search_file("test", "/nonexistent/path.jsonl")

        assert "not found" in str(exc_info.value).lower()

    def test_skips_invalid_json_lines(self, tmp_path):
        """Invalid JSON lines are skipped without error."""
        file_path = tmp_path / "mixed.jsonl"

        with open(file_path, "w") as f:
            f.write(json.dumps({"span_id": "1", "name": "valid"}) + "\n")
            f.write("not valid json\n")
            f.write(json.dumps({"span_id": "2", "name": "also valid"}) + "\n")
            f.write("\n")  # Empty line

        matches = search_file("valid", file_path)

        assert len(matches) == 2

    def test_skips_empty_lines(self, tmp_path):
        """Empty lines are skipped."""
        file_path = tmp_path / "sparse.jsonl"

        with open(file_path, "w") as f:
            f.write(json.dumps({"span_id": "1", "name": "match"}) + "\n")
            f.write("\n")
            f.write("   \n")
            f.write(json.dumps({"span_id": "2", "name": "match"}) + "\n")

        matches = search_file("match", file_path)

        assert len(matches) == 2


class TestSearchDataframe:
    """Tests for search_dataframe function."""

    def test_returns_filtered_dataframe(self):
        """Returns DataFrame with only matching rows."""
        df = pd.DataFrame(
            [
                {"span_id": "1", "name": "ENG2-123 here"},
                {"span_id": "2", "name": "nothing special"},
                {"span_id": "3", "name": "another ENG2-456"},
            ]
        )

        result = search_dataframe(r"ENG2-\d+", df)

        assert len(result) == 2
        assert set(result["span_id"].tolist()) == {"1", "3"}

    def test_empty_dataframe_returns_empty(self):
        """Empty DataFrame input returns empty DataFrame."""
        df = pd.DataFrame(columns=["span_id", "name"])

        result = search_dataframe("test", df)

        assert len(result) == 0
        assert list(result.columns) == ["span_id", "name"]

    def test_no_matches_returns_empty_dataframe_with_same_columns(self):
        """No matches returns empty DataFrame with same columns."""
        df = pd.DataFrame(
            [
                {"span_id": "1", "name": "hello"},
                {"span_id": "2", "name": "world"},
            ]
        )

        result = search_dataframe("xyz", df)

        assert len(result) == 0
        assert list(result.columns) == list(df.columns)


class TestDataFrameInput:
    """Tests for DataFrame input handling."""

    def test_accepts_dataframe_as_input(self):
        """search() accepts pandas DataFrame as input."""
        df = pd.DataFrame(
            [
                {"span_id": "1", "name": "test match"},
                {"span_id": "2", "name": "no match here"},
            ]
        )

        matches = search("match", df)

        assert len(matches) == 2

    def test_handles_dataframe_with_none_values(self):
        """DataFrame with None values is handled correctly."""
        df = pd.DataFrame(
            [
                {"span_id": "1", "name": "test", "input_value": None},
                {"span_id": "2", "name": None, "input_value": "test"},
            ]
        )

        matches = search("test", df)

        assert len(matches) == 2


class TestFieldHandling:
    """Tests for field value handling."""

    def test_searches_raw_attributes_as_json(self):
        """raw_attributes dict is searched as JSON string."""
        spans = [
            {
                "span_id": "1",
                "name": "test",
                "raw_attributes": {"nested": {"key": "ENG2-999"}},
            }
        ]

        matches = search("ENG2-999", spans)

        assert len(matches) == 1
        assert matches[0].field == "raw_attributes"

    def test_handles_list_values_as_json(self):
        """List values are converted to JSON for searching."""
        spans = [
            {
                "span_id": "1",
                "name": "test",
                "input_messages": [{"role": "user", "content": "ENG2-123"}],
            }
        ]

        matches = search("ENG2-123", spans)

        assert len(matches) == 1


class TestMatchLocation:
    """Tests for match location tracking."""

    def test_match_start_and_end_positions(self):
        """Match includes correct start and end positions."""
        spans = [{"span_id": "1", "name": "hello ENG2-123 world"}]

        matches = search("ENG2-123", spans)

        assert len(matches) == 1
        assert matches[0].match_start == 6  # "hello " is 6 chars
        assert matches[0].match_end == 14  # "ENG2-123" is 8 chars
        assert matches[0].matched_text == "ENG2-123"

    def test_multiple_matches_in_same_field(self):
        """Multiple matches in same field are all returned."""
        spans = [{"span_id": "1", "name": "ENG2-123 and ENG2-456"}]

        matches = search(r"ENG2-\d+", spans)

        assert len(matches) == 2
        assert matches[0].matched_text == "ENG2-123"
        assert matches[1].matched_text == "ENG2-456"
        assert matches[0].match_start == 0
        assert matches[1].match_start == 13


class TestFieldValidation:
    """Test Case: Field validation for Story 2.3."""

    def test_valid_fields_pass_validation(self):
        """Valid field names pass validation without error."""
        # Should not raise
        validate_fields(["name", "input_value", "output_value"])
        validate_fields(["span_id", "trace_id"])
        validate_fields(None)  # None is valid (uses defaults)

    def test_single_invalid_field_raises_error(self):
        """Single invalid field raises InvalidFieldError."""
        with pytest.raises(InvalidFieldError) as exc_info:
            validate_fields(["nonexistent_field"])

        error_msg = str(exc_info.value)
        assert "nonexistent_field" in error_msg
        assert "Valid fields" in error_msg

    def test_multiple_invalid_fields_lists_all(self):
        """Multiple invalid fields are all listed in error."""
        with pytest.raises(InvalidFieldError) as exc_info:
            validate_fields(["bad1", "bad2", "name"])  # name is valid

        error_msg = str(exc_info.value)
        assert "bad1" in error_msg
        assert "bad2" in error_msg

    def test_search_with_invalid_field_raises_error(self):
        """search() with invalid field raises InvalidFieldError."""
        spans = [{"span_id": "1", "name": "test"}]

        with pytest.raises(InvalidFieldError):
            search("test", spans, fields=["invalid_field"])

    def test_search_file_with_invalid_field_raises_error(self, tmp_path):
        """search_file() with invalid field raises InvalidFieldError."""
        file_path = tmp_path / "test.jsonl"
        file_path.write_text('{"span_id": "1", "name": "test"}\n')

        with pytest.raises(InvalidFieldError):
            search_file("test", file_path, fields=["invalid_field"])

    def test_valid_search_fields_constant(self):
        """VALID_SEARCH_FIELDS contains expected fields."""
        # Default fields should be valid
        for field in DEFAULT_SEARCH_FIELDS:
            assert field in VALID_SEARCH_FIELDS

        # Additional fields should be valid
        assert "span_id" in VALID_SEARCH_FIELDS
        assert "trace_id" in VALID_SEARCH_FIELDS
        assert "span_kind" in VALID_SEARCH_FIELDS

    def test_search_with_multiple_valid_fields(self):
        """search() works with multiple valid fields."""
        spans = [
            {"span_id": "1", "name": "match", "input_value": "other"},
            {"span_id": "2", "name": "other", "input_value": "match"},
        ]

        matches = search("match", spans, fields=["name", "input_value"])

        assert len(matches) == 2

    def test_error_message_lists_valid_fields(self):
        """Error message includes list of valid fields."""
        with pytest.raises(InvalidFieldError) as exc_info:
            validate_fields(["bad_field"])

        error_msg = str(exc_info.value)
        # Should list some valid fields
        assert "name" in error_msg
        assert "input_value" in error_msg
