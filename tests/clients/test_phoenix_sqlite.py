"""
Tests for PhoenixSQLiteClient.

These tests verify the Phoenix SQLite client functionality including:
- Connection handling for both local and Docker modes
- DataFrame fetching with various filters
- Annotation fetching
- Utility methods (count, time range)
- Error handling for various failure scenarios
"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dev_agent_lens.clients.phoenix_sqlite import (
    PhoenixSQLiteClient,
    PhoenixSQLiteConnectionError,
    PhoenixSQLiteError,
    PhoenixSQLiteQueryError,
)


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database with Phoenix schema."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Create database with Phoenix schema
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create projects table
    cursor.execute("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            description VARCHAR
        )
    """)

    # Create traces table
    cursor.execute("""
        CREATE TABLE traces (
            id INTEGER PRIMARY KEY,
            project_rowid INTEGER NOT NULL,
            trace_id VARCHAR NOT NULL,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            FOREIGN KEY (project_rowid) REFERENCES projects(id)
        )
    """)

    # Create spans table
    cursor.execute("""
        CREATE TABLE spans (
            id INTEGER PRIMARY KEY,
            trace_rowid INTEGER NOT NULL,
            span_id VARCHAR NOT NULL,
            parent_id VARCHAR,
            name VARCHAR,
            span_kind VARCHAR,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            attributes TEXT,
            events TEXT,
            status_code VARCHAR,
            status_message VARCHAR,
            cumulative_error_count INTEGER,
            cumulative_llm_token_count_prompt INTEGER,
            cumulative_llm_token_count_completion INTEGER,
            llm_token_count_prompt INTEGER,
            llm_token_count_completion INTEGER,
            FOREIGN KEY (trace_rowid) REFERENCES traces(id)
        )
    """)

    # Create span_annotations table
    cursor.execute("""
        CREATE TABLE span_annotations (
            id INTEGER PRIMARY KEY,
            span_rowid INTEGER NOT NULL,
            name VARCHAR,
            label VARCHAR,
            score FLOAT,
            explanation VARCHAR,
            metadata TEXT,
            annotator_kind VARCHAR,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY (span_rowid) REFERENCES spans(id)
        )
    """)

    # Insert test project
    cursor.execute("INSERT INTO projects (id, name) VALUES (1, 'dev-agent-lens')")

    # Insert test traces
    cursor.execute("""
        INSERT INTO traces (id, project_rowid, trace_id, start_time, end_time)
        VALUES (1, 1, 'trace-001', '2025-01-01T10:00:00', '2025-01-01T10:05:00')
    """)
    cursor.execute("""
        INSERT INTO traces (id, project_rowid, trace_id, start_time, end_time)
        VALUES (2, 1, 'trace-002', '2025-01-01T11:00:00', '2025-01-01T11:05:00')
    """)

    # Insert test spans
    cursor.execute("""
        INSERT INTO spans (
            id, trace_rowid, span_id, parent_id, name, span_kind,
            start_time, end_time, attributes, events,
            status_code, status_message,
            cumulative_error_count,
            cumulative_llm_token_count_prompt,
            cumulative_llm_token_count_completion,
            llm_token_count_prompt,
            llm_token_count_completion
        ) VALUES (
            1, 1, 'span-001', NULL, 'LLM Call', 'LLM',
            '2025-01-01T10:00:00', '2025-01-01T10:00:05',
            '{"model": "gpt-4", "temperature": 0.7}',
            '[]',
            'OK', NULL,
            0, 100, 50, 100, 50
        )
    """)
    cursor.execute("""
        INSERT INTO spans (
            id, trace_rowid, span_id, parent_id, name, span_kind,
            start_time, end_time, attributes, events,
            status_code, status_message,
            cumulative_error_count,
            cumulative_llm_token_count_prompt,
            cumulative_llm_token_count_completion,
            llm_token_count_prompt,
            llm_token_count_completion
        ) VALUES (
            2, 1, 'span-002', 'span-001', 'Tool Call', 'TOOL',
            '2025-01-01T10:00:02', '2025-01-01T10:00:04',
            '{"tool": "search"}',
            '[]',
            'OK', NULL,
            0, 0, 0, 0, 0
        )
    """)
    cursor.execute("""
        INSERT INTO spans (
            id, trace_rowid, span_id, parent_id, name, span_kind,
            start_time, end_time, attributes, events,
            status_code, status_message,
            cumulative_error_count,
            cumulative_llm_token_count_prompt,
            cumulative_llm_token_count_completion,
            llm_token_count_prompt,
            llm_token_count_completion
        ) VALUES (
            3, 2, 'span-003', NULL, 'Agent Run', 'AGENT',
            '2025-01-01T11:00:00', '2025-01-01T11:00:10',
            '{}',
            '[]',
            'OK', NULL,
            0, 200, 150, 200, 150
        )
    """)

    # Insert test annotations
    cursor.execute("""
        INSERT INTO span_annotations (
            id, span_rowid, name, label, score, explanation,
            metadata, annotator_kind, created_at, updated_at
        ) VALUES (
            1, 1, 'quality', 'good', 0.9, 'High quality response',
            '{"reviewer": "human"}', 'HUMAN',
            '2025-01-01T10:10:00', '2025-01-01T10:10:00'
        )
    """)
    cursor.execute("""
        INSERT INTO span_annotations (
            id, span_rowid, name, label, score, explanation,
            metadata, annotator_kind, created_at, updated_at
        ) VALUES (
            2, 3, 'accuracy', 'excellent', 0.95, 'Very accurate',
            '{}', 'LLM',
            '2025-01-01T11:10:00', '2025-01-01T11:10:00'
        )
    """)

    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    Path(db_path).unlink(missing_ok=True)


