"""
Tests for State Tracker.

These tests verify sync state persistence including:
- First run behavior
- State persistence
- Multiple backends
- Corrupted state handling
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest

from dev_agent_lens.core.state import SyncState, get_default_data_path


class TestSyncStateFirstRun:
    """Tests for first run (no existing state)."""

    def test_no_state_file_returns_none(self, tmp_path):
        """Given no state file, get_last_sync returns None."""
        state = SyncState(data_path=tmp_path)
        result = state.get_last_sync("phoenix-local")
        assert result is None

    def test_creates_state_directory(self, tmp_path):
        """Given no state directory, creates it on init."""
        data_path = tmp_path / "new_dal"
        state = SyncState(data_path=data_path)

        assert (data_path / "state").exists()
        assert state.state_file.parent.exists()

    def test_empty_backends_list_on_first_run(self, tmp_path):
        """Given first run, get_all_backends returns empty list."""
        state = SyncState(data_path=tmp_path)
        result = state.get_all_backends()
        assert result == []


class TestSyncStateSetGet:
    """Tests for setting and getting sync state."""

    def test_set_and_get_sync_time(self, tmp_path):
        """Given set sync time, get returns that time."""
        state = SyncState(data_path=tmp_path)
        now = datetime.now()

        state.set_last_sync("phoenix-local", now)
        result = state.get_last_sync("phoenix-local")

        assert result is not None
        # Compare to within a second (ISO parsing may lose microseconds)
        assert abs((result - now).total_seconds()) < 1

    def test_get_after_set_returns_exact_time(self, tmp_path):
        """Given set sync time, get returns matching timestamp."""
        state = SyncState(data_path=tmp_path)
        now = datetime(2025, 1, 15, 10, 30, 0)

        state.set_last_sync("arize-cloud", now)
        result = state.get_last_sync("arize-cloud")

        assert result == now

    def test_update_existing_backend(self, tmp_path):
        """Given existing backend, update replaces the timestamp."""
        state = SyncState(data_path=tmp_path)
        old_time = datetime(2025, 1, 1)
        new_time = datetime(2025, 1, 15)

        state.set_last_sync("phoenix-local", old_time)
        state.set_last_sync("phoenix-local", new_time)
        result = state.get_last_sync("phoenix-local")

        assert result == new_time


class TestSyncStatePersistence:
    """Tests for state persistence across restarts."""

    def test_state_persists_after_reload(self, tmp_path):
        """Given saved state, reloading preserves data."""
        state1 = SyncState(data_path=tmp_path)
        now = datetime(2025, 1, 15, 10, 30, 0)
        state1.set_last_sync("phoenix-local", now)

        # Create new instance (simulates restart)
        state2 = SyncState(data_path=tmp_path)
        result = state2.get_last_sync("phoenix-local")

        assert result == now

    def test_state_file_is_human_readable_json(self, tmp_path):
        """Given saved state, file is valid JSON with indentation."""
        state = SyncState(data_path=tmp_path)
        state.set_last_sync("phoenix-local", datetime(2025, 1, 15))

        # Read the file directly
        with open(state.state_file, "r") as f:
            content = f.read()

        # Should be valid JSON
        data = json.loads(content)
        assert "backends" in data
        assert "phoenix-local" in data["backends"]

        # Should be formatted (contains newlines from indent)
        assert "\n" in content

    def test_reload_updates_state(self, tmp_path):
        """Given modified file, reload picks up changes."""
        state = SyncState(data_path=tmp_path)
        state.set_last_sync("phoenix-local", datetime(2025, 1, 1))

        # Manually modify the file
        with open(state.state_file, "w") as f:
            json.dump(
                {
                    "version": 1,
                    "backends": {
                        "phoenix-local": {"last_sync": "2025-01-15T12:00:00"}
                    },
                },
                f,
            )

        state.reload()
        result = state.get_last_sync("phoenix-local")

        assert result == datetime(2025, 1, 15, 12, 0, 0)


class TestSyncStateMultipleBackends:
    """Tests for multiple backend support."""

    def test_track_multiple_backends(self, tmp_path):
        """Given multiple backends, tracks each separately."""
        state = SyncState(data_path=tmp_path)
        time1 = datetime(2025, 1, 1)
        time2 = datetime(2025, 1, 15)

        state.set_last_sync("phoenix-local", time1)
        state.set_last_sync("arize-cloud", time2)

        assert state.get_last_sync("phoenix-local") == time1
        assert state.get_last_sync("arize-cloud") == time2

    def test_get_all_backends(self, tmp_path):
        """Given multiple backends, lists all of them."""
        state = SyncState(data_path=tmp_path)
        state.set_last_sync("phoenix-local", datetime.now())
        state.set_last_sync("arize-cloud", datetime.now())
        state.set_last_sync("phoenix-staging", datetime.now())

        backends = state.get_all_backends()

        assert len(backends) == 3
        assert "phoenix-local" in backends
        assert "arize-cloud" in backends
        assert "phoenix-staging" in backends

    def test_clear_backend_removes_only_that_backend(self, tmp_path):
        """Given clear_backend, only removes specified backend."""
        state = SyncState(data_path=tmp_path)
        state.set_last_sync("phoenix-local", datetime.now())
        state.set_last_sync("arize-cloud", datetime.now())

        state.clear_backend("phoenix-local")

        assert state.get_last_sync("phoenix-local") is None
        assert state.get_last_sync("arize-cloud") is not None

    def test_clear_all_removes_all_backends(self, tmp_path):
        """Given clear_all, removes all backend state."""
        state = SyncState(data_path=tmp_path)
        state.set_last_sync("phoenix-local", datetime.now())
        state.set_last_sync("arize-cloud", datetime.now())

        state.clear_all()

        assert state.get_all_backends() == []
        assert state.get_last_sync("phoenix-local") is None
        assert state.get_last_sync("arize-cloud") is None


class TestSyncStateCorruptedFile:
    """Tests for corrupted state file handling."""

    def test_corrupted_json_resets_to_empty(self, tmp_path):
        """Given corrupted JSON file, resets to empty state with warning."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "sync_state.json"

        # Write corrupted JSON
        with open(state_file, "w") as f:
            f.write("{ invalid json }")

        # Should warn and reset
        with pytest.warns(UserWarning, match="Corrupted state file"):
            state = SyncState(data_path=tmp_path)

        # Should have empty state
        assert state.get_all_backends() == []
        assert state.get_last_sync("phoenix-local") is None

    def test_empty_file_treated_as_first_run(self, tmp_path):
        """Given empty file, treats as first run."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "sync_state.json"

        # Create empty file
        state_file.touch()

        state = SyncState(data_path=tmp_path)

        assert state.get_all_backends() == []

    def test_missing_backends_key_handled(self, tmp_path):
        """Given JSON without backends key, handles gracefully."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "sync_state.json"

        # Write valid JSON without backends
        with open(state_file, "w") as f:
            json.dump({"version": 1}, f)

        state = SyncState(data_path=tmp_path)

        assert state.get_all_backends() == []
        # Should still be able to set
        state.set_last_sync("phoenix-local", datetime.now())
        assert state.get_last_sync("phoenix-local") is not None


class TestSyncStateEnvVar:
    """Tests for environment variable configuration."""

    def test_dal_data_path_env_var(self, tmp_path, monkeypatch):
        """Given DAL_DATA_PATH env var, uses that path."""
        custom_path = tmp_path / "custom_dal"
        monkeypatch.setenv("DAL_DATA_PATH", str(custom_path))

        # Use default (should pick up env var)
        from dev_agent_lens.core.state import get_default_data_path

        result = get_default_data_path()

        assert result == custom_path


class TestSyncStateRepr:
    """Tests for string representation."""

    def test_repr_shows_backends_and_path(self, tmp_path):
        """Given state, repr shows backends and path."""
        state = SyncState(data_path=tmp_path)
        state.set_last_sync("phoenix-local", datetime.now())

        result = repr(state)

        assert "phoenix-local" in result
        assert "SyncState" in result
