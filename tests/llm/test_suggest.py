"""
Tests for the suggest module (Story 4.5).

Test Cases:
1. Suggestion structure
2. Heuristic suggestions
3. Preview functionality
4. Filtering and sorting
5. Empty and edge cases
"""

from __future__ import annotations

import pytest

from dev_agent_lens.llm.suggest import (
    Suggestion,
    SuggestionCategory,
    SuggestionResult,
    SuggestionSeverity,
    get_suggestion_preview,
)


class TestSuggestionStructure:
    """Test Case 1: Suggestion structure."""

    def test_suggestion_to_dict(self):
        """Suggestion converts to dictionary."""
        suggestion = Suggestion(
            category=SuggestionCategory.ERROR,
            severity=SuggestionSeverity.HIGH,
            title="Fix Error",
            description="There was an error",
            recommendation="Fix it",
            impact="Improved reliability",
        )

        result = suggestion.to_dict()

        assert result["category"] == "error"
        assert result["severity"] == "high"
        assert result["title"] == "Fix Error"
        assert result["recommendation"] == "Fix it"

    def test_suggestion_result_to_dict(self):
        """SuggestionResult converts to dictionary."""
        suggestions = [
            Suggestion(
                category=SuggestionCategory.ERROR,
                severity=SuggestionSeverity.HIGH,
                title="Error",
                description="Desc",
                recommendation="Fix",
            ),
            Suggestion(
                category=SuggestionCategory.EFFICIENCY,
                severity=SuggestionSeverity.MEDIUM,
                title="Efficiency",
                description="Desc",
                recommendation="Improve",
            ),
        ]
        result = SuggestionResult(
            session_id="test",
            suggestions=suggestions,
            summary="Summary",
        )

        data = result.to_dict()

        assert data["session_id"] == "test"
        assert data["suggestion_count"] == 2
        assert data["by_severity"]["high"] == 1
        assert data["by_severity"]["medium"] == 1
        assert data["by_category"]["error"] == 1


class TestHeuristicSuggestions:
    """Test Case 2: Heuristic suggestions."""

    def test_detects_errors(self):
        """Detects error spans."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "op1", "status_code": "OK"},
                {"span_id": "2", "name": "op2", "status_code": "ERROR"},
            ],
        }

        preview = get_suggestion_preview(session)

        # Should have error-related suggestion
        error_suggestions = [
            s for s in preview["heuristic_suggestions"]
            if s["category"] == "error"
        ]
        assert len(error_suggestions) > 0

    def test_detects_back_to_back(self):
        """Detects back-to-back identical operations."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "input_value": "file.txt"},
                {"span_id": "2", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL", "input_value": "file.txt"},
            ],
        }

        preview = get_suggestion_preview(session)

        # Should detect repeated operations
        efficiency_suggestions = [
            s for s in preview["heuristic_suggestions"]
            if s["category"] == "efficiency"
        ]
        assert len(efficiency_suggestions) > 0

    def test_detects_churn(self):
        """Detects file churn patterns."""
        session = {
            "session_id": "test",
            "spans": [
                {"span_id": "1", "name": "Claude_Code_Tool_Edit", "span_kind": "TOOL", "input_value": '"/path/file.py"'},
                {"span_id": "2", "name": "Claude_Code_Tool_Edit", "span_kind": "TOOL", "input_value": '"/path/file.py"'},
                {"span_id": "3", "name": "Claude_Code_Tool_Edit", "span_kind": "TOOL", "input_value": '"/path/file.py"'},
            ],
        }

        preview = get_suggestion_preview(session)

        churn_suggestions = [
            s for s in preview["heuristic_suggestions"]
            if s["category"] == "churn"
        ]
        assert len(churn_suggestions) > 0


class TestPreviewFunctionality:
    """Test Case 3: Preview functionality."""

    def test_preview_structure(self):
        """Preview returns expected structure."""
        session = {
            "session_id": "test",
            "spans": [{"span_id": "1", "name": "test"}],
        }

        preview = get_suggestion_preview(session)

        assert "session_id" in preview
        assert "span_count" in preview
        assert "heuristic_suggestions" in preview
        assert "suggestion_count" in preview
        assert "llm_required" in preview

    def test_preview_without_llm(self):
        """Preview works without LLM."""
        session = {
            "session_id": "test",
            "spans": [{"span_id": "1", "name": "test", "status_code": "ERROR"}],
        }

        preview = get_suggestion_preview(session)

        # Should have heuristic suggestions
        assert preview["suggestion_count"] > 0
        assert preview["llm_required"] is True