class TestPhoenixSQLiteClientInit:
    """Tests for PhoenixSQLiteClient initialization."""

    def test_local_path_init(self, temp_db):
        """Given local path, client initializes correctly."""
        client = PhoenixSQLiteClient(temp_db, project="dev-agent-lens")
        assert client.db_path == temp_db
        assert client.project == "dev-agent-lens"
        assert client.readonly is True
        assert client._is_docker is False

    def test_docker_path_init(self):
        """Given Docker path, client parses it correctly."""
        client = PhoenixSQLiteClient(
            "docker://phoenix-container:/root/.phoenix/phoenix.db",
            project="test-project"
        )
        assert client._is_docker is True
        assert client._container_name == "phoenix-container"
        assert client._container_db_path == "/root/.phoenix/phoenix.db"
        assert client.project == "test-project"

    def test_env_var_project_default(self, temp_db):
        """Given no project, uses DAL_PHOENIX_PROJECT environment variable."""
        with patch.dict("os.environ", {"DAL_PHOENIX_PROJECT": "env-project"}):
            client = PhoenixSQLiteClient(temp_db)
            assert client.project == "env-project"

    def test_default_project(self, temp_db):
        """Given no project and no env var, uses default."""
        with patch.dict("os.environ", {}, clear=True):
            client = PhoenixSQLiteClient(temp_db)
            assert client.project == "dev-agent-lens"

    def test_readonly_false(self, temp_db):
        """Given readonly=False, sets flag correctly."""
        client = PhoenixSQLiteClient(temp_db, readonly=False)
        assert client.readonly is False

    def test_path_obj_conversion(self, temp_db):
        """Given Path object, converts to string."""
        client = PhoenixSQLiteClient(Path(temp_db))
        assert isinstance(client.db_path, str)

    def test_invalid_docker_path_no_colon(self):
        """Given invalid Docker path (no colon), raises error."""
        with pytest.raises(PhoenixSQLiteConnectionError, match="Invalid Docker path"):
            PhoenixSQLiteClient("docker://container-only")

    def test_invalid_docker_path_empty_parts(self):
        """Given invalid Docker path (empty parts), raises error."""
        with pytest.raises(PhoenixSQLiteConnectionError, match="Invalid Docker path"):
            PhoenixSQLiteClient("docker://:/path")


