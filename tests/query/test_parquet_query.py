"""
Tests for the Parquet query backend.

Tests the DuckDB-based Parquet query functionality for high-performance
querying of trace data.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

# Check if DuckDB is available
try:
    import duckdb
    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False

from dev_agent_lens.query.parquet_query import (
    _build_filter_sql,
    _check_duckdb_available,
    _group_spans_by_session,
    _parse_datetime,
    find_parquet_files,
    get_parquet_stats,
    query_parquet,
    query_source,
    search_parquet,
)
from dev_agent_lens.query.query import QueryResult


# Skip all tests if DuckDB is not available
pytestmark = pytest.mark.skipif(
    not DUCKDB_AVAILABLE,
    reason="DuckDB not installed"
)


@pytest.fixture
def sample_spans_data():
    """Sample span data for creating test Parquet files."""
    return [
        {
            "session_id": "session_abc123",
            "source": "test-source",
            "span_id": "span1",
            "trace_id": "trace1",
            "parent_id": None,
            "name": "Working on ENG2-123",
            "span_kind": "LLM",
            "start_time": datetime(2024, 1, 1, 10, 0, 0),
            "end_time": datetime(2024, 1, 1, 10, 5, 0),
            "status_code": "OK",
            "input_value": "Starting task",
            "output_value": "Completed successfully",
            "input_messages": "[{\"role\": \"user\", \"content\": \"help\"}]",
            "output_messages": "[{\"role\": \"assistant\", \"content\": \"ok\"}]",
            "llm_model_name": "claude-3-sonnet",
            "llm_token_count_prompt": 100,
            "llm_token_count_completion": 50,
            "llm_token_count_total": 150,
            "backend": "phoenix",
            "raw_attributes_json": "{}",
        },
        {
            "session_id": "session_abc123",
            "source": "test-source",
            "span_id": "span2",
            "trace_id": "trace1",
            "parent_id": "span1",
            "name": "Error handling",
            "span_kind": "LLM",
            "start_time": datetime(2024, 1, 1, 10, 10, 0),
            "end_time": datetime(2024, 1, 1, 10, 15, 0),
            "status_code": "ERROR",
            "input_value": "Processing error",
            "output_value": "Error occurred in ENG2-123",
            "input_messages": None,
            "output_messages": None,
            "llm_model_name": "claude-3-sonnet",
            "llm_token_count_prompt": 80,
            "llm_token_count_completion": 20,
            "llm_token_count_total": 100,
            "backend": "phoenix",
            "raw_attributes_json": "{\"error\": true}",
        },
        {
            "session_id": "session_xyz789",
            "source": "test-source",
            "span_id": "span3",
            "trace_id": "trace2",
            "parent_id": None,
            "name": "Working on ENG2-456",
            "span_kind": "LLM",
            "start_time": datetime(2024, 1, 1, 11, 0, 0),
            "end_time": datetime(2024, 1, 1, 11, 30, 0),
            "status_code": "OK",
            "input_value": "Different task",
            "output_value": "Done with gpt-4",
            "input_messages": None,
            "output_messages": None,
            "llm_model_name": "gpt-4",
            "llm_token_count_prompt": 200,
            "llm_token_count_completion": 100,
            "llm_token_count_total": 300,
            "backend": "phoenix",
            "raw_attributes_json": "{}",
        },
    ]


@pytest.fixture
def sample_parquet_file(tmp_path, sample_spans_data):
    """Create a Parquet file with sample data."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    spans_path = tmp_path / "test_spans.parquet"

    table = pa.Table.from_pylist(sample_spans_data)
    pq.write_table(table, spans_path, compression="zstd")

    return spans_path