class TestFilteringAndSorting:
    """Test Case 4: Filtering and sorting."""

    def test_filter_by_severity(self):
        """Filter suggestions by severity."""
        suggestions = [
            Suggestion(
                category=SuggestionCategory.ERROR,
                severity=SuggestionSeverity.HIGH,
                title="High",
                description="",
                recommendation="",
            ),
            Suggestion(
                category=SuggestionCategory.ERROR,
                severity=SuggestionSeverity.LOW,
                title="Low",
                description="",
                recommendation="",
            ),
        ]
        result = SuggestionResult(session_id="test", suggestions=suggestions)

        high = result.filter_by_severity(SuggestionSeverity.HIGH)

        assert len(high) == 1
        assert high[0].title == "High"

    def test_filter_by_category(self):
        """Filter suggestions by category."""
        suggestions = [
            Suggestion(
                category=SuggestionCategory.ERROR,
                severity=SuggestionSeverity.HIGH,
                title="Error",
                description="",
                recommendation="",
            ),
            Suggestion(
                category=SuggestionCategory.CHURN,
                severity=SuggestionSeverity.MEDIUM,
                title="Churn",
                description="",
                recommendation="",
            ),
        ]
        result = SuggestionResult(session_id="test", suggestions=suggestions)

        errors = result.filter_by_category(SuggestionCategory.ERROR)

        assert len(errors) == 1
        assert errors[0].title == "Error"

    def test_count_by_severity(self):
        """Counts suggestions by severity."""
        suggestions = [
            Suggestion(category=SuggestionCategory.ERROR, severity=SuggestionSeverity.HIGH, title="1", description="", recommendation=""),
            Suggestion(category=SuggestionCategory.ERROR, severity=SuggestionSeverity.HIGH, title="2", description="", recommendation=""),
            Suggestion(category=SuggestionCategory.ERROR, severity=SuggestionSeverity.MEDIUM, title="3", description="", recommendation=""),
        ]
        result = SuggestionResult(session_id="test", suggestions=suggestions)

        counts = result._count_by_severity()

        assert counts["high"] == 2
        assert counts["medium"] == 1


class TestEmptyAndEdgeCases:
    """Test Case 5: Empty and edge cases."""

    def test_empty_session(self):
        """Handles empty session."""
        session = {
            "session_id": "empty",
            "spans": [],
        }

        preview = get_suggestion_preview(session)

        assert preview["span_count"] == 0
        assert preview["suggestion_count"] == 0

    def test_clean_session_no_suggestions(self):
        """Clean session has no suggestions."""
        session = {
            "session_id": "clean",
            "spans": [
                {"span_id": "1", "name": "op1", "status_code": "OK"},
                {"span_id": "2", "name": "op2", "status_code": "OK"},
            ],
        }

        preview = get_suggestion_preview(session)

        # Should have no or few suggestions
        assert preview["suggestion_count"] < 2

    def test_session_without_id(self):
        """Handles session without ID."""
        session = {
            "spans": [{"span_id": "1", "name": "test"}],
        }

        preview = get_suggestion_preview(session)

        assert preview["session_id"] is None


class TestSuggestionEnums:
    """Tests for suggestion enums."""

    def test_category_values(self):
        """Category enum has expected values."""
        assert SuggestionCategory.ERROR.value == "error"
        assert SuggestionCategory.EFFICIENCY.value == "efficiency"
        assert SuggestionCategory.CHURN.value == "churn"
        assert SuggestionCategory.BEST_PRACTICE.value == "best_practice"
        assert SuggestionCategory.PERFORMANCE.value == "performance"

    def test_severity_values(self):
        """Severity enum has expected values."""
        assert SuggestionSeverity.HIGH.value == "high"
        assert SuggestionSeverity.MEDIUM.value == "medium"
        assert SuggestionSeverity.LOW.value == "low"


class TestSuggestionEvidence:
    """Tests for suggestion evidence."""

    def test_suggestion_with_evidence(self):
        """Suggestion includes evidence."""
        suggestion = Suggestion(
            category=SuggestionCategory.CHURN,
            severity=SuggestionSeverity.MEDIUM,
            title="Churn",
            description="Files edited multiple times",
            recommendation="Plan better",
            evidence=["/path/file1.py", "/path/file2.py"],
        )

        result = suggestion.to_dict()

        assert len(result["evidence"]) == 2
        assert "/path/file1.py" in result["evidence"]

    def test_suggestion_without_evidence(self):
        """Suggestion works without evidence."""
        suggestion = Suggestion(
            category=SuggestionCategory.ERROR,
            severity=SuggestionSeverity.HIGH,
            title="Error",
            description="Desc",
            recommendation="Fix",
        )

        result = suggestion.to_dict()

        assert result["evidence"] == []
