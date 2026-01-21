"""
Unit tests for Claude Code Session to Markdown Exporter.

Tests use EXACT string matching (assert output == expected) as specified
in AGREED_FORMAT.md. All tests verify byte-for-byte output consistency.
"""

import json
import tempfile
from pathlib import Path

import pytest

from dev_agent_lens.export.markdown import (
    SUBAGENT_PROMPT_PREVIEW_LIMIT,
    SUBAGENT_RESPONSE_SUMMARY_LIMIT,
    TOOL_INPUT_VALUE_LIMIT,
    TOOL_RESULT_FILE_THRESHOLD,
    TOOL_RESULT_INLINE_LIMIT,
    MarkdownExport,
    export_session_to_markdown,
    export_to_files,
    extract_text_content,
    format_timestamp,
    format_tool_input,
    get_language_hint,
    get_tool_target_brief,
    normalize_subagent_type,
    parse_jsonl_file,
    parse_message,
    truncate,
)


class TestTruncate:
    """Tests for the truncate function."""

    def test_no_truncation_when_under_limit(self):
        """Text under limit should not be truncated."""
        assert truncate("hello", 10) == "hello"
        assert truncate("hello", 5) == "hello"

    def test_truncation_at_exact_limit(self):
        """Text at exactly limit should not be truncated."""
        assert truncate("hello", 5) == "hello"

    def test_truncation_over_limit(self):
        """Text over limit should be truncated to (limit-3) + '...'."""
        # 10 chars limit -> show 7 chars + '...'
        assert truncate("hello world", 10) == "hello w..."
        # 500 char limit -> show 497 chars + '...'
        long_text = "a" * 600
        result = truncate(long_text, 500)
        assert result == "a" * 497 + "..."
        assert len(result) == 500

    def test_truncation_exact_threshold(self):
        """Verify exact AGREED_FORMAT thresholds."""
        # Tool result inline: 500 chars -> 497 + '...'
        text_500 = "x" * 501
        result = truncate(text_500, TOOL_RESULT_INLINE_LIMIT)
        assert len(result) == TOOL_RESULT_INLINE_LIMIT
        assert result.endswith("...")

        # Subagent prompt preview: 200 chars -> 197 + '...'
        text_200 = "y" * 201
        result = truncate(text_200, SUBAGENT_PROMPT_PREVIEW_LIMIT)
        assert len(result) == SUBAGENT_PROMPT_PREVIEW_LIMIT
        assert result.endswith("...")


class TestNormalizeSubagentType:
    """Tests for subagent type normalization."""

    def test_lowercase(self):
        assert normalize_subagent_type("Explore") == "explore"
        assert normalize_subagent_type("PLAN") == "plan"

    def test_replace_hyphens(self):
        assert normalize_subagent_type("general-purpose") == "general_purpose"
        assert normalize_subagent_type("claude-code-guide") == "claude_code_guide"

    def test_replace_spaces(self):
        assert normalize_subagent_type("my agent") == "my_agent"

    def test_combined(self):
        assert normalize_subagent_type("My-Custom Agent") == "my_custom_agent"


class TestGetLanguageHint:
    """Tests for file extension to language mapping."""

    def test_python(self):
        assert get_language_hint("/path/to/file.py") == "python"
        assert get_language_hint("test.py") == "python"

    def test_javascript(self):
        assert get_language_hint("app.js") == "javascript"
        assert get_language_hint("component.jsx") == "javascript"

    def test_typescript(self):
        assert get_language_hint("app.ts") == "typescript"
        assert get_language_hint("component.tsx") == "typescript"

    def test_json(self):
        assert get_language_hint("config.json") == "json"

    def test_markdown(self):
        assert get_language_hint("README.md") == "markdown"

    def test_bash(self):
        assert get_language_hint("script.sh") == "bash"
        assert get_language_hint("run.bash") == "bash"

    def test_unknown_extension(self):
        assert get_language_hint("file.xyz") == "text"
        assert get_language_hint("noextension") == "text"