@pytest.fixture
def sample_parquet_source(tmp_path, sample_spans_data):
    """Create a complete Parquet source with sessions and spans files."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Create parquet directory
    parquet_dir = tmp_path / "parquet"
    parquet_dir.mkdir()

    spans_path = parquet_dir / "test-source_spans.parquet"
    sessions_path = parquet_dir / "test-source_sessions.parquet"

    # Write spans
    spans_table = pa.Table.from_pylist(sample_spans_data)
    pq.write_table(spans_table, spans_path, compression="zstd")

    # Write sessions (aggregated)
    sessions_data = [
        {
            "session_id": "session_abc123",
            "source": "test-source",
            "span_count": 2,
            "first_span_time": datetime(2024, 1, 1, 10, 0, 0),
            "last_span_time": datetime(2024, 1, 1, 10, 15, 0),
            "total_prompt_tokens": 180,
            "total_completion_tokens": 70,
            "total_tokens": 250,
            "models_used": "claude-3-sonnet",
            "has_errors": True,
        },
        {
            "session_id": "session_xyz789",
            "source": "test-source",
            "span_count": 1,
            "first_span_time": datetime(2024, 1, 1, 11, 0, 0),
            "last_span_time": datetime(2024, 1, 1, 11, 30, 0),
            "total_prompt_tokens": 200,
            "total_completion_tokens": 100,
            "total_tokens": 300,
            "models_used": "gpt-4",
            "has_errors": False,
        },
    ]
    sessions_table = pa.Table.from_pylist(sessions_data)
    pq.write_table(sessions_table, sessions_path, compression="zstd")

    return tmp_path


class TestDuckDBAvailability:
    """Tests for DuckDB availability check."""

    def test_check_duckdb_available(self):
        """DuckDB should be available in test environment."""
        assert _check_duckdb_available() is True


class TestParseDatetime:
    """Tests for datetime parsing."""

    def test_parse_datetime_none(self):
        """None returns None."""
        assert _parse_datetime(None) is None

    def test_parse_datetime_datetime_object(self):
        """datetime object returns ISO string."""
        dt = datetime(2024, 1, 1, 10, 0, 0)
        result = _parse_datetime(dt)
        assert result == "2024-01-01T10:00:00"

    def test_parse_datetime_string(self):
        """ISO string passes through."""
        s = "2024-01-01T10:00:00"
        assert _parse_datetime(s) == s


class TestBuildFilterSQL:
    """Tests for SQL filter building."""

    def test_no_filters(self):
        """No filters returns empty WHERE clause."""
        where, params = _build_filter_sql()
        assert where == ""
        assert params == []

    def test_session_id_filter(self):
        """session_id filter builds correct SQL."""
        where, params = _build_filter_sql(session_id="abc123")
        assert "session_id = ?" in where
        assert params == ["abc123"]

    def test_status_code_filter(self):
        """status_code filter builds correct SQL."""
        where, params = _build_filter_sql(status_code="ERROR")
        assert "status_code = ?" in where
        assert params == ["ERROR"]

    def test_model_name_filter(self):
        """model_name filter builds case-insensitive LIKE."""
        where, params = _build_filter_sql(model_name="Claude")
        assert "LOWER(llm_model_name) LIKE ?" in where
        assert params == ["%claude%"]

    def test_time_range_filters(self):
        """Time range filters build correct SQL."""
        where, params = _build_filter_sql(
            start_time="2024-01-01T10:00:00",
            end_time="2024-01-01T12:00:00",
        )
        assert "start_time >= ?" in where
        assert "start_time <= ?" in where
        assert len(params) == 2

    def test_combined_filters(self):
        """Multiple filters combine with AND."""
        where, params = _build_filter_sql(
            session_id="abc",
            status_code="OK",
            model_name="claude",
        )
        assert where.startswith("WHERE")
        assert " AND " in where
        assert len(params) == 3


class TestGroupSpansBySession:
    """Tests for session grouping."""

    def test_groups_by_session_id(self, sample_spans_data):
        """Spans are grouped by session_id."""
        sessions = _group_spans_by_session(sample_spans_data)

        assert len(sessions) == 2
        session_ids = {s["session_id"] for s in sessions}
        assert "session_abc123" in session_ids
        assert "session_xyz789" in session_ids

    def test_session_metadata(self, sample_spans_data):
        """Sessions have correct metadata."""
        sessions = _group_spans_by_session(sample_spans_data)

        # Find the abc123 session
        abc_session = next(s for s in sessions if s["session_id"] == "session_abc123")

        assert abc_session["span_count"] == 2
        assert abc_session["start_time"] is not None
        assert abc_session["end_time"] is not None

    def test_sessions_sorted_by_most_recent(self, sample_spans_data):
        """Sessions are sorted by most recent first."""
        sessions = _group_spans_by_session(sample_spans_data)

        # xyz789 has later end_time, should be first
        assert sessions[0]["session_id"] == "session_xyz789"


class TestQueryParquet:
    """Tests for the main query_parquet function."""

    def test_query_all_spans(self, sample_parquet_file):
        """Query without filters returns all spans."""
        result = query_parquet(spans_path=sample_parquet_file)

        assert isinstance(result, QueryResult)
        assert result.total_spans == 3
        assert result.total_sessions == 2

    def test_query_by_session_id(self, sample_parquet_file):
        """Filter by session_id works."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            session_id="session_abc123",
        )

        assert result.total_spans == 2
        assert result.total_sessions == 1
        assert result.sessions[0]["session_id"] == "session_abc123"

    def test_query_by_status_code(self, sample_parquet_file):
        """Filter by status_code works."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            status_code="ERROR",
        )

        assert result.total_spans == 1
        df = result.to_dataframe()
        assert df.iloc[0]["status_code"] == "ERROR"

    def test_query_by_model_name(self, sample_parquet_file):
        """Filter by model_name works (case-insensitive partial match)."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            model_name="claude",
        )

        assert result.total_spans == 2
        df = result.to_dataframe()
        assert all("claude" in name.lower() for name in df["llm_model_name"])

    def test_query_with_pattern(self, sample_parquet_file):
        """Regex pattern filtering works."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            pattern=r"ENG2-\d+",
        )

        assert result.total_spans == 3  # All spans mention ENG2-xxx

    def test_query_with_pattern_case_insensitive(self, sample_parquet_file):
        """Case-insensitive pattern matching works."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            pattern="error",
            case_insensitive=True,
        )

        assert result.total_spans >= 1

    def test_query_flat_mode(self, sample_parquet_file):
        """flat=True returns ungrouped spans."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            flat=True,
        )

        assert result.total_sessions == 1
        assert result.sessions[0]["session_id"] is None
        assert result.total_spans == 3

    def test_query_with_limit(self, sample_parquet_file):
        """limit parameter works."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            limit=2,
        )

        assert result.total_spans == 2

    def test_query_nonexistent_file_raises(self, tmp_path):
        """Nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            query_parquet(spans_path=tmp_path / "nonexistent.parquet")

    def test_query_result_has_backend_parquet(self, sample_parquet_file):
        """Result query_params includes backend=parquet."""
        result = query_parquet(spans_path=sample_parquet_file)

        assert result.query_params.get("backend") == "parquet"


class TestSearchParquet:
    """Tests for the search_parquet function."""

    def test_search_finds_matches(self, sample_parquet_file):
        """search_parquet finds matching spans."""
        spans = search_parquet(
            spans_path=sample_parquet_file,
            pattern=r"ENG2-\d+",
        )

        assert len(spans) >= 1
        assert isinstance(spans[0], dict)

    def test_search_case_insensitive(self, sample_parquet_file):
        """Case-insensitive search works."""
        spans = search_parquet(
            spans_path=sample_parquet_file,
            pattern="ERROR",
            case_insensitive=True,
        )

        assert len(spans) >= 1

    def test_search_specific_fields(self, sample_parquet_file):
        """Search in specific fields."""
        spans = search_parquet(
            spans_path=sample_parquet_file,
            pattern="Starting",
            fields=["input_value"],
        )

        assert len(spans) == 1
        assert spans[0]["input_value"] == "Starting task"

    def test_search_with_limit(self, sample_parquet_file):
        """Search respects limit."""
        spans = search_parquet(
            spans_path=sample_parquet_file,
            pattern=".*",
            limit=1,
        )

        assert len(spans) == 1

    def test_search_no_matches_returns_empty(self, sample_parquet_file):
        """No matches returns empty list."""
        spans = search_parquet(
            spans_path=sample_parquet_file,
            pattern="nonexistent_xyz_pattern",
        )

        assert spans == []


class TestGetParquetStats:
    """Tests for file statistics."""

    def test_get_stats(self, sample_parquet_file):
        """get_parquet_stats returns correct info."""
        stats = get_parquet_stats(sample_parquet_file)

        assert stats["row_count"] == 3
        assert stats["session_count"] == 2
        assert "file_size_bytes" in stats
        assert "columns" in stats
        assert "session_id" in stats["columns"]

    def test_stats_nonexistent_file_raises(self, tmp_path):
        """Nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            get_parquet_stats(tmp_path / "nonexistent.parquet")


