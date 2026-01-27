"""
Tests for ClaudeClient.

These tests verify the Claude client functionality including:
- Session discovery and listing
- Session reading
- Metadata extraction
- Error handling
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_agent_lens.clients.claude import ClaudeClient, ClaudeClientError, SessionInfo


class TestClaudeClientInit:
    """Tests for ClaudeClient initialization."""

    def test_default_values(self):
        """Given no arguments, client uses default Claude directory."""
        with patch.dict("os.environ", {}, clear=True):
            client = ClaudeClient()
            assert client.claude_dir == Path.home() / ".claude" / "projects"

    def test_env_var_override(self):
        """Given environment variable, client uses env value."""
        with patch.dict("os.environ", {"DAL_CLAUDE_DIR": "/custom/claude/dir"}):
            client = ClaudeClient()
            assert client.claude_dir == Path("/custom/claude/dir")

    def test_explicit_path_overrides_env(self):
        """Given explicit path, it overrides environment variable."""
        with patch.dict("os.environ", {"DAL_CLAUDE_DIR": "/env/path"}):
            client = ClaudeClient(claude_dir="/explicit/path")
            assert client.claude_dir == Path("/explicit/path")

    def test_path_accepts_string(self):
        """Given string path, client converts to Path."""
        client = ClaudeClient(claude_dir="/some/path")
        assert isinstance(client.claude_dir, Path)
        assert client.claude_dir == Path("/some/path")

    def test_path_accepts_path_object(self):
        """Given Path object, client uses it directly."""
        path = Path("/some/path")
        client = ClaudeClient(claude_dir=path)
        assert client.claude_dir == path


class TestClaudeClientListSessions:
    """Tests for session listing."""

    def test_list_sessions_empty_dir(self, tmp_path):
        """Given empty directory, returns empty list."""
        client = ClaudeClient(claude_dir=tmp_path)
        sessions = client.list_sessions()
        assert sessions == []

    def test_list_sessions_nonexistent_dir(self, tmp_path):
        """Given nonexistent directory, returns empty list."""
        client = ClaudeClient(claude_dir=tmp_path / "nonexistent")
        sessions = client.list_sessions()
        assert sessions == []

    def test_list_sessions_finds_files(self, tmp_path):
        """Given directory with sessions, lists them correctly."""
        # Create mock session files
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        (project_dir / "session-001.jsonl").write_text('{"type": "user"}\n')
        (project_dir / "session-002.jsonl").write_text('{"type": "user"}\n')

        client = ClaudeClient(claude_dir=tmp_path)
        sessions = client.list_sessions()

        assert len(sessions) == 2
        session_ids = [s.session_id for s in sessions]
        assert "session-001" in session_ids
        assert "session-002" in session_ids

    def test_list_sessions_includes_metadata(self, tmp_path):
        """Sessions include file metadata."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        session_file = project_dir / "session-001.jsonl"
        session_file.write_text('{"type": "user"}\n')

        client = ClaudeClient(claude_dir=tmp_path)
        sessions = client.list_sessions()

        assert len(sessions) == 1
        session = sessions[0]
        assert session.session_id == "session-001"
        assert session.file_path == session_file
        assert session.project_hash == "abc123"
        assert isinstance(session.modified_time, datetime)
        assert session.size_bytes > 0

    def test_list_sessions_sorted_by_modified_time(self, tmp_path):
        """Sessions sorted by modification time (newest first)."""
        import time

        project_dir = tmp_path / "abc123"
        project_dir.mkdir()

        # Create files with different modification times
        (project_dir / "old.jsonl").write_text('{"type": "user"}\n')
        time.sleep(0.01)  # Ensure different timestamps
        (project_dir / "new.jsonl").write_text('{"type": "user"}\n')

        client = ClaudeClient(claude_dir=tmp_path)
        sessions = client.list_sessions()

        assert sessions[0].session_id == "new"
        assert sessions[1].session_id == "old"

    def test_list_sessions_with_limit(self, tmp_path):
        """Given limit, returns only that many sessions."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        for i in range(5):
            (project_dir / f"session-{i:03d}.jsonl").write_text('{"type": "user"}\n')

        client = ClaudeClient(claude_dir=tmp_path)
        sessions = client.list_sessions(limit=3)

        assert len(sessions) == 3

    def test_list_sessions_filter_by_project_hash(self, tmp_path):
        """Given project_hash, returns only sessions from that project."""
        proj1 = tmp_path / "project1"
        proj2 = tmp_path / "project2"
        proj1.mkdir()
        proj2.mkdir()
        (proj1 / "session-1.jsonl").write_text('{"type": "user"}\n')
        (proj2 / "session-2.jsonl").write_text('{"type": "user"}\n')

        client = ClaudeClient(claude_dir=tmp_path)
        sessions = client.list_sessions(project_hash="project1")

        assert len(sessions) == 1
        assert sessions[0].session_id == "session-1"


class TestClaudeClientReadSession:
    """Tests for session reading."""

    def test_read_session_returns_messages(self, tmp_path):
        """Given valid session, returns parsed messages."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        session_file = project_dir / "session-001.jsonl"
        session_file.write_text(
            '{"type": "user", "uuid": "u1"}\n'
            '{"type": "assistant", "uuid": "a1"}\n'
        )

        client = ClaudeClient(claude_dir=tmp_path)
        messages = client.read_session("session-001")

        assert len(messages) == 2
        assert messages[0]["type"] == "user"
        assert messages[1]["type"] == "assistant"

    def test_read_session_not_found(self, tmp_path):
        """Given nonexistent session, raises ClaudeClientError."""
        client = ClaudeClient(claude_dir=tmp_path)

        with pytest.raises(ClaudeClientError, match="not found"):
            client.read_session("nonexistent")

    def test_read_session_skips_malformed_lines(self, tmp_path):
        """Given malformed JSONL, skips bad lines."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        session_file = project_dir / "session-001.jsonl"
        session_file.write_text(
            '{"type": "user"}\n'
            'not valid json\n'
            '{"type": "assistant"}\n'
        )

        client = ClaudeClient(claude_dir=tmp_path)
        messages = client.read_session("session-001")

        assert len(messages) == 2

    def test_read_session_handles_empty_lines(self, tmp_path):
        """Given empty lines in JSONL, skips them."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        session_file = project_dir / "session-001.jsonl"
        session_file.write_text(
            '{"type": "user"}\n'
            '\n'
            '{"type": "assistant"}\n'
        )

        client = ClaudeClient(claude_dir=tmp_path)
        messages = client.read_session("session-001")

        assert len(messages) == 2


