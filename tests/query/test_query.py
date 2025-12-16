"""
Tests for the unified query API.

Test Cases from Story 2.2:
1. Pattern Query: `query(pattern="ENG2-\\d+")` returns matching sessions
2. Session Query: `query(session_id="abc")` returns specific session
3. Combined: `query(pattern="error", session_id="abc")` filters within session
4. No Results: Returns empty list, not error
5. API Usable: Can call from Python without CLI
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from dev_agent_lens.query.query import (
    QueryResult,
    query,
    query_file,
)


# Test fixtures
@pytest.fixture
def sample_spans():
    """Sample spans for testing.

    Note: Session IDs are extracted from raw_attributes using the pattern:
    - raw_attributes is a dict containing nested metadata
    - The session ID comes from user_id field with pattern "session_<id>" or "user_session_<id>"
    """
    return [
        {
            "span_id": "span1",
            "trace_id": "trace1",
            "name": "Working on ENG2-123",
            "input_value": "Starting task",
            "output_value": "Completed",
            "start_time": "2024-01-01T10:00:00",
            "end_time": "2024-01-01T10:05:00",
            "status_code": "OK",
            "llm_model_name": "claude-3-sonnet",
            # raw_attributes as dict (how it comes from normalization)
            "raw_attributes": {"metadata": {"user_id": "user_session_abc123"}},
        },
        {
            "span_id": "span2",
            "trace_id": "trace1",
            "name": "Error handling",
            "input_value": "Processing error",
            "output_value": "Error occurred",
            "start_time": "2024-01-01T10:10:00",
            "end_time": "2024-01-01T10:15:00",
            "status_code": "ERROR",
            "llm_model_name": "claude-3-sonnet",
            "raw_attributes": {"metadata": {"user_id": "user_session_abc123"}},
        },
        {
            "span_id": "span3",
            "trace_id": "trace2",
            "name": "Working on ENG2-456",
            "input_value": "Different task",
            "output_value": "Done",
            "start_time": "2024-01-01T11:00:00",
            "end_time": "2024-01-01T11:30:00",
            "status_code": "OK",
            "llm_model_name": "gpt-4",
            "raw_attributes": {"metadata": {"user_id": "user_session_xyz789"}},
        },
    ]


@pytest.fixture
def sample_jsonl_file(tmp_path, sample_spans):
    """Create a JSONL file with sample spans."""
    file_path = tmp_path / "sessions.jsonl"
    with open(file_path, "w") as f:
        for span in sample_spans:
            f.write(json.dumps(span) + "\n")
    return file_path


class TestQueryResult:
    """Tests for QueryResult dataclass."""

    def test_to_dict(self):
        """to_dict returns serializable dict."""
        result = QueryResult(
            sessions=[{"session_id": "abc", "spans": [], "span_count": 0}],
            total_spans=5,
            total_sessions=1,
            query_params={"pattern": "test"},
        )

        d = result.to_dict()

        assert d["total_spans"] == 5
        assert d["total_sessions"] == 1
        assert d["query_params"]["pattern"] == "test"

    def test_to_dataframe(self, sample_spans):
        """to_dataframe returns flat DataFrame of all spans."""
        result = QueryResult(
            sessions=[
                {"session_id": "abc", "spans": sample_spans[:2], "span_count": 2},
                {"session_id": "xyz", "spans": sample_spans[2:], "span_count": 1},
            ],
            total_spans=3,
            total_sessions=2,
        )

        df = result.to_dataframe()

        assert len(df) == 3
        assert "span_id" in df.columns

    def test_to_dataframe_empty(self):
        """to_dataframe with empty result returns empty DataFrame."""
        result = QueryResult(sessions=[], total_spans=0, total_sessions=0)

        df = result.to_dataframe()

        assert len(df) == 0


class TestPatternQuery:
    """Test Case 1: Pattern queries."""

    def test_query_pattern_returns_matching_sessions(self, sample_spans):
        """query(pattern="ENG2-\\d+") returns matching sessions."""
        result = query(pattern=r"ENG2-\d+", spans=sample_spans)

        assert result.total_sessions == 2
        assert result.total_spans == 2

        # Check that we got spans with ENG2 references
        all_spans = result.to_dataframe()
        assert "ENG2-123" in all_spans["name"].iloc[0] or "ENG2-456" in all_spans["name"].iloc[0]

    def test_query_pattern_case_insensitive(self, sample_spans):
        """Pattern with case_insensitive flag works."""
        result = query(pattern="error", spans=sample_spans, case_insensitive=True)

        # Should match "Error" in span2
        assert result.total_spans >= 1

    def test_query_pattern_specific_fields(self, sample_spans):
        """Pattern search in specific fields."""
        result = query(pattern="Starting", spans=sample_spans, fields=["input_value"])

        assert result.total_spans == 1
        df = result.to_dataframe()
        assert df.iloc[0]["span_id"] == "span1"


class TestSessionQuery:
    """Test Case 2: Session queries."""

    def test_query_session_id_returns_specific_session(self, sample_spans):
        """query(session_id="abc123") returns specific session."""
        result = query(session_id="abc123", spans=sample_spans)

        assert result.total_sessions == 1
        assert result.total_spans == 2
        assert result.sessions[0]["session_id"] == "abc123"

    def test_query_session_id_not_found(self, sample_spans):
        """query with non-existent session_id returns empty."""
        result = query(session_id="nonexistent", spans=sample_spans)

        assert result.total_sessions == 0
        assert result.total_spans == 0


class TestCombinedFilters:
    """Test Case 3: Combined filters."""

    def test_query_pattern_and_session_id(self, sample_spans):
        """query(pattern="error", session_id="abc123") filters within session."""
        result = query(
            pattern="error",
            session_id="abc123",
            spans=sample_spans,
            case_insensitive=True,
        )

        # Should find the error span in session abc123
        assert result.total_spans >= 1
        df = result.to_dataframe()
        assert all(df["span_id"].isin(["span2"]))  # Only error span in abc123 session

    def test_query_status_code_filter(self, sample_spans):
        """query with status_code filter."""
        result = query(status_code="ERROR", spans=sample_spans)

        assert result.total_spans == 1
        df = result.to_dataframe()
        assert df.iloc[0]["status_code"] == "ERROR"

    def test_query_model_name_filter(self, sample_spans):
        """query with model_name filter (case-insensitive partial match)."""
        result = query(model_name="claude", spans=sample_spans)

        assert result.total_spans == 2  # span1 and span2 use claude
        df = result.to_dataframe()
        assert all("claude" in name.lower() for name in df["llm_model_name"])

    def test_query_time_range_filter(self, sample_spans):
        """query with time range filter."""
        result = query(
            start_time="2024-01-01T10:05:00",
            end_time="2024-01-01T10:30:00",
            spans=sample_spans,
        )

        assert result.total_spans == 1
        df = result.to_dataframe()
        assert df.iloc[0]["span_id"] == "span2"

    def test_query_all_filters_combined(self, sample_spans):
        """All filters combined with AND logic."""
        result = query(
            pattern="error",
            session_id="abc123",
            status_code="ERROR",
            model_name="claude",
            spans=sample_spans,
            case_insensitive=True,
        )

        assert result.total_spans == 1
        df = result.to_dataframe()
        assert df.iloc[0]["span_id"] == "span2"


class TestNoResults:
    """Test Case 4: No results handling."""

    def test_no_matches_returns_empty_not_error(self, sample_spans):
        """No matches returns empty QueryResult, not error."""
        result = query(pattern="nonexistent_pattern_xyz", spans=sample_spans)

        assert result.total_spans == 0
        assert result.total_sessions == 0
        assert result.sessions == []

    def test_empty_spans_returns_empty(self):
        """Empty spans input returns empty result."""
        result = query(pattern="test", spans=[])

        assert result.total_spans == 0
        assert result.total_sessions == 0

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """Nonexistent file returns empty result."""
        result = query(file_path=tmp_path / "nonexistent.jsonl")

        assert result.total_spans == 0
        assert result.total_sessions == 0


class TestPythonAPI:
    """Test Case 5: Python API usability."""

    def test_can_call_from_python_without_cli(self, sample_spans):
        """API works independently of CLI."""
        # Import and use directly
        from dev_agent_lens.query import query as query_func

        result = query_func(pattern="ENG2", spans=sample_spans)

        assert isinstance(result, QueryResult)
        assert result.total_spans > 0

    def test_accepts_dataframe_input(self, sample_spans):
        """API accepts DataFrame as input."""
        df = pd.DataFrame(sample_spans)

        result = query(pattern="ENG2", spans=df)

        assert result.total_spans == 2

    def test_accepts_file_path(self, sample_jsonl_file):
        """API accepts file path."""
        result = query(pattern="ENG2", file_path=sample_jsonl_file)

        assert result.total_spans == 2

    def test_query_file_convenience_function(self, sample_jsonl_file):
        """query_file convenience function works."""
        result = query_file(sample_jsonl_file, pattern="ENG2")

        assert result.total_spans == 2


class TestSessionGrouping:
    """Tests for session grouping behavior."""

    def test_groups_spans_by_session(self, sample_spans):
        """Spans are grouped by session_id."""
        result = query(spans=sample_spans)

        assert result.total_sessions == 2  # abc123 and xyz789

        session_ids = [s["session_id"] for s in result.sessions]
        assert "abc123" in session_ids
        assert "xyz789" in session_ids

    def test_flat_mode_returns_ungrouped(self, sample_spans):
        """flat=True returns spans without grouping."""
        result = query(spans=sample_spans, flat=True)

        assert result.total_sessions == 1  # Single "session" with all spans
        assert result.total_spans == 3
        assert result.sessions[0]["session_id"] is None

    def test_sessions_sorted_by_most_recent(self, sample_spans):
        """Sessions are sorted by most recent activity."""
        result = query(spans=sample_spans)

        # xyz789 has later end_time (11:30) than abc123 (10:15)
        assert result.sessions[0]["session_id"] == "xyz789"

    def test_spans_within_session_sorted_by_time(self, sample_spans):
        """Spans within each session are sorted by start_time."""
        result = query(session_id="abc123", spans=sample_spans)

        spans = result.sessions[0]["spans"]
        assert len(spans) == 2
        # span1 (10:00) should come before span2 (10:10)
        assert spans[0]["span_id"] == "span1"
        assert spans[1]["span_id"] == "span2"


class TestQueryParams:
    """Tests for query params tracking."""

    def test_query_params_stored_in_result(self, sample_spans):
        """Query parameters are stored in result."""
        result = query(
            pattern="test",
            session_id="abc",
            status_code="OK",
            spans=sample_spans,
        )

        assert result.query_params["pattern"] == "test"
        assert result.query_params["session_id"] == "abc"
        assert result.query_params["status_code"] == "OK"


class TestErrorHandling:
    """Tests for error handling."""

    def test_no_data_source_raises_error(self):
        """Missing both spans and file_path raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            query(pattern="test")

        assert "spans" in str(exc_info.value).lower() or "file_path" in str(exc_info.value).lower()

    def test_invalid_pattern_raises_regex_error(self, sample_spans):
        """Invalid regex pattern raises RegexSearchError."""
        from dev_agent_lens.query import RegexSearchError

        with pytest.raises(RegexSearchError):
            query(pattern="[invalid", spans=sample_spans)


class TestTimeFiltering:
    """Tests for time range filtering."""

    def test_start_time_as_datetime(self, sample_spans):
        """start_time accepts datetime object."""
        result = query(
            start_time=datetime(2024, 1, 1, 10, 30),
            spans=sample_spans,
        )

        # Only span3 starts after 10:30
        assert result.total_spans == 1

    def test_end_time_as_datetime(self, sample_spans):
        """end_time accepts datetime object."""
        result = query(
            end_time=datetime(2024, 1, 1, 10, 5),
            spans=sample_spans,
        )

        # Only span1 starts at or before 10:05
        assert result.total_spans == 1

    def test_time_range_as_strings(self, sample_spans):
        """Time range accepts ISO string format."""
        result = query(
            start_time="2024-01-01T10:00:00",
            end_time="2024-01-01T10:10:00",
            spans=sample_spans,
        )

        # span1 at 10:00 and span2 at 10:10 (boundary)
        assert result.total_spans == 2