class TestFindParquetFiles:
    """Tests for Parquet file discovery."""

    def test_find_parquet_files(self, sample_parquet_source):
        """find_parquet_files discovers available sources."""
        sources = find_parquet_files(data_path=sample_parquet_source)

        assert "test-source" in sources
        assert "spans" in sources["test-source"]
        assert "sessions" in sources["test-source"]

    def test_find_parquet_files_filter_by_source(self, sample_parquet_source):
        """Filter by specific source name."""
        sources = find_parquet_files(
            source="test-source",
            data_path=sample_parquet_source,
        )

        assert len(sources) == 1
        assert "test-source" in sources

    def test_find_parquet_files_nonexistent_source(self, sample_parquet_source):
        """Nonexistent source returns empty."""
        sources = find_parquet_files(
            source="nonexistent",
            data_path=sample_parquet_source,
        )

        assert sources == {}


class TestQuerySource:
    """Tests for the unified query_source function."""

    def test_query_source_uses_parquet(self, sample_parquet_source):
        """query_source uses Parquet when available."""
        result = query_source(
            source="test-source",
            data_path=sample_parquet_source,
        )

        assert result.total_spans == 3
        assert result.query_params.get("backend") == "parquet"

    def test_query_source_with_filters(self, sample_parquet_source):
        """query_source passes through filters."""
        result = query_source(
            source="test-source",
            session_id="session_abc123",
            data_path=sample_parquet_source,
        )

        assert result.total_spans == 2

    def test_query_source_nonexistent_returns_empty(self, tmp_path):
        """Nonexistent source returns empty result."""
        result = query_source(
            source="nonexistent",
            data_path=tmp_path,
        )

        # Should return empty result, not raise
        assert result.total_spans == 0


