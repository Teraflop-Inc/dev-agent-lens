"""
Tests for the export formats module.

Test Cases from Story 2.5:
1. JSON Output: Valid JSON array
2. CSV Output: Valid CSV with headers
3. Markdown Output: Markdown table format
4. File Output: --output-file path writes to file
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from dev_agent_lens.query.export import (
    export,
    export_csv,
    export_json,
    export_markdown,
)
from dev_agent_lens.query.query import QueryResult


@pytest.fixture
def sample_result():
    """Sample QueryResult for testing."""
    return QueryResult(
        sessions=[
            {
                "session_id": "session1",
                "span_count": 2,
                "start_time": "2024-01-01T10:00:00",
                "end_time": "2024-01-01T10:30:00",
                "spans": [
                    {
                        "span_id": "span1",
                        "name": "First span",
                        "status_code": "OK",
                        "start_time": "2024-01-01T10:00:00",
                        "input_value": "Hello",
                    },
                    {
                        "span_id": "span2",
                        "name": "Second span",
                        "status_code": "ERROR",
                        "start_time": "2024-01-01T10:15:00",
                        "input_value": "World",
                    },
                ],
            },
            {
                "session_id": "session2",
                "span_count": 1,
                "start_time": "2024-01-01T11:00:00",
                "end_time": "2024-01-01T11:30:00",
                "spans": [
                    {
                        "span_id": "span3",
                        "name": "Third span",
                        "status_code": "OK",
                        "start_time": "2024-01-01T11:00:00",
                        "input_value": "Test",
                    },
                ],
            },
        ],
        total_spans=3,
        total_sessions=2,
        query_params={"pattern": "test"},
    )


@pytest.fixture
def empty_result():
    """Empty QueryResult for testing."""
    return QueryResult(sessions=[], total_spans=0, total_sessions=0)


class TestJSONExport:
    """Test Case 1: JSON output."""

    def test_exports_valid_json(self, sample_result):
        """export_json returns valid JSON."""
        json_str = export_json(sample_result)

        # Should parse without error
        data = json.loads(json_str)

        assert "sessions" in data
        assert "total_spans" in data
        assert data["total_spans"] == 3
        assert data["total_sessions"] == 2

    def test_json_contains_all_sessions(self, sample_result):
        """JSON output contains all sessions."""
        json_str = export_json(sample_result)
        data = json.loads(json_str)

        assert len(data["sessions"]) == 2
        session_ids = [s["session_id"] for s in data["sessions"]]
        assert "session1" in session_ids
        assert "session2" in session_ids

    def test_json_contains_spans(self, sample_result):
        """JSON output contains spans within sessions."""
        json_str = export_json(sample_result)
        data = json.loads(json_str)

        session1 = next(s for s in data["sessions"] if s["session_id"] == "session1")
        assert len(session1["spans"]) == 2

    def test_json_custom_indent(self, sample_result):
        """JSON output respects custom indent."""
        json_str = export_json(sample_result, indent=4)

        # Should have 4-space indentation
        assert "    " in json_str

    def test_json_empty_result(self, empty_result):
        """JSON handles empty result."""
        json_str = export_json(empty_result)
        data = json.loads(json_str)

        assert data["total_spans"] == 0
        assert data["sessions"] == []


class TestCSVExport:
    """Test Case 2: CSV output."""

    def test_exports_valid_csv(self, sample_result):
        """export_csv returns valid CSV with headers."""
        csv_str = export_csv(sample_result)

        # Parse CSV
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)

        assert len(rows) == 3  # 3 spans total

    def test_csv_has_headers(self, sample_result):
        """CSV output has headers."""
        csv_str = export_csv(sample_result)

        lines = csv_str.strip().split("\n")
        header = lines[0]

        assert "session_id" in header
        assert "span_id" in header
        assert "name" in header

    def test_csv_includes_session_id(self, sample_result):
        """CSV includes session_id column by default."""
        csv_str = export_csv(sample_result)

        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)

        assert all("session_id" in row for row in rows)
        session_ids = [row["session_id"] for row in rows]
        assert session_ids.count("session1") == 2
        assert session_ids.count("session2") == 1

    def test_csv_without_session_id(self, sample_result):
        """CSV can exclude session_id column."""
        csv_str = export_csv(sample_result, include_session_id=False)

        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)

        # session_id should not be in the data
        # (Note: it might still be in span data if span has it)
        header_line = csv_str.split("\n")[0]
        # The first column should NOT be session_id
        assert not header_line.startswith("session_id")

    def test_csv_empty_result(self, empty_result):
        """CSV handles empty result."""
        csv_str = export_csv(empty_result)

        assert csv_str == ""


class TestMarkdownExport:
    """Test Case 3: Markdown output."""

    def test_exports_markdown_table(self, sample_result):
        """export_markdown returns Markdown table format."""
        md_str = export_markdown(sample_result)

        # Should have table structure
        lines = md_str.strip().split("\n")
        assert any("|" in line for line in lines)

        # Should have header separator
        assert any("---" in line for line in lines)

    def test_markdown_has_header(self, sample_result):
        """Markdown table has header row."""
        md_str = export_markdown(sample_result)

        lines = md_str.strip().split("\n")
        header = lines[0]

        assert "session_id" in header
        assert "span_id" in header

    def test_markdown_custom_columns(self, sample_result):
        """Markdown respects custom column selection."""
        md_str = export_markdown(sample_result, columns=["span_id", "name"])

        lines = md_str.strip().split("\n")
        header = lines[0]

        assert "span_id" in header
        assert "name" in header
        assert "status_code" not in header

    def test_markdown_truncates_long_values(self, sample_result):
        """Markdown truncates long cell values."""
        # Add a span with a very long value
        sample_result.sessions[0]["spans"][0]["name"] = "A" * 100

        md_str = export_markdown(sample_result, max_width=20)

        # Should be truncated with ...
        assert "..." in md_str
        assert "A" * 100 not in md_str

    def test_markdown_includes_summary(self, sample_result):
        """Markdown includes summary line."""
        md_str = export_markdown(sample_result)

        assert "3 spans" in md_str
        assert "2 sessions" in md_str

    def test_markdown_empty_result(self, empty_result):
        """Markdown handles empty result."""
        md_str = export_markdown(empty_result)

        assert "No results found" in md_str


class TestFileOutput:
    """Test Case 4: File output."""

    def test_json_writes_to_file(self, sample_result, tmp_path):
        """JSON can write to file."""
        output_file = tmp_path / "output.json"

        export_json(sample_result, output_file=output_file)

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["total_spans"] == 3

    def test_csv_writes_to_file(self, sample_result, tmp_path):
        """CSV can write to file."""
        output_file = tmp_path / "output.csv"

        export_csv(sample_result, output_file=output_file)

        assert output_file.exists()
        content = output_file.read_text()
        assert "span_id" in content

    def test_markdown_writes_to_file(self, sample_result, tmp_path):
        """Markdown can write to file."""
        output_file = tmp_path / "output.md"

        export_markdown(sample_result, output_file=output_file)

        assert output_file.exists()
        content = output_file.read_text()
        assert "|" in content


class TestExportFunction:
    """Tests for unified export function."""

    def test_export_json_format(self, sample_result):
        """export() with format='json' uses JSON exporter."""
        output = export(sample_result, format="json")

        data = json.loads(output)
        assert data["total_spans"] == 3

    def test_export_csv_format(self, sample_result):
        """export() with format='csv' uses CSV exporter."""
        output = export(sample_result, format="csv")

        reader = csv.DictReader(io.StringIO(output))
        rows = list(reader)
        assert len(rows) == 3

    def test_export_markdown_format(self, sample_result):
        """export() with format='markdown' uses Markdown exporter."""
        output = export(sample_result, format="markdown")

        assert "|" in output
        assert "---" in output

    def test_export_invalid_format_raises_error(self, sample_result):
        """export() with invalid format raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            export(sample_result, format="invalid")

        assert "invalid" in str(exc_info.value).lower()
        assert "json" in str(exc_info.value)  # Lists valid formats

    def test_export_passes_kwargs(self, sample_result):
        """export() passes kwargs to format-specific exporter."""
        output = export(sample_result, format="json", indent=4)

        # Should have 4-space indentation
        assert "    " in output

    def test_export_to_file(self, sample_result, tmp_path):
        """export() can write to file."""
        output_file = tmp_path / "output.json"

        export(sample_result, format="json", output_file=output_file)

        assert output_file.exists()