class TestClaudeClientGetSessionMetadata:
    """Tests for metadata extraction."""

    def test_get_session_metadata_extracts_fields(self, tmp_path):
        """Given session with metadata, extracts all fields."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        session_file = project_dir / "session-001.jsonl"
        session_file.write_text(
            '{"type": "user", "cwd": "/project", "gitBranch": "main", "timestamp": "2026-01-19T10:00:00.000Z"}\n'
            '{"type": "assistant", "timestamp": "2026-01-19T10:00:01.000Z"}\n'
        )

        client = ClaudeClient(claude_dir=tmp_path)
        metadata = client.get_session_metadata("session-001")

        assert metadata["session_id"] == "session-001"
        assert metadata["project_path"] == "/project"
        assert metadata["git_branch"] == "main"
        assert metadata["message_count"] == 2
        assert "2026-01-19" in metadata["start_time"]
        assert "2026-01-19" in metadata["end_time"]

    def test_get_session_metadata_not_found(self, tmp_path):
        """Given nonexistent session, raises ClaudeClientError."""
        client = ClaudeClient(claude_dir=tmp_path)

        with pytest.raises(ClaudeClientError, match="not found"):
            client.get_session_metadata("nonexistent")

    def test_get_session_metadata_handles_missing_fields(self, tmp_path):
        """Given session without metadata fields, returns None for them."""
        project_dir = tmp_path / "abc123"
        project_dir.mkdir()
        session_file = project_dir / "session-001.jsonl"
        session_file.write_text('{"type": "user"}\n')

        client = ClaudeClient(claude_dir=tmp_path)
        metadata = client.get_session_metadata("session-001")

        assert metadata["project_path"] is None
        assert metadata["git_branch"] is None
        assert metadata["start_time"] is None


class TestClaudeClientTestConnection:
    """Tests for connection testing."""

    def test_connection_success(self, tmp_path):
        """Given existing directory, connection succeeds."""
        client = ClaudeClient(claude_dir=tmp_path)
        assert client.test_connection() is True

    def test_connection_failure(self, tmp_path):
        """Given nonexistent directory, connection fails."""
        client = ClaudeClient(claude_dir=tmp_path / "nonexistent")
        assert client.test_connection() is False


class TestClaudeClientRepr:
    """Tests for string representation."""

    def test_repr(self):
        """Given client, repr shows directory."""
        client = ClaudeClient(claude_dir="/test/path")
        result = repr(client)
        assert "/test/path" in result
        assert "ClaudeClient" in result


class TestClaudeClientWithFixtures:
    """Tests using fixture files."""

    @pytest.fixture
    def fixture_dir(self):
        """Path to test fixtures."""
        return Path(__file__).parent.parent / "fixtures"

    def test_read_minimal_session(self, fixture_dir, tmp_path):
        """Can read the minimal fixture session."""
        # Copy fixture to temp location with proper structure
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        fixture_content = (fixture_dir / "claude_session_minimal.jsonl").read_text()
        (project_dir / "session-001.jsonl").write_text(fixture_content)

        client = ClaudeClient(claude_dir=tmp_path)
        messages = client.read_session("session-001")

        assert len(messages) == 5
        assert messages[0]["type"] == "user"
        assert messages[1]["type"] == "assistant"

    def test_metadata_from_minimal_session(self, fixture_dir, tmp_path):
        """Can extract metadata from minimal fixture."""
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        fixture_content = (fixture_dir / "claude_session_minimal.jsonl").read_text()
        (project_dir / "session-001.jsonl").write_text(fixture_content)

        client = ClaudeClient(claude_dir=tmp_path)
        metadata = client.get_session_metadata("session-001")

        assert metadata["project_path"] == "/project"
        assert metadata["git_branch"] == "main"
        assert metadata["message_count"] == 5
