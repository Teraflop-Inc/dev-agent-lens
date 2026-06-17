"""
Tests for Thread Unification Module.

These tests verify session merging functionality including:
- New session detection
- Session continuation matching
- Span deduplication
- Temporal ordering
- Match report generation
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd

from dev_agent_lens.core.unify import (
    MatchReport,
    _extract_user_attribution,
    get_session_spans,
    list_sessions,
    read_sessions_file,
    unify_sessions,
    write_sessions_file,
)

LITELLM_USER_STRING = (
    "user_abc123def_account_11111111-1111-1111-1111-111111111111"
    "_session_22222222-2222-2222-2222-222222222222"
)


class TestUserAttributionPropagation:
    """Tests for propagating user_id/account_id across a trace."""

    def test_llm_span_gets_user_attribution(self):
        """Given an LLM span with proxy metadata, extracts user_id/account_id."""
        df = pd.DataFrame(
            [
                {
                    "span_id": "llm1",
                    "trace_id": "t1",
                    "metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING},
                }
            ]
        )
        result = _extract_user_attribution(df)
        assert result.iloc[0]["user_id"] == "abc123def"
        assert result.iloc[0]["account_id"] == (
            "11111111-1111-1111-1111-111111111111"
        )

    def test_tool_span_inherits_user_from_sibling_llm_span(self):
        """Given a tool span sharing a trace with an LLM span, it inherits user_id."""
        df = pd.DataFrame(
            [
                {
                    "span_id": "llm1",
                    "trace_id": "t1",
                    "metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING},
                },
                {"span_id": "tool1", "trace_id": "t1", "metadata": None},
            ]
        )
        result = _extract_user_attribution(df)
        tool_row = result[result["span_id"] == "tool1"].iloc[0]
        assert tool_row["user_id"] == "abc123def"
        assert tool_row["account_id"] == "11111111-1111-1111-1111-111111111111"

    def test_unattributable_spans_remain_none(self):
        """Given spans with no proxy metadata anywhere, attribution is None."""
        df = pd.DataFrame(
            [{"span_id": "tool1", "trace_id": "t1", "metadata": None}]
        )
        result = _extract_user_attribution(df)
        assert result.iloc[0]["user_id"] is None
        assert result.iloc[0]["account_id"] is None

    def test_empty_dataframe(self):
        """Given an empty DataFrame, returns it with attribution columns added."""
        result = _extract_user_attribution(pd.DataFrame())
        assert "user_id" in result.columns
        assert "account_id" in result.columns


class TestUnifyNewSessions:
    """Tests for adding new sessions."""

    def test_all_new_sessions_added(self, tmp_path):
        """Given all new session IDs, adds all as new sessions."""
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc123"}},
            {"span_id": "span2", "metadata": {"user_id": "test_session_def456"}},
        ]

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        assert len(report.new_sessions) == 2
        assert "abc123" in report.new_sessions
        assert "def456" in report.new_sessions
        assert len(report.continued_sessions) == 0

    def test_empty_existing_file_treats_all_as_new(self, tmp_path):
        """Given no existing file, treats all spans as new."""
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc123"}},
        ]
        nonexistent = tmp_path / "nonexistent.jsonl"

        unified, report = unify_sessions(new_spans, nonexistent, state_path=tmp_path)

        assert len(report.new_sessions) == 1
        assert report.total_spans_after == 1


class TestUnifyContinuation:
    """Tests for session continuation detection."""

    def test_detects_session_continuation(self, tmp_path):
        """Given spans for existing session, merges into that session."""
        # Create existing file
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc123"}},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        # Add new spans with same session
        new_spans = [
            {"span_id": "span2", "metadata": {"user_id": "test_session_abc123"}},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        assert "abc123" in report.continued_sessions
        assert len(report.new_sessions) == 0
        assert report.total_spans_after == 2

    def test_mixed_new_and_continued(self, tmp_path):
        """Given mix of new and existing sessions, classifies correctly."""
        # Create existing file
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_existing"}},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        # Add new spans with mixed sessions
        new_spans = [
            {"span_id": "span2", "metadata": {"user_id": "test_session_existing"}},
            {"span_id": "span3", "metadata": {"user_id": "test_session_new"}},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        assert "existing" in report.continued_sessions
        assert "new" in report.new_sessions
        assert report.total_spans_after == 3


class TestUnifyDeduplication:
    """Tests for span deduplication."""

    def test_removes_duplicate_span_ids(self, tmp_path):
        """Given duplicate span_ids, keeps only one copy."""
        # Create existing file
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}, "version": "old"},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        # Add new span with same ID
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}, "version": "new"},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        assert report.duplicates_removed == 1
        assert len(unified) == 1
        # Should keep the newer version
        assert unified.iloc[0]["version"] == "new"

    def test_keeps_unique_spans(self, tmp_path):
        """Given all unique span_ids, keeps all spans."""
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        new_spans = [
            {"span_id": "span2", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span3", "metadata": {"user_id": "test_session_abc"}},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        assert report.duplicates_removed == 0
        assert len(unified) == 3


class TestUnifyOrdering:
    """Tests for temporal ordering."""

    def test_spans_ordered_by_start_time(self, tmp_path):
        """Given spans with timestamps, ordered by start_time."""
        base_time = datetime(2025, 1, 1, 12, 0, 0)

        new_spans = [
            {
                "span_id": "span3",
                "metadata": {"user_id": "test_session_abc"},
                "start_time": (base_time + timedelta(hours=2)).isoformat(),
            },
            {
                "span_id": "span1",
                "metadata": {"user_id": "test_session_abc"},
                "start_time": base_time.isoformat(),
            },
            {
                "span_id": "span2",
                "metadata": {"user_id": "test_session_abc"},
                "start_time": (base_time + timedelta(hours=1)).isoformat(),
            },
        ]

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        # Should be sorted by start_time
        span_ids = unified["span_id"].tolist()
        assert span_ids == ["span1", "span2", "span3"]

    def test_handles_missing_timestamps(self, tmp_path):
        """Given some spans without timestamps, handles gracefully."""
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
            {
                "span_id": "span2",
                "metadata": {"user_id": "test_session_abc"},
                "start_time": "2025-01-01T12:00:00",
            },
        ]

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        # Should not crash
        assert len(unified) == 2


class TestUnifyNoDataLoss:
    """Tests for data integrity."""

    def test_all_unique_spans_preserved(self, tmp_path):
        """After unification, all unique spans are present."""
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_a"}},
            {"span_id": "span2", "metadata": {"user_id": "test_session_a"}},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        new_spans = [
            {"span_id": "span3", "metadata": {"user_id": "test_session_a"}},
            {"span_id": "span4", "metadata": {"user_id": "test_session_b"}},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        all_span_ids = set(unified["span_id"].tolist())
        assert all_span_ids == {"span1", "span2", "span3", "span4"}

    def test_preserves_all_columns(self, tmp_path):
        """All columns from both sources are preserved."""
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_a"}, "extra_col": "value1"},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        new_spans = [
            {"span_id": "span2", "metadata": {"user_id": "test_session_a"}, "new_col": "value2"},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        assert "extra_col" in unified.columns
        assert "new_col" in unified.columns


class TestMatchReport:
    """Tests for match report generation."""

    def test_report_shows_new_vs_continued(self, tmp_path):
        """Match report shows which sessions were continued vs new."""
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_existing"}},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        new_spans = [
            {"span_id": "span2", "metadata": {"user_id": "test_session_existing"}},
            {"span_id": "span3", "metadata": {"user_id": "test_session_new"}},
        ]

        unified, report = unify_sessions(new_spans, existing_file, state_path=tmp_path)

        assert "existing" in report.continued_sessions
        assert "new" in report.new_sessions
        assert report.total_spans_before == 3
        assert report.total_spans_after == 3
        assert report.spans_added == 2

    def test_report_saved_to_file(self, tmp_path):
        """Match report is saved to state directory."""
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ]

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        report_file = tmp_path / "match_report.json"
        assert report_file.exists()

        with open(report_file) as f:
            saved_report = json.load(f)

        assert "new_sessions" in saved_report
        assert "continued_sessions" in saved_report
        assert "timestamp" in saved_report

    def test_report_to_dict(self):
        """Report converts to dictionary correctly."""
        report = MatchReport(
            timestamp="2025-01-01T12:00:00",
            new_sessions=["session1"],
            continued_sessions=["session2"],
            total_spans_before=10,
            total_spans_after=8,
            duplicates_removed=2,
            spans_added=5,
        )

        d = report.to_dict()

        assert d["new_sessions"] == ["session1"]
        assert d["continued_sessions"] == ["session2"]
        assert d["summary"]["new_session_count"] == 1
        assert d["summary"]["continued_session_count"] == 1


class TestFileIO:
    """Tests for file read/write operations."""

    def test_write_and_read_sessions_file(self, tmp_path):
        """Can write and read JSONL file."""
        output_file = tmp_path / "test.jsonl"
        df = pd.DataFrame([
            {"span_id": "span1", "name": "test1"},
            {"span_id": "span2", "name": "test2"},
        ])

        write_sessions_file(df, output_file)
        result = read_sessions_file(output_file)

        assert len(result) == 2
        assert "span1" in result["span_id"].values

    def test_read_nonexistent_file(self, tmp_path):
        """Reading nonexistent file returns empty DataFrame."""
        result = read_sessions_file(tmp_path / "nonexistent.jsonl")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_read_empty_file(self, tmp_path):
        """Reading empty file returns empty DataFrame."""
        empty_file = tmp_path / "empty.jsonl"
        empty_file.touch()

        result = read_sessions_file(empty_file)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_unify_writes_output_file(self, tmp_path):
        """Unify writes to output file when specified."""
        output_file = tmp_path / "output.jsonl"
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ]

        unified, report = unify_sessions(
            new_spans, output_file=output_file, state_path=tmp_path
        )

        assert output_file.exists()
        result = read_sessions_file(output_file)
        assert len(result) == 1


class TestGetSessionSpans:
    """Tests for get_session_spans function."""

    def test_gets_spans_for_session(self, tmp_path):
        """Returns only spans for specified session."""
        df = pd.DataFrame([
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span2", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span3", "metadata": {"user_id": "test_session_def"}},
        ])

        result = get_session_spans(df, "abc")

        assert len(result) == 2
        assert all(result["span_id"].isin(["span1", "span2"]))

    def test_returns_empty_for_unknown_session(self, tmp_path):
        """Returns empty DataFrame for unknown session."""
        df = pd.DataFrame([
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ])

        result = get_session_spans(df, "unknown")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0


class TestListSessions:
    """Tests for list_sessions function."""

    def test_lists_all_sessions(self):
        """Returns summary for all sessions."""
        df = pd.DataFrame([
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span2", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span3", "metadata": {"user_id": "test_session_def"}},
        ])

        result = list_sessions(df)

        assert len(result) == 2
        session_ids = [s["session_id"] for s in result]
        assert "abc" in session_ids
        assert "def" in session_ids

    def test_includes_span_count(self):
        """Each session summary includes span count."""
        df = pd.DataFrame([
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span2", "metadata": {"user_id": "test_session_abc"}},
            {"span_id": "span3", "metadata": {"user_id": "test_session_abc"}},
        ])

        result = list_sessions(df)

        abc_session = next(s for s in result if s["session_id"] == "abc")
        assert abc_session["span_count"] == 3

    def test_returns_empty_for_empty_df(self):
        """Returns empty list for empty DataFrame."""
        df = pd.DataFrame()

        result = list_sessions(df)

        assert result == []


class TestEdgeCases:
    """Tests for edge cases."""

    def test_handles_empty_new_spans(self, tmp_path):
        """Handles empty new spans list."""
        existing_file = tmp_path / "existing.jsonl"
        existing_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ]
        write_sessions_file(pd.DataFrame(existing_spans), existing_file)

        unified, report = unify_sessions([], existing_file, state_path=tmp_path)

        assert len(unified) == 1
        assert report.spans_added == 0

    def test_handles_spans_without_session_id(self, tmp_path):
        """Handles spans without session ID in metadata."""
        new_spans = [
            {"span_id": "span1", "name": "no_session"},
            {"span_id": "span2", "metadata": {"user_id": "test_session_abc"}},
        ]

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        assert len(unified) == 2
        assert len(report.new_sessions) == 1  # Only abc has session ID

    def test_handles_list_of_dicts(self, tmp_path):
        """Accepts list of dicts as input."""
        new_spans = [
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ]

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        assert len(unified) == 1

    def test_handles_dataframe_input(self, tmp_path):
        """Accepts DataFrame as input."""
        new_spans = pd.DataFrame([
            {"span_id": "span1", "metadata": {"user_id": "test_session_abc"}},
        ])

        unified, report = unify_sessions(new_spans, state_path=tmp_path)

        assert len(unified) == 1
