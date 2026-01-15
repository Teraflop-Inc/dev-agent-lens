#!/usr/bin/env python3
"""
Test suite for validate_export.py

Tests the ground truth validation script to ensure it correctly identifies
missing content and handles edge cases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from validate_export import (
    SUBAGENT_INLINE_THRESHOLD,
    TOOL_INLINE_THRESHOLD,
    ValidationResult,
    extract_assistant_messages,
    extract_subagents,
    extract_tool_calls,
    extract_tool_results,
    extract_user_messages,
    is_system_reminder,
    is_warmup_message,
    parse_message_content,
    validate_session,
)


class TestParsingFunctions:
    """Test message parsing and filtering functions."""

    def test_is_system_reminder(self):
        """Test system reminder detection."""
        assert is_system_reminder("<system-reminder>You should use Read tool</system-reminder>")
        assert is_system_reminder("  <system-reminder>Test</system-reminder>  ")
        assert not is_system_reminder("Regular message")
        assert not is_system_reminder("system-reminder without tags")

    def test_is_warmup_message(self):
        """Test warmup message detection."""
        assert is_warmup_message("Warmup")
        assert is_warmup_message('"Warmup"')
        assert is_warmup_message("  Warmup  ")
        assert not is_warmup_message("Warmup message")
        assert not is_warmup_message("Not a warmup")

    def test_parse_message_content_text(self):
        """Test parsing plain text content."""
        result = parse_message_content("Hello world")
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "Hello world"

    def test_parse_message_content_json_array(self):
        """Test parsing JSON array of message blocks."""
        content = json.dumps([
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "name": "Read", "id": "tool_123", "input": {"file": "test.txt"}},
        ])
        result = parse_message_content(content)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "Hello"
        assert result[1]["type"] == "tool_use"
        assert result[1]["tool"] == "Read"
        assert result[1]["id"] == "tool_123"

    def test_parse_message_content_empty(self):
        """Test parsing empty content."""
        assert parse_message_content("") == []
        assert parse_message_content(None) == []


class TestExtractors:
    """Test content extraction from spans."""

    def test_extract_user_messages_filters_system_reminders(self):
        """Test that system reminders are excluded from user messages."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "input_value": json.dumps([
                    {"type": "text", "text": "Real user message"},
                    {"type": "text", "text": "<system-reminder>System note</system-reminder>"},
                ]),
            }
        ]
        messages = extract_user_messages(spans)
        assert len(messages) == 1
        assert messages[0]["text"] == "Real user message"
        assert not any("system-reminder" in m["text"] for m in messages)

    def test_extract_user_messages_includes_warmup(self):
        """Test that warmup messages are included with flag."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "input_value": json.dumps([{"type": "text", "text": "Warmup"}]),
            }
        ]
        messages = extract_user_messages(spans)
        assert len(messages) == 1
        assert messages[0]["is_warmup"] is True

    def test_extract_user_messages_skips_compaction(self):
        """Test that compaction-related inputs are skipped."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "input_value": "Your task is to create a detailed summary of the conversation...",
            },
            {
                "name": "Claude_Code_Internal_Prompt_2",
                "span_id": "span_2",
                "input_value": "This session is being continued... The conversation is summarized below:",
            },
        ]
        messages = extract_user_messages(spans)
        assert len(messages) == 0

    def test_extract_tool_calls_excludes_task_tool(self):
        """Test that Task tool (subagents) are excluded from tool calls."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "output_value": json.dumps([
                    {"type": "tool_use", "name": "Read", "id": "tool_1", "input": {}},
                    {"type": "tool_use", "name": "Task", "id": "tool_2", "input": {}},
                ]),
            }
        ]
        tool_calls = extract_tool_calls(spans)
        assert len(tool_calls) == 1
        assert tool_calls[0]["tool_name"] == "Read"

    def test_extract_tool_results_excludes_subagent_results(self):
        """Test that tool results for subagents are excluded."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "output_value": json.dumps([
                    {"type": "tool_use", "name": "Read", "id": "tool_1", "input": {}},
                    {"type": "tool_use", "name": "Task", "id": "tool_2", "input": {}},
                ]),
            },
            {
                "name": "Claude_Code_Internal_Prompt_2",
                "span_id": "span_2",
                "input_value": json.dumps([
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": "File content"},
                    {"type": "tool_result", "tool_use_id": "tool_2", "content": "Subagent result"},
                ]),
            },
        ]
        tool_results, subagent_ids = extract_tool_results(spans)
        assert len(tool_results) == 1
        assert "tool_1" in tool_results
        assert "tool_2" not in tool_results
        assert "tool_2" in subagent_ids

    def test_extract_subagents(self):
        """Test subagent extraction."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "output_value": json.dumps([
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "id": "tool_1",
                        "input": {
                            "subagent_type": "general-purpose",
                            "prompt": "Do something",
                        },
                    }
                ]),
            }
        ]
        subagents = extract_subagents(spans)
        assert len(subagents) == 1
        assert subagents[0]["subagent_type"] == "general-purpose"
        assert subagents[0]["tool_id"] == "tool_1"


class TestValidationResult:
    """Test ValidationResult dataclass."""

    def test_passed_property_all_found(self):
        """Test that passed returns True when all content found."""
        result = ValidationResult(session_id="test")
        result.user_messages_expected = 2
        result.user_messages_found = 2
        result.assistant_messages_expected = 3
        result.assistant_messages_found = 3
        result.tool_calls_expected = 1
        result.tool_calls_found = 1
        result.subagents_expected = 1
        result.subagents_found = 1
        result.tool_results_missing = 0
        assert result.passed is True

    def test_passed_property_user_missing(self):
        """Test that passed returns False when user messages missing."""
        result = ValidationResult(session_id="test")
        result.user_messages_expected = 2
        result.user_messages_found = 1
        assert result.passed is False

    def test_passed_property_tool_results_missing(self):
        """Test that passed returns False when tool results missing."""
        result = ValidationResult(session_id="test")
        result.tool_results_expected = 5
        result.tool_results_inline = 3
        result.tool_results_linked = 1
        result.tool_results_missing = 1  # One missing
        assert result.passed is False


class TestThresholds:
    """Test threshold constants match 4.1 decisions."""

    def test_tool_inline_threshold(self):
        """Test tool inline threshold is 500 chars."""
        assert TOOL_INLINE_THRESHOLD == 500

    def test_subagent_inline_threshold(self):
        """Test subagent inline threshold is 1000 chars."""
        assert SUBAGENT_INLINE_THRESHOLD == 1000


class TestEdgeCases:
    """Test edge case handling."""

    def test_empty_session(self):
        """Test handling of session with no spans."""
        # This would raise ValueError in run_export, which is correct behavior
        # We just verify the extractors handle empty lists gracefully
        assert extract_user_messages([]) == []
        assert extract_assistant_messages([]) == []
        assert extract_tool_calls([]) == []
        assert extract_subagents([]) == []
        tool_results, subagent_ids = extract_tool_results([])
        assert tool_results == {}
        assert subagent_ids == set()

    def test_span_with_missing_fields(self):
        """Test handling of spans with missing fields."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                # Missing input_value and output_value
            }
        ]
        # Should not crash, just return empty results
        assert extract_user_messages(spans) == []
        assert extract_assistant_messages(spans) == []
        assert extract_tool_calls(spans) == []

    def test_span_with_raw_attributes_json(self):
        """Test extraction from raw_attributes_json when direct fields missing."""
        spans = [
            {
                "name": "Claude_Code_Internal_Prompt_1",
                "span_id": "span_1",
                "raw_attributes_json": json.dumps({
                    "attributes": {
                        "input": {"value": json.dumps([{"type": "text", "text": "From raw attrs"}])},
                        "output": {"value": json.dumps([{"type": "text", "text": "Output from raw"}])},
                    }
                }),
            }
        ]
        # Note: The extract functions expect input_value/output_value to be populated
        # by load_session_spans, so we need to test after that processing
        # For now, just verify it doesn't crash
        messages = extract_user_messages(spans)
        # Will be empty because input_value not set, but shouldn't crash
        assert isinstance(messages, list)