class TestPhoenixSQLiteClientConnection:
    """Tests for connection handling."""

    def test_local_connection_success(self, temp_db):
        """Given valid local database, connection succeeds."""
        client = PhoenixSQLiteClient(temp_db)
        assert client.test_connection() is True

    def test_local_connection_nonexistent_file(self):
        """Given nonexistent file, connection fails."""
        client = PhoenixSQLiteClient("/nonexistent/path/phoenix.db")
        assert client.test_connection() is False

    def test_local_connection_readonly_mode(self, temp_db):
        """Given readonly=True, connection uses read-only mode."""
        client = PhoenixSQLiteClient(temp_db, readonly=True)
        conn = client._get_connection()
        assert conn is not None
        client.close()

    def test_connection_reuse(self, temp_db):
        """Given multiple calls, reuses same connection."""
        client = PhoenixSQLiteClient(temp_db)
        conn1 = client._get_connection()
        conn2 = client._get_connection()
        assert conn1 is conn2
        client.close()

    def test_close_connection(self, temp_db):
        """Given close() call, closes connection."""
        client = PhoenixSQLiteClient(temp_db)
        client._get_connection()
        client.close()
        assert client._connection is None

    def test_context_manager(self, temp_db):
        """Given context manager usage, closes connection on exit."""
        with PhoenixSQLiteClient(temp_db) as client:
            client._get_connection()
            assert client._connection is not None
        assert client._connection is None

    def test_docker_connection_no_docker_installed(self):
        """Given Docker mode but no docker command, raises error."""
        client = PhoenixSQLiteClient(
            "docker://container:/path/phoenix.db"
        )
        with patch("subprocess.run", side_effect=FileNotFoundError("docker not found")):
            assert client.test_connection() is False


class TestPhoenixSQLiteClientFetchSpans:
    """Tests for span fetching."""

    def test_fetch_all_spans(self, temp_db):
        """Given no filters, returns all spans."""
        client = PhoenixSQLiteClient(temp_db, project="dev-agent-lens")
        df = client.get_spans_dataframe()

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert "context.span_id" in df.columns
        assert "context.trace_id" in df.columns
        assert list(df["context.span_id"]) == ["span-001", "span-002", "span-003"]

    def test_fetch_with_column_names(self, temp_db):
        """Given fetch, returns correct column names."""
        client = PhoenixSQLiteClient(temp_db)
        df = client.get_spans_dataframe()

        expected_columns = [
            "context.span_id",
            "context.trace_id",
            "parent_id",
            "name",
            "span_kind",
            "start_time",
            "end_time",
            "status_code",
            "status_message",
            "attributes",
            "events",
            "cumulative_error_count",
            "cumulative_llm_token_count_prompt",
            "cumulative_llm_token_count_completion",
            "llm_token_count_prompt",
            "llm_token_count_completion",
        ]

        for col in expected_columns:
            assert col in df.columns

    def test_fetch_with_start_time_filter(self, temp_db):
        """Given start_time filter, returns only matching spans."""
        client = PhoenixSQLiteClient(temp_db)
        start_time = datetime(2025, 1, 1, 10, 30)

        df = client.get_spans_dataframe(start_time=start_time)

        assert len(df) == 1
        assert df.iloc[0]["context.span_id"] == "span-003"

    def test_fetch_with_end_time_filter(self, temp_db):
        """Given end_time filter, returns only matching spans."""
        client = PhoenixSQLiteClient(temp_db)
        end_time = datetime(2025, 1, 1, 10, 30)

        df = client.get_spans_dataframe(end_time=end_time)

        assert len(df) == 2
        assert "span-003" not in df["context.span_id"].values

    def test_fetch_with_both_time_filters(self, temp_db):
        """Given start and end time filters, returns matching range."""
        client = PhoenixSQLiteClient(temp_db)
        start_time = datetime(2025, 1, 1, 10, 0)
        end_time = datetime(2025, 1, 1, 10, 30)

        df = client.get_spans_dataframe(start_time=start_time, end_time=end_time)

        assert len(df) == 2
        assert "span-003" not in df["context.span_id"].values

    def test_fetch_with_limit(self, temp_db):
        """Given limit, returns only that many rows."""
        client = PhoenixSQLiteClient(temp_db)
        df = client.get_spans_dataframe(limit=2)

        assert len(df) == 2

    def test_fetch_with_offset(self, temp_db):
        """Given offset, skips that many rows."""
        client = PhoenixSQLiteClient(temp_db)
        df = client.get_spans_dataframe(limit=2, offset=1)

        assert len(df) == 2
        assert df.iloc[0]["context.span_id"] == "span-002"

    def test_fetch_empty_result(self, temp_db):
        """Given filters with no matches, returns empty DataFrame."""
        client = PhoenixSQLiteClient(temp_db)
        start_time = datetime(2025, 1, 2)

        df = client.get_spans_dataframe(start_time=start_time)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_fetch_attributes_parsed_as_dict(self, temp_db):
        """Given JSON attributes, parses them as dict."""
        client = PhoenixSQLiteClient(temp_db)
        df = client.get_spans_dataframe(limit=1)

        attributes = df.iloc[0]["attributes"]
        assert isinstance(attributes, dict)
        assert "model" in attributes

    def test_fetch_timestamps_as_datetime(self, temp_db):
        """Given timestamp strings, converts to datetime."""
        client = PhoenixSQLiteClient(temp_db)
        df = client.get_spans_dataframe(limit=1)

        assert pd.api.types.is_datetime64_any_dtype(df["start_time"])
        assert pd.api.types.is_datetime64_any_dtype(df["end_time"])

    def test_fetch_nonexistent_project(self, temp_db):
        """Given nonexistent project, returns empty DataFrame."""
        client = PhoenixSQLiteClient(temp_db, project="nonexistent")
        df = client.get_spans_dataframe()

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0