class TestExtractTextContent:
    """Tests for extracting text from message content."""

    def test_string_content(self):
        assert extract_text_content("Hello world") == "Hello world"

    def test_list_with_text_blocks(self):
        content = [
            {"type": "text", "text": "First part"},
            {"type": "text", "text": "Second part"},
        ]
        assert extract_text_content(content) == "First part\nSecond part"

    def test_list_with_mixed_blocks(self):
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "name": "Read", "id": "123"},
            {"type": "text", "text": "World"},
        ]
        assert extract_text_content(content) == "Hello\nWorld"

    def test_empty_content(self):
        assert extract_text_content("") == ""
        assert extract_text_content([]) == ""
        assert extract_text_content(None) == ""


class TestFormatTimestamp:
    """Tests for timestamp formatting."""

    def test_format_with_timezone(self):
        from datetime import datetime, timezone

        dt = datetime(2026, 1, 19, 10, 30, 45, tzinfo=timezone.utc)
        assert format_timestamp(dt) == "2026-01-19 10:30:45 UTC"

    def test_format_naive_datetime(self):
        from datetime import datetime

        dt = datetime(2026, 1, 19, 10, 30, 45)
        # Naive datetime assumed to be UTC
        assert format_timestamp(dt) == "2026-01-19 10:30:45 UTC"

    def test_none_timestamp(self):
        assert format_timestamp(None) == ""


class TestFormatToolInput:
    """Tests for tool input formatting."""

    def test_simple_input(self):
        tool_input = {"file_path": "/path/to/file.py"}
        assert format_tool_input(tool_input) == "file_path: /path/to/file.py"

    def test_multiple_keys_alphabetical(self):
        tool_input = {"pattern": "*.py", "path": "/src"}
        expected = "path: /src\npattern: *.py"
        assert format_tool_input(tool_input) == expected

    def test_long_value_truncation(self):
        long_value = "x" * 300
        tool_input = {"content": long_value}
        result = format_tool_input(tool_input)
        # Should be truncated to 197 + '...'
        assert "content: " in result
        assert result.endswith("...")
        value_part = result.split(": ", 1)[1]
        assert len(value_part) == TOOL_INPUT_VALUE_LIMIT

    def test_dict_value_json_serialized(self):
        tool_input = {"options": {"key": "value"}}
        result = format_tool_input(tool_input)
        assert 'options: {"key": "value"}' == result

    def test_empty_input(self):
        assert format_tool_input({}) == ""


class TestGetToolTargetBrief:
    """Tests for parallel tools table target formatting."""

    def test_read_tool(self):
        assert get_tool_target_brief("Read", {"file_path": "/src/app.py"}) == "/src/app.py"

    def test_read_tool_long_path(self):
        # Path must exceed 60 characters to trigger truncation
        long_path = "/very/long/path/that/definitely/exceeds/sixty/characters/limit/and/more/file.py"
        assert len(long_path) > 60  # Verify our test path is long enough
        result = get_tool_target_brief("Read", {"file_path": long_path})
        assert result.startswith("...")
        assert len(result) <= 60

    def test_bash_tool(self):
        result = get_tool_target_brief("Bash", {"command": "ls -la"})
        assert result == "ls -la"

    def test_grep_tool(self):
        result = get_tool_target_brief("Grep", {"pattern": "TODO", "path": "/src"})
        assert result == "`TODO` in `/src`"

    def test_task_tool(self):
        result = get_tool_target_brief(
            "Task",
            {"subagent_type": "Explore", "description": "Find all usages"},
        )
        assert result == "Explore: Find all usages"