class TestComplexData:
    """Tests for handling complex data types."""

    def test_json_handles_nested_dicts(self):
        """JSON export handles nested dictionaries."""
        result = QueryResult(
            sessions=[{
                "session_id": "s1",
                "spans": [{
                    "span_id": "1",
                    "raw_attributes": {"nested": {"key": "value"}},
                }],
                "span_count": 1,
            }],
            total_spans=1,
            total_sessions=1,
        )

        json_str = export_json(result)
        data = json.loads(json_str)

        assert data["sessions"][0]["spans"][0]["raw_attributes"]["nested"]["key"] == "value"

    def test_csv_serializes_dicts_as_json(self):
        """CSV export serializes dicts as JSON strings."""
        result = QueryResult(
            sessions=[{
                "session_id": "s1",
                "spans": [{
                    "span_id": "1",
                    "raw_attributes": {"key": "value"},
                }],
                "span_count": 1,
            }],
            total_spans=1,
            total_sessions=1,
        )

        csv_str = export_csv(result)

        reader = csv.DictReader(io.StringIO(csv_str))
        row = next(reader)

        # raw_attributes should be JSON string
        assert '{"key": "value"}' in row.get("raw_attributes", "")

    def test_markdown_handles_none_values(self):
        """Markdown export handles None values gracefully."""
        result = QueryResult(
            sessions=[{
                "session_id": "s1",
                "spans": [{
                    "span_id": "1",
                    "name": None,
                    "status_code": None,
                }],
                "span_count": 1,
            }],
            total_spans=1,
            total_sessions=1,
        )

        md_str = export_markdown(result)

        # Should not raise and should produce valid markdown
        assert "|" in md_str