class TestPhoenixSQLiteClientFetchAnnotations:
    """Tests for annotation fetching."""

    def test_fetch_annotations_with_spans_df(self, temp_db):
        """Given spans DataFrame, fetches annotations."""
        client = PhoenixSQLiteClient(temp_db)
        spans_df = client.get_spans_dataframe()

        annotations_df = client.get_span_annotations_dataframe(
            spans_dataframe=spans_df
        )

        assert isinstance(annotations_df, pd.DataFrame)
        assert len(annotations_df) == 2
        assert "span_id" in annotations_df.columns
        assert "name" in annotations_df.columns
        assert "score" in annotations_df.columns

    def test_fetch_annotations_with_span_ids(self, temp_db):
        """Given list of span IDs, fetches annotations."""
        client = PhoenixSQLiteClient(temp_db)

        annotations_df = client.get_span_annotations_dataframe(
            span_ids=["span-001", "span-003"]
        )

        assert len(annotations_df) == 2
        assert set(annotations_df["span_id"]) == {"span-001", "span-003"}

    def test_fetch_annotations_single_span(self, temp_db):
        """Given single span ID, fetches its annotations."""
        client = PhoenixSQLiteClient(temp_db)

        annotations_df = client.get_span_annotations_dataframe(
            span_ids=["span-001"]
        )

        assert len(annotations_df) == 1
        assert annotations_df.iloc[0]["span_id"] == "span-001"
        assert annotations_df.iloc[0]["name"] == "quality"

    def test_fetch_annotations_no_matches(self, temp_db):
        """Given span ID with no annotations, returns empty DataFrame."""
        client = PhoenixSQLiteClient(temp_db)

        annotations_df = client.get_span_annotations_dataframe(
            span_ids=["span-002"]
        )

        assert isinstance(annotations_df, pd.DataFrame)
        assert len(annotations_df) == 0

    def test_fetch_annotations_requires_input(self, temp_db):
        """Given neither spans_dataframe nor span_ids, raises ValueError."""
        client = PhoenixSQLiteClient(temp_db)

        with pytest.raises(ValueError, match="Either spans_dataframe or span_ids"):
            client.get_span_annotations_dataframe()

    def test_fetch_annotations_metadata_parsed(self, temp_db):
        """Given JSON metadata, parses as dict."""
        client = PhoenixSQLiteClient(temp_db)

        annotations_df = client.get_span_annotations_dataframe(
            span_ids=["span-001"]
        )

        metadata = annotations_df.iloc[0]["metadata"]
        assert isinstance(metadata, dict)

    def test_fetch_annotations_timestamps_as_datetime(self, temp_db):
        """Given timestamp strings, converts to datetime."""
        client = PhoenixSQLiteClient(temp_db)

        annotations_df = client.get_span_annotations_dataframe(
            span_ids=["span-001"]
        )

        assert pd.api.types.is_datetime64_any_dtype(annotations_df["created_at"])
        assert pd.api.types.is_datetime64_any_dtype(annotations_df["updated_at"])