class TestParseMessage:
    """Tests for JSONL message parsing."""

    def test_parse_user_message(self):
        raw = {
            "type": "user",
            "uuid": "abc123",
            "parentUuid": "parent123",
            "message": {"content": "Hello"},
            "timestamp": "2026-01-19T10:00:00Z",
            "cwd": "/project",
            "gitBranch": "main",
        }
        msg = parse_message(raw)
        assert msg.type == "user"
        assert msg.uuid == "abc123"
        assert msg.parent_uuid == "parent123"
        assert msg.cwd == "/project"
        assert msg.git_branch == "main"

    def test_parse_assistant_with_tool_use(self):
        raw = {
            "type": "assistant",
            "uuid": "def456",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll read the file."},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "id": "toolu_123",
                        "input": {"file_path": "/file.py"},
                    },
                ]
            },
        }
        msg = parse_message(raw)
        assert msg.type == "assistant"
        assert len(msg.tool_uses) == 1
        assert msg.tool_uses[0]["name"] == "Read"
        assert msg.tool_uses[0]["id"] == "toolu_123"

    def test_parse_parallel_tool_uses(self):
        raw = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "t1", "input": {}},
                    {"type": "tool_use", "name": "Read", "id": "t2", "input": {}},
                    {"type": "tool_use", "name": "Grep", "id": "t3", "input": {}},
                ]
            },
        }
        msg = parse_message(raw)
        assert len(msg.tool_uses) == 3

    def test_parse_tool_result_string(self):
        raw = {
            "type": "user",
            "toolUseResult": "file contents here",
        }
        msg = parse_message(raw)
        assert msg.tool_use_result == "file contents here"

    def test_parse_subagent_result(self):
        raw = {
            "type": "user",
            "toolUseResult": {
                "agentId": "abc12345",
                "status": "completed",
                "content": [{"type": "text", "text": "Found 5 files"}],
                "totalDurationMs": 5000,
                "totalTokens": 1000,
            },
        }
        msg = parse_message(raw)
        assert isinstance(msg.tool_use_result, dict)
        assert msg.tool_use_result["agentId"] == "abc12345"


