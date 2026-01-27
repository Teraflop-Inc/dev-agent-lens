"""
Claude Client Module

Provides a client for reading Claude Code session files from disk.
Sessions are stored in ~/.claude/projects/<project_hash>/<session_id>.jsonl
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class ClaudeClientError(Exception):
    """Raised when Claude session operations fail."""

    pass


@dataclass
class SessionInfo:
    """Information about a Claude session."""

    session_id: str
    """Session UUID (filename without .jsonl extension)."""

    file_path: Path
    """Full path to the session JSONL file."""

    project_hash: str
    """SHA-256 hash of the project path (parent directory name)."""

    modified_time: datetime
    """Last modification time of the session file."""

    size_bytes: int
    """Size of the session file in bytes."""

    # Optional metadata (populated by get_session_metadata)
    project_path: str | None = None
    """Original project path (cwd) if available."""

    git_branch: str | None = None
    """Git branch if available."""

    message_count: int | None = None
    """Number of messages in the session if scanned."""


class ClaudeClient:
    """
    Client for reading Claude Code session files.

    Reads sessions from the Claude Code session storage directory,
    typically ~/.claude/projects/<project_hash>/<session_id>.jsonl

    Args:
        claude_dir: Base directory for Claude projects. Defaults to
            DAL_CLAUDE_DIR environment variable or ~/.claude/projects.

    Example:
        >>> client = ClaudeClient()
        >>> sessions = client.list_sessions()
        >>> for session in sessions[:5]:
        ...     print(f"{session.session_id}: {session.size_bytes} bytes")
    """

    def __init__(self, claude_dir: str | Path | None = None) -> None:
        if claude_dir is not None:
            self.claude_dir = Path(claude_dir)
        else:
            env_dir = os.getenv("DAL_CLAUDE_DIR")
            if env_dir:
                self.claude_dir = Path(env_dir).expanduser()
            else:
                self.claude_dir = Path.home() / ".claude" / "projects"

    def _iter_session_files(self) -> Iterator[Path]:
        """Iterate over all session JSONL files in the claude directory."""
        if not self.claude_dir.exists():
            return

        # Walk all project hash directories
        for project_dir in self.claude_dir.iterdir():
            if not project_dir.is_dir():
                continue
            # Find all .jsonl files in the project directory
            for session_file in project_dir.glob("*.jsonl"):
                if session_file.is_file():
                    yield session_file

    def list_sessions(
        self,
        limit: int | None = None,
        project_hash: str | None = None,
    ) -> list[SessionInfo]:
        """
        List all available Claude sessions.

        Args:
            limit: Maximum number of sessions to return (most recent first).
            project_hash: Filter to sessions from a specific project hash.

        Returns:
            List of SessionInfo objects sorted by modification time (newest first).

        Raises:
            ClaudeClientError: If the claude directory is not accessible.
        """
        if not self.claude_dir.exists():
            return []

        sessions = []

        for session_file in self._iter_session_files():
            # Extract project hash from parent directory
            file_project_hash = session_file.parent.name

            # Filter by project hash if specified
            if project_hash and file_project_hash != project_hash:
                continue

            try:
                stat = session_file.stat()
                sessions.append(
                    SessionInfo(
                        session_id=session_file.stem,
                        file_path=session_file,
                        project_hash=file_project_hash,
                        modified_time=datetime.fromtimestamp(stat.st_mtime),
                        size_bytes=stat.st_size,
                    )
                )
            except OSError:
                # Skip files we can't stat
                continue

        # Sort by modification time (newest first)
        sessions.sort(key=lambda s: s.modified_time, reverse=True)

        if limit:
            sessions = sessions[:limit]

        return sessions

    def read_session(self, session_id: str) -> list[dict[str, Any]]:
        """
        Read a session's JSONL messages.

        Args:
            session_id: The session UUID (filename without .jsonl).

        Returns:
            List of message dictionaries parsed from the JSONL file.

        Raises:
            ClaudeClientError: If the session is not found or cannot be read.
        """
        session_file = self._find_session_file(session_id)
        if session_file is None:
            raise ClaudeClientError(f"Session not found: {session_id}")

        return list(self._parse_jsonl_file(session_file))

    def read_session_raw(self, session_id: str) -> str:
        """
        Read a session's raw JSONL content.

        Args:
            session_id: The session UUID (filename without .jsonl).

        Returns:
            Raw JSONL content as a string.

        Raises:
            ClaudeClientError: If the session is not found or cannot be read.
        """
        session_file = self._find_session_file(session_id)
        if session_file is None:
            raise ClaudeClientError(f"Session not found: {session_id}")

        try:
            return session_file.read_text(encoding="utf-8")
        except OSError as e:
            raise ClaudeClientError(f"Failed to read session {session_id}: {e}") from e

    def get_session_metadata(self, session_id: str) -> dict[str, Any]:
        """
        Get session metadata (project_path, git_branch, timestamps, etc.).

        Scans the first few messages to extract metadata without reading
        the entire session.

        Args:
            session_id: The session UUID (filename without .jsonl).

        Returns:
            Dictionary with session metadata including:
            - session_id: The session UUID
            - project_path: Original working directory (cwd)
            - git_branch: Git branch if available
            - start_time: First message timestamp
            - end_time: Last message timestamp
            - message_count: Total number of messages

        Raises:
            ClaudeClientError: If the session is not found or cannot be read.
        """
        session_file = self._find_session_file(session_id)
        if session_file is None:
            raise ClaudeClientError(f"Session not found: {session_id}")

        metadata = {
            "session_id": session_id,
            "file_path": str(session_file),
            "project_hash": session_file.parent.name,
            "project_path": None,
            "git_branch": None,
            "start_time": None,
            "end_time": None,
            "message_count": 0,
        }

        first_timestamp = None
        last_timestamp = None

        for msg in self._parse_jsonl_file(session_file):
            metadata["message_count"] += 1

            # Extract cwd and git_branch from first message that has them
            if metadata["project_path"] is None and msg.get("cwd"):
                metadata["project_path"] = msg["cwd"]
            if metadata["git_branch"] is None and msg.get("gitBranch"):
                metadata["git_branch"] = msg["gitBranch"]

            # Track timestamps
            ts = msg.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if first_timestamp is None:
                        first_timestamp = dt
                    last_timestamp = dt
                except (ValueError, TypeError):
                    pass

        metadata["start_time"] = first_timestamp.isoformat() if first_timestamp else None
        metadata["end_time"] = last_timestamp.isoformat() if last_timestamp else None

        return metadata

    def get_session_file_path(self, session_id: str) -> Path | None:
        """
        Get the file path for a session.

        Args:
            session_id: The session UUID (filename without .jsonl).

        Returns:
            Path to the session file, or None if not found.
        """
        return self._find_session_file(session_id)

    def _find_session_file(self, session_id: str) -> Path | None:
        """Find a session file by ID, searching all project directories."""
        if not self.claude_dir.exists():
            return None

        for session_file in self._iter_session_files():
            if session_file.stem == session_id:
                return session_file

        return None

    def _parse_jsonl_file(self, file_path: Path) -> Iterator[dict[str, Any]]:
        """Parse a JSONL file, yielding each line as a dict."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass  # Skip malformed lines
        except OSError as e:
            raise ClaudeClientError(f"Failed to read file {file_path}: {e}") from e

    def test_connection(self) -> bool:
        """
        Test if the Claude sessions directory is accessible.

        Returns:
            True if directory exists and is readable, False otherwise.
        """
        return self.claude_dir.exists() and self.claude_dir.is_dir()

    def __repr__(self) -> str:
        return f"ClaudeClient(claude_dir='{self.claude_dir}')"