class TestPhoenixSQLiteClientUtilityMethods:
    """Tests for utility methods."""

    def test_get_total_span_count(self, temp_db):
        """Given project, returns total span count."""
        client = PhoenixSQLiteClient(temp_db)
        count = client.get_total_span_count()

        assert count == 3

    def test_get_total_span_count_with_filters(self, temp_db):
        """Given time filters, returns filtered count."""
        client = PhoenixSQLiteClient(temp_db)
        start_time = datetime(2025, 1, 1, 10, 30)
        count = client.get_total_span_count(start_time=start_time)

        assert count == 1

    def test_get_total_span_count_empty(self, temp_db):
        """Given nonexistent project, returns 0."""
        client = PhoenixSQLiteClient(temp_db, project="nonexistent")
        count = client.get_total_span_count()

        assert count == 0

    def test_get_time_range(self, temp_db):
        """Given project with spans, returns time range."""
        client = PhoenixSQLiteClient(temp_db)
        min_time, max_time = client.get_time_range()

        assert isinstance(min_time, datetime)
        assert isinstance(max_time, datetime)
        assert min_time < max_time
        assert min_time.year == 2025
        assert max_time.year == 2025

    def test_get_time_range_no_spans(self, temp_db):
        """Given project with no spans, raises ValueError."""
        client = PhoenixSQLiteClient(temp_db, project="nonexistent")

        with pytest.raises(ValueError, match="No spans found"):
            client.get_time_range()


class TestPhoenixSQLiteClientDockerMode:
    """Tests for Docker execution mode."""

    def test_docker_mode_execute_query(self):
        """Given Docker mode, executes query via docker exec."""
        client = PhoenixSQLiteClient(
            "docker://test-container:/root/phoenix.db",
            project="test-project"
        )

        mock_stdout = '[{"count": 42}]'

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_stdout,
                stderr=""
            )

            rows = client._execute_query("SELECT COUNT(*) as count FROM projects")

            assert rows == [{"count": 42}]
            mock_run.assert_called_once()

    def test_docker_mode_execution_failure(self):
        """Given Docker exec failure, raises PhoenixSQLiteQueryError."""
        client = PhoenixSQLiteClient(
            "docker://test-container:/root/phoenix.db"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Container not found"
            )

            with pytest.raises(PhoenixSQLiteQueryError, match="Docker exec failed"):
                client._execute_query("SELECT 1")

    def test_docker_mode_timeout(self):
        """Given Docker exec timeout, raises PhoenixSQLiteQueryError."""
        client = PhoenixSQLiteClient(
            "docker://test-container:/root/phoenix.db"
        )

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 300)):
            with pytest.raises(PhoenixSQLiteQueryError, match="timed out"):
                client._execute_query("SELECT 1")

    def test_docker_mode_invalid_json_response(self):
        """Given invalid JSON from Docker, raises PhoenixSQLiteQueryError."""
        client = PhoenixSQLiteClient(
            "docker://test-container:/root/phoenix.db"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not valid json",
                stderr=""
            )

            with pytest.raises(PhoenixSQLiteQueryError, match="Failed to parse"):
                client._execute_query("SELECT 1")


class TestPhoenixSQLiteClientErrorHandling:
    """Tests for error handling."""

    def test_query_error_invalid_sql(self, temp_db):
        """Given invalid SQL, raises PhoenixSQLiteQueryError."""
        client = PhoenixSQLiteClient(temp_db)

        with pytest.raises(PhoenixSQLiteQueryError):
            client._execute_query("SELECT * FROM nonexistent_table")

    def test_docker_mode_get_connection_raises_error(self):
        """Given Docker mode, _get_connection() raises error."""
        client = PhoenixSQLiteClient(
            "docker://test-container:/root/phoenix.db"
        )

        with pytest.raises(PhoenixSQLiteConnectionError, match="Docker mode"):
            client._get_connection()


class TestPhoenixSQLiteClientRepr:
    """Tests for string representation."""

    def test_repr_local(self, temp_db):
        """Given local client, repr shows path and project."""
        client = PhoenixSQLiteClient(temp_db, project="test-project")
        result = repr(client)

        assert temp_db in result
        assert "test-project" in result

    def test_repr_docker(self):
        """Given Docker client, repr shows Docker path and project."""
        client = PhoenixSQLiteClient(
            "docker://container:/path/db", project="test-project"
        )
        result = repr(client)

        assert "docker://container:/path/db" in result
        assert "test-project" in result