class TestExportSessionToMarkdown:
    """Integration tests for full session export."""

    def test_simple_conversation(self):
        """Test basic user-assistant conversation without tools."""
        jsonl_content = [
            {
                "type": "summary",
                "summary": "Test conversation",
                "sessionId": "a2c1f62a-5ee0-4a49-9187-9c2130d8deac",
            },
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Hello"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "gitBranch": "main",
                "sessionId": "a2c1f62a-5ee0-4a49-9187-9c2130d8deac",
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {"content": [{"type": "text", "text": "Hi there!"}]},
                "timestamp": "2026-01-19T10:00:05.000Z",
                "sessionId": "a2c1f62a-5ee0-4a49-9187-9c2130d8deac",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "a2c1f62a-5ee0-4a49-9187-9c2130d8deac.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            # Check metadata
            assert export.session_id == "a2c1f62a-5ee0-4a49-9187-9c2130d8deac"
            assert export.project_path == "/project"
            assert export.git_branch == "main"
            assert export.summary == "Test conversation"

            # Check stats
            assert export.stats["user_turns"] == 1
            assert export.stats["assistant_turns"] == 1
            assert export.stats["tool_calls"] == 0
            assert export.stats["subagents"] == 0

            # Check content structure
            assert "# Session: a2c1f62a" in export.main_content
            assert "## Metadata" in export.main_content
            assert "## Conversation" in export.main_content
            assert "### User" in export.main_content
            assert "Hello" in export.main_content
            assert "### Assistant" in export.main_content
            assert "Hi there!" in export.main_content

            # Check pipeline-specific markers
            assert "<!-- BEGIN PIPELINE_SPECIFIC -->" in export.main_content
            assert "<!-- END PIPELINE_SPECIFIC -->" in export.main_content
            assert "(Claude only)" in export.main_content

    def test_single_tool_call(self):
        """Test conversation with a single tool call."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Read the file"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "id": "toolu_1",
                            "input": {"file_path": "/project/test.py"},
                        }
                    ]
                },
                "timestamp": "2026-01-19T10:00:01.000Z",
                "sessionId": "test-session",
            },
            {
                "type": "user",
                "uuid": "u2",
                "parentUuid": "a1",
                "toolUseResult": "print('hello')",
                "timestamp": "2026-01-19T10:00:02.000Z",
                "sessionId": "test-session",
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "u2",
                "message": {"content": [{"type": "text", "text": "Found the file."}]},
                "timestamp": "2026-01-19T10:00:03.000Z",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert export.stats["tool_calls"] == 1
            assert "### Tool: Read" in export.main_content
            assert "file_path: /project/test.py" in export.main_content
            assert "print('hello')" in export.main_content
            assert "```python" in export.main_content  # Language hint from .py

    def test_parallel_tool_calls(self):
        """Test conversation with parallel tool calls."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Read both files"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "id": "toolu_1",
                            "input": {"file_path": "/a.py"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "id": "toolu_2",
                            "input": {"file_path": "/b.py"},
                        },
                    ]
                },
                "timestamp": "2026-01-19T10:00:01.000Z",
                "sessionId": "test-session",
            },
            {
                "type": "user",
                "uuid": "u2",
                "parentUuid": "a1",
                "toolUseResult": "content_a",
                "timestamp": "2026-01-19T10:00:02.000Z",
                "sessionId": "test-session",
            },
            {
                "type": "user",
                "uuid": "u3",
                "parentUuid": "u2",
                "toolUseResult": "content_b",
                "timestamp": "2026-01-19T10:00:03.000Z",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert export.stats["tool_calls"] == 2
            assert "### Parallel Tools (2 calls)" in export.main_content
            assert "| # | Tool | Target |" in export.main_content
            assert "| 1 | Read | /a.py |" in export.main_content
            assert "| 2 | Read | /b.py |" in export.main_content
            assert "**[1]**" in export.main_content
            assert "**[2]**" in export.main_content

    def test_subagent_call(self):
        """Test conversation with a subagent call."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Find all usages"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "main-session",
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Task",
                            "id": "toolu_task",
                            "input": {
                                "subagent_type": "Explore",
                                "description": "Find function usages",
                                "prompt": "Search for all usages of the function foo()",
                            },
                        }
                    ]
                },
                "timestamp": "2026-01-19T10:00:01.000Z",
                "sessionId": "main-session",
            },
            {
                "type": "user",
                "uuid": "u2",
                "parentUuid": "a1",
                "toolUseResult": {
                    "agentId": "abc12345",
                    "status": "completed",
                    "prompt": "Search for all usages of the function foo()",
                    "content": [{"type": "text", "text": "Found 5 usages of foo()"}],
                    "totalDurationMs": 5000,
                    "totalTokens": 1000,
                    "totalToolUseCount": 3,
                },
                "timestamp": "2026-01-19T10:00:10.000Z",
                "sessionId": "main-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "main-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert export.stats["subagents"] == 1
            assert "### Subagent: Explore" in export.main_content
            assert "**Task**: Find function usages" in export.main_content
            assert "Search for all usages of the function foo()" in export.main_content
            assert "Found 5 usages of foo()" in export.main_content
            assert "subagent_explore_1.md" in export.main_content

            # Check subagent file was generated (summary-only since no agent file)
            assert "subagent_explore_1" in export.subagent_files
            subagent_content = export.subagent_files["subagent_explore_1"]
            assert "# Subagent: Explore (subagent_explore_1)" in subagent_content
            assert "*[Full conversation not available" in subagent_content

    def test_empty_session(self):
        """Test handling of empty session file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "empty.jsonl"
            jsonl_path.write_text("")

            export = export_session_to_markdown(jsonl_path)

            assert "Empty Session" in export.main_content
            assert export.stats["user_turns"] == 0

    def test_no_branch_handling(self):
        """Test that missing git branch shows placeholder."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Hello"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                # No gitBranch field
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert "*[No branch]*" in export.main_content

    def test_no_summary_handling(self):
        """Test that missing summary shows placeholder."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Hello"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert "*[No summary]*" in export.main_content


class TestExportToFiles:
    """Tests for writing export to files."""

    def test_writes_main_file(self):
        """Test that main file is written correctly."""
        export = MarkdownExport(
            main_content="# Test\n\nContent\n",
            session_id="test-123",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            files = export_to_files(export, tmpdir)

            assert len(files) == 1
            assert files[0].name == "test-123.md"
            assert files[0].read_text() == "# Test\n\nContent\n"

    def test_writes_subagent_files(self):
        """Test that subagent files are written."""
        export = MarkdownExport(
            main_content="# Main\n",
            subagent_files={
                "subagent_explore_1": "# Subagent 1\n",
                "subagent_explore_2": "# Subagent 2\n",
            },
            session_id="test-123",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            files = export_to_files(export, tmpdir)

            assert len(files) == 3
            filenames = [f.name for f in files]
            assert "test-123.md" in filenames
            assert "subagent_explore_1.md" in filenames
            assert "subagent_explore_2.md" in filenames

    def test_writes_tool_result_files(self):
        """Test that large tool result files are written."""
        export = MarkdownExport(
            main_content="# Main\n",
            tool_result_files={
                "001_read": "Long content...",
                "002_bash": "More content...",
            },
            session_id="test-123",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            files = export_to_files(export, tmpdir)

            assert len(files) == 3
            tool_results_dir = Path(tmpdir) / "tool_results"
            assert tool_results_dir.exists()
            assert (tool_results_dir / "001_read.txt").exists()
            assert (tool_results_dir / "002_bash.txt").exists()


class TestExactStringMatching:
    """Tests verifying exact string matching requirements from AGREED_FORMAT.md."""

    def test_deterministic_output(self):
        """Same input should always produce identical output."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Test"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "gitBranch": "main",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            # Run export multiple times
            export1 = export_session_to_markdown(jsonl_path)
            export2 = export_session_to_markdown(jsonl_path)
            export3 = export_session_to_markdown(jsonl_path)

            # All outputs should be identical
            assert export1.main_content == export2.main_content
            assert export2.main_content == export3.main_content

    def test_file_ends_with_newline(self):
        """All files should end with a single newline."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Test"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert export.main_content.endswith("\n")
            # Should not end with multiple newlines
            assert not export.main_content.endswith("\n\n")

    def test_pipeline_specific_markers_always_present(self):
        """PIPELINE_SPECIFIC markers should always be present."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Test"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert "<!-- BEGIN PIPELINE_SPECIFIC -->" in export.main_content
            assert "<!-- END PIPELINE_SPECIFIC -->" in export.main_content

    def test_section_separators(self):
        """Section separators should be exactly '---' on their own line."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Test"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            # Check that --- appears on its own line
            lines = export.main_content.split("\n")
            separator_lines = [line for line in lines if line.strip() == "---"]
            assert len(separator_lines) >= 1

            # Each separator should be exactly "---"
            for line in lines:
                if "---" in line:
                    assert line.strip() == "---" or line.strip().startswith("-") is False

    def test_timestamps_inside_pipeline_specific(self):
        """Timestamps should be inside PIPELINE_SPECIFIC section per v1.1."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Test"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            # Extract PIPELINE_SPECIFIC section
            content = export.main_content
            begin_idx = content.index("<!-- BEGIN PIPELINE_SPECIFIC -->")
            end_idx = content.index("<!-- END PIPELINE_SPECIFIC -->")
            pipeline_section = content[begin_idx:end_idx]

            # Timestamps should be INSIDE this section
            assert "**Started**:" in pipeline_section
            assert "**Ended**:" in pipeline_section
            assert "(Claude only)" in pipeline_section

            # Timestamps should NOT appear BEFORE this section
            before_section = content[:begin_idx]
            assert "**Started**:" not in before_section
            assert "**Ended**:" not in before_section


class TestCompactionHandling:
    """Tests for compaction boundary detection and formatting."""

    def test_single_compaction_detection(self):
        """Single compaction boundary should be detected and formatted."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Initial message"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {"content": [{"type": "text", "text": "Response before compaction"}]},
                "timestamp": "2026-01-19T10:00:10.000Z",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "c1",
                "content": "Conversation compacted",
                "timestamp": "2026-01-19T10:30:00.000Z",
                "compactMetadata": {
                    "trigger": "auto",
                    "preTokens": 155000,
                },
                "sessionId": "test-session",
            },
            {
                "type": "user",
                "uuid": "u2",
                "message": {"content": "This session is being continued from a previous conversation. Summary: user asked about X, assistant explained Y."},
                "timestamp": "2026-01-19T10:30:01.000Z",
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "u2",
                "message": {"content": [{"type": "text", "text": "Continuing the conversation..."}]},
                "timestamp": "2026-01-19T10:30:10.000Z",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            # Check compaction section is present
            assert "### Compaction #1" in export.main_content
            assert "**Trigger**: auto (Claude only)" in export.main_content
            assert "**Pre-compaction tokens**: 155000 (Claude only)" in export.main_content
            assert "> **Context Summary**:" in export.main_content

            # Check stats include compaction count
            assert export.stats["compactions"] == 1

            # Check footer includes compaction count
            assert "1 compactions" in export.main_content

    def test_multiple_compactions(self):
        """Multiple compaction boundaries should be numbered sequentially."""
        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Start"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "c1",
                "content": "Compacted",
                "timestamp": "2026-01-19T10:30:00.000Z",
                "compactMetadata": {"trigger": "auto", "preTokens": 100000},
            },
            {
                "type": "user",
                "uuid": "u2",
                "message": {"content": "First summary..."},
                "timestamp": "2026-01-19T10:30:01.000Z",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "c2",
                "content": "Compacted again",
                "timestamp": "2026-01-19T11:00:00.000Z",
                "compactMetadata": {"trigger": "manual", "preTokens": 200000},
            },
            {
                "type": "user",
                "uuid": "u3",
                "message": {"content": "Second summary..."},
                "timestamp": "2026-01-19T11:00:01.000Z",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            assert "### Compaction #1" in export.main_content
            assert "### Compaction #2" in export.main_content
            assert "**Trigger**: auto (Claude only)" in export.main_content
            assert "**Trigger**: manual (Claude only)" in export.main_content
            assert export.stats["compactions"] == 2
            assert "2 compactions" in export.main_content

    def test_compaction_metadata_extraction(self):
        """Compaction metadata should be correctly extracted."""
        from dev_agent_lens.export.markdown import is_compaction_boundary, get_compaction_metadata

        compact_msg = {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {
                "trigger": "auto",
                "preTokens": 155643,
            },
        }

        assert is_compaction_boundary(compact_msg) is True
        metadata = get_compaction_metadata(compact_msg)
        assert metadata["trigger"] == "auto"
        assert metadata["pre_tokens"] == 155643

    def test_non_compaction_system_message(self):
        """Non-compaction system messages should not be treated as compaction."""
        from dev_agent_lens.export.markdown import is_compaction_boundary

        regular_system = {
            "type": "system",
            "content": "Some system message",
        }

        assert is_compaction_boundary(regular_system) is False

    def test_compaction_summary_truncation(self):
        """Long compaction summaries should be truncated and linked to external file."""
        # Create a summary longer than 500 chars
        long_summary = "This is a very long summary. " * 50  # ~1500 chars

        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Start"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "c1",
                "content": "Compacted",
                "timestamp": "2026-01-19T10:30:00.000Z",
                "compactMetadata": {"trigger": "auto", "preTokens": 100000},
            },
            {
                "type": "user",
                "uuid": "u2",
                "message": {"content": long_summary},
                "timestamp": "2026-01-19T10:30:01.000Z",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            # Should have truncated summary with link
            assert "→ Full summary: [compaction_1_summary.txt](./compaction_1_summary.txt)" in export.main_content

            # Should have external file with full content
            assert "compaction_1_summary" in export.tool_result_files
            assert export.tool_result_files["compaction_1_summary"] == long_summary

    def test_compaction_files_written_to_root(self):
        """Compaction summary files should be written to root, not tool_results/."""
        long_summary = "Long summary content. " * 50

        jsonl_content = [
            {
                "type": "user",
                "uuid": "u1",
                "message": {"content": "Start"},
                "timestamp": "2026-01-19T10:00:00.000Z",
                "cwd": "/project",
                "sessionId": "test-session",
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "c1",
                "content": "Compacted",
                "timestamp": "2026-01-19T10:30:00.000Z",
                "compactMetadata": {"trigger": "auto", "preTokens": 100000},
            },
            {
                "type": "user",
                "uuid": "u2",
                "message": {"content": long_summary},
                "timestamp": "2026-01-19T10:30:01.000Z",
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = Path(tmpdir) / "test-session.jsonl"
            with open(jsonl_path, "w") as f:
                for line in jsonl_content:
                    f.write(json.dumps(line) + "\n")

            export = export_session_to_markdown(jsonl_path)

            output_dir = Path(tmpdir) / "output"
            files = export_to_files(export, output_dir)

            # Check compaction file is at root level
            compaction_file = output_dir / "compaction_1_summary.txt"
            assert compaction_file.exists()
            assert compaction_file in files

            # Check it's NOT in tool_results/
            tool_results_compaction = output_dir / "tool_results" / "compaction_1_summary.txt"
            assert not tool_results_compaction.exists()
