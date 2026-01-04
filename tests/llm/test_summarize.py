"""
Tests for the summarize module (Story 4.3).

Test Cases:
1. Summary generation structure
2. Preview functionality
3. Session metrics extraction
4. Empty session handling
"""

from __future__ import annotations

import pytest

from dev_agent_lens.llm.summarize import (
    SessionSummary,
    get_summary_preview,
)


class TestSessionSummary:
    """Test Case 1: Summary generation structure."""

    def test_summary_to_dict(self):
        """Summary converts to dictionary."""
        summary = SessionSummary(
            session_id="test_session",
            summary="Test summary content",
            span_count=10,
            duration_minutes=5.0,
            tool_count=3,
            failure_count=1,
        )

        result = summary.to_dict()

        assert result["session_id"] == "test_session"
        assert result["summary"] == "Test summary content"
        assert result["span_count"] == 10
        assert result["duration_minutes"] == 5.0
        assert result["tool_count"] == 3
        assert result["failure_count"] == 1

    def test_summary_with_tokens(self):
        """Summary includes token usage."""
        summary = SessionSummary(
            session_id="test",
            summary="Summary",
            tokens_used={"prompt_tokens": 100, "completion_tokens": 50},
            model_used="gpt-4o-mini",
        )

        result = summary.to_dict()

        assert result["tokens_used"]["prompt_tokens"] == 100
        assert result["model_used"] == "gpt-4o-mini"


class TestSummaryPreview:
    """Test Case 2: Preview functionality."""

    def test_preview_structure(self):
        """Preview returns expected structure."""
        session = {
            "session_id": "test123",
            "spans": [
                {"span_id": "1", "name": "test", "span_kind": "TOOL"},
            ],
        }

        preview = get_summary_preview(session)

        assert "session_id" in preview
        assert "span_count" in preview
        assert "prompt_length" in preview
        assert "estimated_tokens" in preview
        assert "batch_summary" in preview
        assert "metrics" in preview

    def test_preview_calculates_tokens(self):
        """Preview estimates token count."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "test", "input_value": "x" * 400},
            ],
        }

        preview = get_summary_preview(session)

        assert preview["estimated_tokens"] > 0

    def test_preview_includes_metrics(self):
        """Preview includes session metrics."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
                {"span_id": "2", "name": "Claude_Code_Tool_Write", "span_kind": "TOOL"},
            ],
        }

        preview = get_summary_preview(session)

        assert "duration_minutes" in preview["metrics"]
        assert "tool_calls" in preview["metrics"]


class TestSessionMetrics:
    """Test Case 3: Session metrics extraction."""

    def test_extracts_tool_count(self):
        """Extracts tool call count."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
                {"span_id": "2", "name": "Claude_Code_Tool_Write", "span_kind": "TOOL"},
                {"span_id": "3", "name": "llm_call", "span_kind": "LLM"},
            ],
        }

        preview = get_summary_preview(session)

        assert preview["metrics"]["tool_calls"] == 2

    def test_extracts_failure_count(self):
        """Extracts failure count."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "op1", "status_code": "OK"},
                {"span_id": "2", "name": "op2", "status_code": "ERROR"},
            ],
        }

        preview = get_summary_preview(session)

        assert preview["metrics"]["failures"] == 1


class TestEmptySession:
    """Test Case 4: Empty session handling."""

    def test_empty_session_preview(self):
        """Handles empty session."""
        session = {
            "session_id": "empty",
            "spans": [],
        }

        preview = get_summary_preview(session)

        assert preview["span_count"] == 0
        assert preview["estimated_tokens"] > 0  # Base prompt still has tokens

    def test_session_without_id(self):
        """Handles session without ID."""
        session = {
            "spans": [{"span_id": "1", "name": "test"}],
        }

        preview = get_summary_preview(session)

        assert preview["session_id"] is None


class TestBatchSummary:
    """Tests for batch summary in preview."""

    def test_includes_categories(self):
        """Preview includes span categories."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
                {"span_id": "2", "name": "llm", "span_kind": "LLM", "llm_model_name": "sonnet"},
            ],
        }

        preview = get_summary_preview(session)

        assert "categories" in preview["batch_summary"]

    def test_includes_has_errors(self):
        """Preview includes error status."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "op", "status_code": "ERROR"},
            ],
        }

        preview = get_summary_preview(session)

        assert "has_errors" in preview["batch_summary"]
        assert preview["batch_summary"]["has_errors"] is True

    def test_no_errors_when_clean(self):
        """No errors flag when session is clean."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "op", "status_code": "OK"},
            ],
        }

        preview = get_summary_preview(session)

        assert preview["batch_summary"]["has_errors"] is False
