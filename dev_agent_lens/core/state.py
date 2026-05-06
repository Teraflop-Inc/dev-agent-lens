"""
State Tracker Module

Provides state persistence for tracking sync operations across backends and sources.
State is stored in a human-readable JSON file at ~/.dal/data/state/sync_state.json.

Schema v1 (legacy):
    {
        "version": 1,
        "backends": {
            "phoenix-local": {"last_sync": "2025-01-01T00:00:00"},
            "arize-cloud": {"last_sync": "2025-01-01T00:00:00"}
        }
    }

Schema v2 (per-source):
    {
        "version": 2,
        "backends": {...},  # Kept for backward compatibility
        "sources": {
            "phoenix-alex": {
                "type": "phoenix",
                "last_sync": "2025-01-01T00:00:00",
                "span_count": 1000,
                "local_only": true
            },
            "arize-team": {
                "type": "arize",
                "last_sync": "2025-01-01T00:00:00",
                "span_count": 500,
                "local_only": false
            }
        }
    }
"""

from __future__ import annotations

import fcntl
import json
import os
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def get_default_data_path() -> Path:
    """Get the default data path for DAL state files."""
    env_path = os.getenv("DAL_DATA_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".dal" / "data"


@dataclass
class SourceState:
    """State information for a named source."""

    name: str
    source_type: str
    last_sync: datetime | None
    span_count: int = 0
    local_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.source_type,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
            "span_count": self.span_count,
            "local_only": self.local_only,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "SourceState":
        """Create from dictionary."""
        last_sync = None
        if data.get("last_sync"):
            try:
                last_sync = datetime.fromisoformat(data["last_sync"])
            except (ValueError, TypeError):
                pass

        return cls(
            name=name,
            source_type=data.get("type", "unknown"),
            last_sync=last_sync,
            span_count=data.get("span_count", 0),
            local_only=data.get("local_only", True),
        )


class SyncState:
    """
    Tracks synchronization state for multiple backends and sources.

    State is persisted to a JSON file and includes the last sync timestamp
    for each configured backend/source. The file is human-readable and can be
    manually edited if needed.

    Supports both v1 (legacy backends) and v2 (named sources) schemas.

    Args:
        data_path: Base path for DAL data files. Defaults to ~/.dal/data.

    Example:
        >>> state = SyncState()
        >>> state.set_last_sync("phoenix-local", datetime.now())
        >>> print(state.get_last_sync("phoenix-local"))
        >>>
        >>> # Named sources (v2)
        >>> state.set_source_state("phoenix-alex", "phoenix", datetime.now(), 100)
        >>> print(state.get_source_state("phoenix-alex"))
    """

    STATE_FILENAME = "sync_state.json"
    CURRENT_VERSION = 2

    def __init__(self, data_path: Path | str | None = None) -> None:
        if data_path is None:
            self._data_path = get_default_data_path()
        else:
            self._data_path = Path(data_path).expanduser()

        self._state_dir = self._data_path / "state"
        self._state_file = self._state_dir / self.STATE_FILENAME
        self._state: dict[str, Any] = {}

        # Ensure directory exists
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Load existing state
        self._load_state()

    def _load_state(self) -> None:
        """Load state from disk, handling missing or corrupted files."""
        if not self._state_file.exists():
            self._state = {"backends": {}, "sources": {}, "version": self.CURRENT_VERSION}
            return

        try:
            with open(self._state_file, "r") as f:
                # Use shared lock for reading
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    content = f.read()
                    if content.strip():
                        self._state = json.loads(content)
                    else:
                        self._state = {"backends": {}, "sources": {}, "version": self.CURRENT_VERSION}
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Validate and migrate state structure
            if "backends" not in self._state:
                self._state["backends"] = {}
            if "sources" not in self._state:
                self._state["sources"] = {}
            if "version" not in self._state:
                self._state["version"] = 1

            # Auto-upgrade to v2 if needed
            if self._state["version"] < self.CURRENT_VERSION:
                self._state["version"] = self.CURRENT_VERSION
                self._save_state()

        except (json.JSONDecodeError, IOError) as e:
            warnings.warn(
                f"Corrupted state file at {self._state_file}, resetting to empty state: {e}",
                UserWarning,
                stacklevel=2,
            )
            self._state = {"backends": {}, "sources": {}, "version": self.CURRENT_VERSION}
            self._save_state()

    def _save_state(self) -> None:
        """Save state to disk with file locking."""
        with open(self._state_file, "w") as f:
            # Use exclusive lock for writing
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(self._state, f, indent=2, default=str)
                f.write("\n")  # Trailing newline for human readability
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def get_last_sync(self, backend: str) -> datetime | None:
        """
        Get the last sync timestamp for a backend.

        Args:
            backend: The backend identifier (e.g., "phoenix-local", "arize-cloud").

        Returns:
            The datetime of the last sync, or None if never synced.
        """
        backend_state = self._state.get("backends", {}).get(backend)
        if backend_state is None:
            return None

        last_sync = backend_state.get("last_sync")
        if last_sync is None:
            return None

        # Parse ISO format timestamp
        try:
            return datetime.fromisoformat(last_sync)
        except (ValueError, TypeError):
            return None

    def set_last_sync(self, backend: str, timestamp: datetime) -> None:
        """
        Set the last sync timestamp for a backend.

        Args:
            backend: The backend identifier.
            timestamp: The timestamp of the sync operation.
        """
        if "backends" not in self._state:
            self._state["backends"] = {}

        if backend not in self._state["backends"]:
            self._state["backends"][backend] = {}

        self._state["backends"][backend]["last_sync"] = timestamp.isoformat()
        self._save_state()

    def get_all_backends(self) -> list[str]:
        """
        Get a list of all backends that have sync state.

        Returns:
            List of backend identifiers.
        """
        return list(self._state.get("backends", {}).keys())

    def clear_backend(self, backend: str) -> None:
        """
        Clear the sync state for a specific backend.

        Args:
            backend: The backend identifier to clear.
        """
        if backend in self._state.get("backends", {}):
            del self._state["backends"][backend]
            self._save_state()

    def clear_all(self) -> None:
        """Clear all sync state."""
        self._state = {"backends": {}, "sources": {}, "version": self.CURRENT_VERSION}
        self._save_state()

    def reload(self) -> None:
        """Reload state from disk."""
        self._load_state()

    @property
    def state_file(self) -> Path:
        """Get the path to the state file."""
        return self._state_file

    # --- Named Source Methods (v2) ---

    def get_source_state(self, source_name: str) -> SourceState | None:
        """
        Get the state for a named source.

        Args:
            source_name: The source identifier (e.g., "phoenix-alex").

        Returns:
            SourceState object, or None if source has no state.
        """
        source_data = self._state.get("sources", {}).get(source_name)
        if source_data is None:
            return None

        return SourceState.from_dict(source_name, source_data)

    def set_source_state(
        self,
        source_name: str,
        source_type: str,
        last_sync: datetime,
        span_count: int = 0,
        local_only: bool = True,
    ) -> None:
        """
        Set the state for a named source.

        Args:
            source_name: The source identifier.
            source_type: The source type ("phoenix" or "arize").
            last_sync: The timestamp of the sync operation.
            span_count: Number of spans synced.
            local_only: Whether this source is local-only.
        """
        if "sources" not in self._state:
            self._state["sources"] = {}

        source_state = SourceState(
            name=source_name,
            source_type=source_type,
            last_sync=last_sync,
            span_count=span_count,
            local_only=local_only,
        )

        self._state["sources"][source_name] = source_state.to_dict()
        self._save_state()

    def get_source_last_sync(self, source_name: str) -> datetime | None:
        """
        Get the last sync timestamp for a named source.

        Args:
            source_name: The source identifier.

        Returns:
            The datetime of the last sync, or None if never synced.
        """
        source_state = self.get_source_state(source_name)
        return source_state.last_sync if source_state else None

    def get_all_sources(self) -> list[str]:
        """
        Get a list of all sources that have sync state.

        Returns:
            List of source identifiers.
        """
        return list(self._state.get("sources", {}).keys())

    def clear_source(self, source_name: str) -> None:
        """
        Clear the sync state for a specific source.

        Args:
            source_name: The source identifier to clear.
        """
        if source_name in self._state.get("sources", {}):
            del self._state["sources"][source_name]
            self._save_state()

    def get_syncable_sources(self) -> list[SourceState]:
        """
        Get all sources that are not local-only (can be synced to Oxen).

        Returns:
            List of SourceState objects for non-local-only sources.
        """
        sources = []
        for name, data in self._state.get("sources", {}).items():
            source_state = SourceState.from_dict(name, data)
            if not source_state.local_only:
                sources.append(source_state)
        return sources

    def __repr__(self) -> str:
        backends = self.get_all_backends()
        sources = self.get_all_sources()
        return f"SyncState(backends={backends}, sources={sources}, path='{self._state_file}')"