class TestRawAttributesParsing:
    """Tests for raw_attributes JSON parsing."""

    def test_raw_attributes_parsed(self, sample_parquet_file):
        """raw_attributes_json is parsed back to dict."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            session_id="session_abc123",
            status_code="ERROR",
        )

        df = result.to_dataframe()
        span = df.iloc[0]

        # raw_attributes should be a dict, not JSON string
        assert isinstance(span["raw_attributes"], dict)
        assert span["raw_attributes"].get("error") is True


class TestCombinedFilters:
    """Tests for combining multiple filters."""

    def test_all_filters_combined(self, sample_parquet_file):
        """All filters work together with AND logic."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            session_id="session_abc123",
            status_code="ERROR",
            model_name="claude",
        )

        assert result.total_spans == 1
        df = result.to_dataframe()
        assert df.iloc[0]["span_id"] == "span2"

    def test_pattern_with_sql_filters(self, sample_parquet_file):
        """Pattern filter works with SQL filters."""
        result = query_parquet(
            spans_path=sample_parquet_file,
            pattern=r"ENG2-123",
            status_code="ERROR",
        )

        # Should find span2 which has ERROR status and mentions ENG2-123
        assert result.total_spans == 1


class TestQuerySessionsParquetBackend:
    """Tests for query_sessions with Parquet backend support."""

    def test_query_sessions_with_source_uses_parquet(self, sample_parquet_source):
        """query_sessions with source uses Parquet backend when available."""
        from dev_agent_lens.query.query import query_sessions

        sessions = query_sessions(
            source="test-source",
            storage_path=sample_parquet_source,
        )

        assert len(sessions) == 2
        session_ids = {s["session_id"] for s in sessions}
        assert "session_abc123" in session_ids
        assert "session_xyz789" in session_ids

    def test_query_sessions_with_source_and_session_id(self, sample_parquet_source):
        """query_sessions with source and session_id filter."""
        from dev_agent_lens.query.query import query_sessions

        sessions = query_sessions(
            source="test-source",
            session_id="session_abc123",
            storage_path=sample_parquet_source,
        )

        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "session_abc123"
        assert sessions[0]["span_count"] == 2

    def test_query_sessions_with_source_and_search(self, sample_parquet_source):
        """query_sessions with source and search pattern."""
        from dev_agent_lens.query.query import query_sessions

        sessions = query_sessions(
            source="test-source",
            search=r"ENG2-456",
            storage_path=sample_parquet_source,
        )

        # Should find the session with span3 which mentions ENG2-456
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "session_xyz789"

    def test_query_sessions_prefer_parquet_false(self, sample_parquet_source):
        """query_sessions with prefer_parquet=False falls back to JSONL."""
        from dev_agent_lens.query.query import query_sessions

        # With prefer_parquet=False, it should try JSONL even if source specified
        # Since there's no JSONL file, it should return empty
        sessions = query_sessions(
            source="test-source",
            prefer_parquet=False,
            storage_path=sample_parquet_source,
        )

        # Falls back to JSONL which doesn't exist, returns empty
        assert sessions == []

    def test_query_sessions_nonexistent_source(self, tmp_path):
        """query_sessions with nonexistent source returns empty."""
        from dev_agent_lens.query.query import query_sessions

        sessions = query_sessions(
            source="nonexistent-source",
            storage_path=tmp_path,
        )

        assert sessions == []

    def test_query_sessions_without_source_uses_jsonl(self, tmp_path):
        """query_sessions without source uses JSONL backend."""
        from dev_agent_lens.query.query import query_sessions

        # Without source, tries JSONL which doesn't exist
        sessions = query_sessions(storage_path=tmp_path)

        assert sessions == []

    def test_query_sessions_returns_spans_in_sessions(self, sample_parquet_source):
        """query_sessions returns sessions with spans included."""
        from dev_agent_lens.query.query import query_sessions

        sessions = query_sessions(
            source="test-source",
            session_id="session_abc123",
            storage_path=sample_parquet_source,
        )

        assert len(sessions) == 1
        session = sessions[0]
        assert "spans" in session
        assert len(session["spans"]) == 2

        # Check span data is present
        span_ids = {s.get("span_id") for s in session["spans"]}
        assert "span1" in span_ids
        assert "span2" in span_ids
