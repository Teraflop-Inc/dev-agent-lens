"""
Tests for Events Parquet Export.

These tests verify the events Parquet export functionality including:
- Schema validation
- Event extraction from Claude sessions
- Order index preservation
- Tool event merging
- Metadata preservation
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Check for pyarrow availability
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    import duckdb

    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False
    pa = None
    pq = None
    duckdb = None

from dev_agent_lens.export.parquet_events import (
    extract_events_from_session,
    export_claude_to_events_parquet,
    get_events_schema,
    ExportResult,
)


@pytest.fixture
def fixture_dir():
    """Path to test fixtures."""
    return Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def minimal_session(fixture_dir):
    """Path to minimal session fixture."""
    return fixture_dir / "claude_session_minimal.jsonl"


@pytest.fixture
def metadata_session(fixture_dir):
    """Path to session with metadata fixture."""
    return fixture_dir / "claude_session_with_metadata.jsonl"


@pytest.fixture
def subagent_session(fixture_dir):
    """Path to session with subagent fixture."""
    return fixture_dir / "claude_session_with_subagent.jsonl"


@pytest.fixture
def compaction_session(fixture_dir):
    """Path to session with compaction fixture."""
    return fixture_dir / "claude_session_with_compaction.jsonl"


class TestExtractEventsFromSession:
    """Tests for event extraction."""

    def test_extracts_user_events(self, minimal_session):
        """Given session, extracts user message events."""
        events = extract_events_from_session(minimal_session)

        user_events = [e for e in events if e["event_type"] == "user"]
        assert len(user_events) == 2

    def test_extracts_assistant_events(self, minimal_session):
        """Given session, extracts assistant message events."""
        events = extract_events_from_session(minimal_session)

        assistant_events = [e for e in events if e["event_type"] == "assistant"]
        assert len(assistant_events) == 1

    def test_extracts_tool_events(self, minimal_session):
        """Given session with tool use, extracts tool events."""
        events = extract_events_from_session(minimal_session)

        tool_events = [e for e in events if e["event_type"] == "tool"]
        assert len(tool_events) == 1
        assert tool_events[0]["tool_name"] == "Read"

    def test_tool_events_have_input_and_result(self, minimal_session):
        """Tool events have both input and result."""
        events = extract_events_from_session(minimal_session)

        tool_events = [e for e in events if e["event_type"] == "tool"]
        assert len(tool_events) == 1

        tool_event = tool_events[0]
        assert "main.py" in tool_event["tool_input"]
        assert "def main()" in tool_event["tool_result"]

    def test_order_index_sequential(self, minimal_session):
        """Order index increases sequentially."""
        events = extract_events_from_session(minimal_session)

        order_indices = [e["order_idx"] for e in events]
        # Should be sequential starting from 0
        assert order_indices == list(range(len(events)))

    def test_preserves_conversation_order(self, minimal_session):
        """Events preserve conversation flow order."""
        events = extract_events_from_session(minimal_session)

        # Expected order: user -> tool -> assistant -> user
        event_types = [e["event_type"] for e in events]
        assert event_types[0] == "user"  # "Read the file"
        assert event_types[1] == "tool"  # Read tool call
        assert event_types[2] == "assistant"  # "The file contains..."
        assert event_types[3] == "user"  # "Thanks"

    def test_session_id_from_filename(self, minimal_session):
        """Session ID defaults to filename stem."""
        events = extract_events_from_session(minimal_session)

        for event in events:
            assert event["session_id"] == "claude_session_minimal"

    def test_session_id_override(self, minimal_session):
        """Can override session ID."""
        events = extract_events_from_session(minimal_session, session_id="custom-id")

        for event in events:
            assert event["session_id"] == "custom-id"

    def test_extracts_project_path(self, minimal_session):
        """Extracts project_path from session."""
        events = extract_events_from_session(minimal_session)

        # All events should have the same project_path
        for event in events:
            assert event["project_path"] == "/project"

    def test_extracts_git_branch(self, minimal_session):
        """Extracts git_branch from session."""
        events = extract_events_from_session(minimal_session)

        for event in events:
            assert event["git_branch"] == "main"

    def test_events_have_timestamps(self, minimal_session):
        """Events have timestamps."""
        events = extract_events_from_session(minimal_session)

        for event in events:
            assert event["timestamp"] is not None

    def test_events_have_event_id(self, minimal_session):
        """Events have unique event IDs."""
        events = extract_events_from_session(minimal_session)

        event_ids = [e["event_id"] for e in events]
        assert len(set(event_ids)) == len(event_ids)  # All unique

    def test_includes_raw_message_json(self, minimal_session):
        """Events include raw message JSON for debugging."""
        events = extract_events_from_session(minimal_session)

        for event in events:
            assert event["raw_message_json"] is not None


class TestExtractSubagentEvents:
    """Tests for subagent event extraction."""

    def test_extracts_subagent_events(self, subagent_session):
        """Given session with Task tool, extracts subagent events."""
        events = extract_events_from_session(subagent_session)

        subagent_events = [e for e in events if e["event_type"] == "subagent"]
        assert len(subagent_events) == 1

    def test_subagent_has_type(self, subagent_session):
        """Subagent events have subagent_type."""
        events = extract_events_from_session(subagent_session)

        subagent_events = [e for e in events if e["event_type"] == "subagent"]
        assert subagent_events[0]["subagent_type"] == "Explore"

    def test_subagent_has_response(self, subagent_session):
        """Subagent events have response text."""
        events = extract_events_from_session(subagent_session)

        subagent_events = [e for e in events if e["event_type"] == "subagent"]
        assert "Python files" in subagent_events[0]["text"]


class TestExtractCompactionEvents:
    """Tests for compaction event extraction."""

    def test_extracts_compaction_events(self, compaction_session):
        """Given session with compaction, extracts compaction events."""
        events = extract_events_from_session(compaction_session)

        compaction_events = [e for e in events if e["event_type"] == "compaction"]
        assert len(compaction_events) == 1

    def test_compaction_has_metadata(self, compaction_session):
        """Compaction events have metadata in tool_input."""
        events = extract_events_from_session(compaction_session)

        compaction_events = [e for e in events if e["event_type"] == "compaction"]
        assert "context_length" in compaction_events[0]["tool_input"]

    def test_compaction_has_summary_text(self, compaction_session):
        """Compaction events have summary text."""
        events = extract_events_from_session(compaction_session)

        compaction_events = [e for e in events if e["event_type"] == "compaction"]
        assert "Compaction Summary" in compaction_events[0]["text"]


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
class TestEventsParquetSchema:
    """Tests for Parquet schema."""

    def test_schema_has_required_columns(self):
        """Schema has all required columns."""
        schema = get_events_schema()

        required = {
            "session_id",
            "event_id",
            "order_idx",
            "event_type",
            "timestamp",
            "tool_name",
            "text",
        }
        schema_names = set(schema.names)
        assert required.issubset(schema_names)

    def test_schema_column_types(self):
        """Schema has correct column types."""
        schema = get_events_schema()

        # Check key column types
        assert schema.field("session_id").type == pa.string()
        assert schema.field("order_idx").type == pa.int32()
        assert schema.field("event_type").type == pa.string()


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
class TestExportToParquet:
    """Tests for Parquet export."""

    def test_export_creates_file(self, minimal_session, tmp_path):
        """Export creates Parquet file."""
        output_path = tmp_path / "events.parquet"

        result = export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        assert output_path.exists()
        assert result.output_path == output_path

    def test_export_result_has_stats(self, minimal_session, tmp_path):
        """Export result includes statistics."""
        output_path = tmp_path / "events.parquet"

        result = export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        assert result.event_count > 0
        assert result.session_count == 1
        assert result.bytes_written > 0
        assert "user" in result.event_type_counts

    def test_export_valid_parquet(self, minimal_session, tmp_path):
        """Exported file is valid Parquet."""
        output_path = tmp_path / "events.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        # Should be readable
        table = pq.read_table(output_path)
        assert len(table) > 0

    def test_export_multiple_sessions(self, minimal_session, metadata_session, tmp_path):
        """Can export multiple sessions to single file."""
        output_path = tmp_path / "events.parquet"

        result = export_claude_to_events_parquet(
            session_files=[minimal_session, metadata_session],
            output_path=output_path,
        )

        assert result.session_count == 2

    def test_export_uses_compression(self, minimal_session, tmp_path):
        """Export uses specified compression."""
        output_zstd = tmp_path / "zstd.parquet"
        output_none = tmp_path / "none.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_zstd,
            compression="zstd",
        )
        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_none,
            compression="none",
        )

        # ZSTD should be smaller
        assert output_zstd.stat().st_size < output_none.stat().st_size


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
class TestDuckDBValidation:
    """Tests validating Parquet with DuckDB."""

    def test_schema_via_duckdb(self, minimal_session, tmp_path):
        """DuckDB can read and describe schema."""
        output_path = tmp_path / "events.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        conn = duckdb.connect()
        schema_df = conn.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{output_path}')"
        ).df()

        required = {"session_id", "order_idx", "event_type", "timestamp", "tool_name"}
        column_names = set(schema_df["column_name"])
        assert required.issubset(column_names)

    def test_event_types_via_duckdb(self, minimal_session, tmp_path):
        """DuckDB query returns expected event types."""
        output_path = tmp_path / "events.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        conn = duckdb.connect()
        result = conn.execute(
            f"""
            SELECT event_type, COUNT(*) as count
            FROM read_parquet('{output_path}')
            GROUP BY event_type
        """
        ).fetchall()

        event_types = [r[0] for r in result]
        assert "user" in event_types
        assert "assistant" in event_types or "tool" in event_types

    def test_order_preserved_via_duckdb(self, minimal_session, tmp_path):
        """DuckDB query shows order is preserved."""
        output_path = tmp_path / "events.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        conn = duckdb.connect()
        # Check that order_idx increases within sessions
        violations = conn.execute(
            f"""
            WITH ordered AS (
                SELECT order_idx, LAG(order_idx) OVER (
                    PARTITION BY session_id ORDER BY order_idx
                ) as prev
                FROM read_parquet('{output_path}')
            )
            SELECT COUNT(*) FROM ordered
            WHERE prev IS NOT NULL AND order_idx <= prev
        """
        ).fetchone()[0]

        assert violations == 0

    def test_tool_events_have_names_via_duckdb(self, minimal_session, tmp_path):
        """Tool events have tool_name populated."""
        output_path = tmp_path / "events.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        conn = duckdb.connect()
        result = conn.execute(
            f"""
            SELECT COUNT(*), SUM(CASE WHEN tool_name IS NULL THEN 1 ELSE 0 END)
            FROM read_parquet('{output_path}')
            WHERE event_type = 'tool'
        """
        ).fetchone()

        total, missing = result
        assert missing == 0, f"{missing}/{total} tool events missing tool_name"


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
class TestConversationAnalytics:
    """Tests demonstrating analytics queries."""

    def test_query_tool_after_user_request(self, minimal_session, tmp_path):
        """Can query what tool was called after user request."""
        output_path = tmp_path / "events.parquet"

        export_claude_to_events_parquet(
            session_file=minimal_session,
            output_path=output_path,
        )

        conn = duckdb.connect()
        result = conn.execute(
            f"""
            WITH ordered_events AS (
                SELECT
                    order_idx,
                    event_type,
                    text,
                    tool_name,
                    LAG(event_type) OVER (ORDER BY order_idx) as prev_type,
                    LAG(text) OVER (ORDER BY order_idx) as prev_text
                FROM read_parquet('{output_path}')
            )
            SELECT tool_name, prev_text
            FROM ordered_events
            WHERE event_type = 'tool'
              AND prev_type = 'user'
              AND prev_text LIKE '%Read%'
        """
        ).fetchone()

        assert result is not None
        assert result[0] == "Read"
