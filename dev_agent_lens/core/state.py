"""
State Tracker Module

Provides state persistence for tracking sync operations across backends.
State is stored in a human-readable JSON file at ~/.dal/data/state/sync_state.json.
"""

from __future__ import annotations

import fcntl
import json
import os
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any


def get_default_data_path() -> Path:
    """Get the default data path for DAL state files."""
    env_path = os.getenv("DAL_DATA_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".dal" / "data"


class SyncState:
    """
    Tracks synchronization state for multiple backends.

    State is persisted to a JSON file and includes the last sync timestamp
    for each configured backend. The file is human-readable and can be
    manually edited if needed.

    Args:
        data_path: Base path for DAL data files. Defaults to ~/.dal/data.

    Example:
        >>> state = SyncState()
        >>> state.set_last_sync("phoenix-local", datetime.now())
        >>> print(state.get_last_sync("phoenix-local"))
    """

    STATE_FILENAME = "sync_state.json"

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
            self._state = {"backends": {}, "version": 1}
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
                        self._state = {"backends": {}, "version": 1}
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Validate state structure
            if "backends" not in self._state:
                self._state["backends"] = {}
            if "version" not in self._state:
                self._state["version"] = 1

        except (json.JSONDecodeError, IOError) as e:
            warnings.warn(
                f"Corrupted state file at {self._state_file}, resetting to empty state: {e}",
                UserWarning,
                stacklevel=2,
            )
            self._state = {"backends": {}, "version": 1}
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
        self._state = {"backends": {}, "version": 1}
        self._save_state()

    def reload(self) -> None:
        """Reload state from disk."""
        self._load_state()

    @property
    def state_file(self) -> Path:
        """Get the path to the state file."""
        return self._state_file

    def __repr__(self) -> str:
        backends = self.get_all_backends()
        return f"SyncState(backends={backends}, path='{self._state_file}')"