@pytest.mark.integration
class TestIntegrationWithRealSessions:
    """Integration tests with real sessions (require parquet data)."""

    @pytest.fixture
    def parquet_path(self):
        """Get path to parquet file."""
        from dev_agent_lens.storage.oxen_store import OxenStore

        store = OxenStore()
        parquet_dir = Path(store.data_path) / "parquet"
        parquet_files = list(parquet_dir.glob("*_spans.parquet"))
        if not parquet_files:
            pytest.skip("No parquet files found")
        return str(parquet_files[0])

    @pytest.mark.parametrize(
        "session_id,expected_type",
        [
            ("3640c6d77574ea64f556583219487860", "simple_with_subagent"),
            ("3200b4ff336a699241939b7cbd124d99", "multi_turn"),
            ("6a333d9730112aa89c13f43c68493689", "complex_tools"),
        ],
    )
    def test_real_sessions_validation(self, parquet_path, session_id, expected_type):
        """Test validation on real sessions."""
        try:
            result = validate_session(session_id, parquet_path)
            # All test sessions should pass validation
            assert result.passed, f"Validation failed for {expected_type} session"
            # Verify we actually found content
            total_expected = (
                result.user_messages_expected
                + result.assistant_messages_expected
                + result.tool_calls_expected
                + result.tool_results_expected
                + result.subagents_expected
            )
            assert total_expected > 0, f"No content found in {expected_type} session"
        except ValueError as e:
            if "No spans found" in str(e):
                pytest.skip(f"Session {session_id} not in parquet file")
            raise

    def test_invalid_session_id(self, parquet_path):
        """Test that invalid session ID raises appropriate error."""
        with pytest.raises(ValueError, match="No spans found"):
            validate_session("invalid_session_id", parquet_path)


class TestValidationLogic:
    """Test the validation logic that compares extracted content to exports."""

    def test_validation_result_print_report(self, capsys):
        """Test that print_report produces readable output."""
        result = ValidationResult(session_id="test_session")
        result.user_messages_expected = 2
        result.user_messages_found = 1
        result.user_messages_missing = ["Missing user message"]
        result.assistant_messages_expected = 3
        result.assistant_messages_found = 3
        result.tool_calls_expected = 1
        result.tool_calls_found = 1
        result.tool_results_expected = 2
        result.tool_results_inline = 1
        result.tool_results_linked = 1
        result.subagents_expected = 0
        result.subagents_found = 0
        result.warnings = ["Test warning"]

        result.print_report()
        captured = capsys.readouterr()

        # Verify key elements are in output
        assert "test_session" in captured.out
        assert "User messages: 1/2" in captured.out
        assert "Assistant messages: 3/3" in captured.out
        assert "FAIL" in captured.out  # Should fail due to missing user message
        assert "Test warning" in captured.out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
