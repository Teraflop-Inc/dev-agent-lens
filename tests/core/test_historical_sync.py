"""Tests for historical sync checkpoint/resume functionality."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dev_agent_lens.core.historical_sync import (
    DateRange,
    HistoricalSyncState,
    SyncConfig,
    SyncStats,
    clear_historical_sync,
    get_state_dir,
    list_historical_syncs,
)


class TestDateRange:
    """Tests for DateRange dataclass."""

    def test_to_dict(self):
        """Test serialization to dict."""
        dr = DateRange(
            start=datetime(2025, 1, 1, 0, 0, 0),
            end=datetime(2025, 1, 2, 0, 0, 0),
            spans=1000,
        )
        result = dr.to_dict()
        assert result["start"] == "2025-01-01 00:00:00"
        assert result["end"] == "2025-01-02 00:00:00"
        assert result["spans"] == 1000

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "start": "2025-01-01 00:00:00",
            "end": "2025-01-02 00:00:00",
            "spans": 500,
        }
        dr = DateRange.from_dict(data)
        assert dr.start == datetime(2025, 1, 1, 0, 0, 0)
        assert dr.end == datetime(2025, 1, 2, 0, 0, 0)
        assert dr.spans == 500

    def test_overlaps(self):
        """Test overlap detection."""
        r1 = DateRange(
            start=datetime(2025, 1, 1),
            end=datetime(2025, 1, 3),
        )
        r2 = DateRange(
            start=datetime(2025, 1, 2),
            end=datetime(2025, 1, 4),
        )
        r3 = DateRange(
            start=datetime(2025, 1, 5),
            end=datetime(2025, 1, 6),
        )

        assert r1.overlaps(r2)
        assert r2.overlaps(r1)
        assert not r1.overlaps(r3)
        assert not r3.overlaps(r1)

    def test_contains(self):
        """Test datetime containment."""
        dr = DateRange(
            start=datetime(2025, 1, 1),
            end=datetime(2025, 1, 3),
        )
        assert dr.contains(datetime(2025, 1, 2))
        assert dr.contains(datetime(2025, 1, 1))
        assert dr.contains(datetime(2025, 1, 3))
        assert not dr.contains(datetime(2025, 1, 4))


class TestSyncConfig:
    """Tests for SyncConfig."""

    def test_batch_duration_hours(self):
        """Test batch duration with hours."""
        config = SyncConfig(batch_hours=6)
        assert config.batch_duration == timedelta(hours=6)

    def test_batch_duration_days(self):
        """Test batch duration with days."""
        config = SyncConfig(batch_days=7)
        assert config.batch_duration == timedelta(days=7)

    def test_to_dict_from_dict(self):
        """Test round-trip serialization."""
        config = SyncConfig(
            batch_hours=12,
            batch_days=1,
            limit=10000,
            timeout=30,
            delay=0.5,
        )
        data = config.to_dict()
        restored = SyncConfig.from_dict(data)

        assert restored.batch_hours == config.batch_hours
        assert restored.batch_days == config.batch_days
        assert restored.limit == config.limit
        assert restored.timeout == config.timeout
        assert restored.delay == config.delay


class TestHistoricalSyncState:
    """Tests for HistoricalSyncState."""

    @pytest.fixture
    def temp_state_dir(self, tmp_path, monkeypatch):
        """Create a temporary state directory."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("DAL_STATE_PATH", str(state_dir))
        return state_dir

    def test_create_new_state(self, temp_state_dir):
        """Test creating a new state."""
        state = HistoricalSyncState(
            source="test-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )

        assert state.source == "test-source"
        assert state.target_start == datetime(2025, 1, 1)
        assert state.target_end == datetime(2025, 1, 31)
        assert state.is_complete is False
        assert state.progress_percent == 0.0

    def test_save_and_load(self, temp_state_dir):
        """Test saving and loading state."""
        state = HistoricalSyncState(
            source="test-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )
        state.stats.total_spans = 1000
        state.save()

        # Verify file exists
        assert (temp_state_dir / "historical-sync-test-source.json").exists()

        # Load and verify
        loaded = HistoricalSyncState.load("test-source")
        assert loaded is not None
        assert loaded.source == "test-source"
        assert loaded.stats.total_spans == 1000

    def test_load_nonexistent(self, temp_state_dir):
        """Test loading nonexistent state returns None."""
        result = HistoricalSyncState.load("nonexistent")
        assert result is None

    def test_mark_batch_completed(self, temp_state_dir):
        """Test marking a batch as completed."""
        state = HistoricalSyncState(
            source="test-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 10),
        )

        # Complete a batch
        state.mark_batch_completed(
            datetime(2025, 1, 8),
            datetime(2025, 1, 10),
            spans=5000,
        )

        assert len(state.completed_ranges) == 1
        assert state.stats.total_spans == 5000
        assert state.stats.batches_completed == 1
        assert state.progress_percent > 0

    def test_get_remaining_ranges(self, temp_state_dir):
        """Test getting remaining ranges to sync."""
        state = HistoricalSyncState(
            source="test-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 10),
        )

        # Complete first half
        state.mark_batch_completed(
            datetime(2025, 1, 1),
            datetime(2025, 1, 5),
            spans=5000,
        )

        remaining = state.get_remaining_ranges()
        assert len(remaining) == 1
        assert remaining[0][0] == datetime(2025, 1, 5)
        assert remaining[0][1] == datetime(2025, 1, 10)

    def test_is_complete(self, temp_state_dir):
        """Test completion detection."""
        state = HistoricalSyncState(
            source="test-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 10),
        )

        assert not state.is_complete

        # Complete the full range
        state.mark_batch_completed(
            datetime(2025, 1, 1),
            datetime(2025, 1, 10),
            spans=10000,
        )

        assert state.is_complete

    def test_load_or_create_new(self, temp_state_dir):
        """Test load_or_create creates new state."""
        state, is_resuming = HistoricalSyncState.load_or_create(
            source="new-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )

        assert not is_resuming
        assert state.source == "new-source"

    def test_load_or_create_resume(self, temp_state_dir):
        """Test load_or_create resumes existing state."""
        # Create and save state
        original = HistoricalSyncState(
            source="existing-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )
        original.stats.total_spans = 5000
        original.save()

        # Load it back
        state, is_resuming = HistoricalSyncState.load_or_create(
            source="existing-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )

        assert is_resuming
        assert state.stats.total_spans == 5000

    def test_delete(self, temp_state_dir):
        """Test deleting state file."""
        state = HistoricalSyncState(
            source="to-delete",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )
        state.save()

        assert state.state_file.exists()
        assert state.delete()
        assert not state.state_file.exists()

    def test_merge_adjacent_ranges(self, temp_state_dir):
        """Test that adjacent completed ranges are merged."""
        state = HistoricalSyncState(
            source="test-source",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 10),
        )

        # Complete two adjacent ranges
        state.mark_batch_completed(
            datetime(2025, 1, 1),
            datetime(2025, 1, 3),
            spans=3000,
        )
        state.mark_batch_completed(
            datetime(2025, 1, 3),
            datetime(2025, 1, 5),
            spans=2000,
        )

        # Should be merged into one range
        assert len(state.completed_ranges) == 1
        assert state.completed_ranges[0].start == datetime(2025, 1, 1)
        assert state.completed_ranges[0].end == datetime(2025, 1, 5)
        assert state.completed_ranges[0].spans == 5000


class TestListAndClear:
    """Tests for list and clear functions."""

    @pytest.fixture
    def temp_state_dir(self, tmp_path, monkeypatch):
        """Create a temporary state directory."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setenv("DAL_STATE_PATH", str(state_dir))
        return state_dir

    def test_list_historical_syncs(self, temp_state_dir):
        """Test listing all historical syncs."""
        # Create multiple states
        for name in ["source-a", "source-b"]:
            state = HistoricalSyncState(
                source=name,
                target_start=datetime(2025, 1, 1),
                target_end=datetime(2025, 1, 31),
            )
            state.save()

        syncs = list_historical_syncs()
        assert len(syncs) == 2
        sources = {s.source for s in syncs}
        assert "source-a" in sources
        assert "source-b" in sources

    def test_list_empty(self, temp_state_dir):
        """Test listing when no syncs exist."""
        syncs = list_historical_syncs()
        assert syncs == []

    def test_clear_historical_sync(self, temp_state_dir):
        """Test clearing a historical sync."""
        state = HistoricalSyncState(
            source="to-clear",
            target_start=datetime(2025, 1, 1),
            target_end=datetime(2025, 1, 31),
        )
        state.save()

        assert clear_historical_sync("to-clear")
        assert not clear_historical_sync("to-clear")  # Already cleared
        assert HistoricalSyncState.load("to-clear") is None
