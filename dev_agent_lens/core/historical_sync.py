"""
Historical Sync State Module

Manages checkpoint/resume state for historical sync operations.
This allows interrupted syncs to resume from where they left off.

Storage:
    ~/.dal/state/historical-sync-{source}.json

State Format:
    {
        "version": 1,
        "source": "arize-ax-alex",
        "started_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T12:00:00",
        "target_range": {
            "start": "2025-11-01",
            "end": "2025-12-31"
        },
        "completed_ranges": [
            {"start": "2025-12-25", "end": "2025-12-31", "spans": 45000},
            {"start": "2025-12-18", "end": "2025-12-25", "spans": 52000}
        ],
        "current_batch": {
            "start": "2025-12-11",
            "end": "2025-12-18"
        },
        "stats": {
            "total_spans": 97000,
            "batches_completed": 2,
            "batches_failed": 0,
            "subdivisions": 1
        },
        "config": {
            "batch_hours": null,
            "batch_days": 7,
            "limit": 50000
        }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def get_state_dir() -> Path:
    """Get the state directory for historical sync."""
    env_path = os.getenv("DAL_STATE_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".dal" / "state"


@dataclass
class DateRange:
    """A date range with span count."""

    start: datetime
    end: datetime
    spans: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": self.end.strftime("%Y-%m-%d %H:%M:%S"),
            "spans": self.spans,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DateRange":
        return cls(
            start=datetime.strptime(data["start"], "%Y-%m-%d %H:%M:%S"),
            end=datetime.strptime(data["end"], "%Y-%m-%d %H:%M:%S"),
            spans=data.get("spans", 0),
        )

    def overlaps(self, other: "DateRange") -> bool:
        """Check if this range overlaps with another."""
        return self.start < other.end and self.end > other.start

    def contains(self, dt: datetime) -> bool:
        """Check if a datetime is within this range."""
        return self.start <= dt <= self.end


@dataclass
class SyncStats:
    """Statistics for a historical sync."""

    total_spans: int = 0
    batches_completed: int = 0
    batches_failed: int = 0
    subdivisions: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total_spans": self.total_spans,
            "batches_completed": self.batches_completed,
            "batches_failed": self.batches_failed,
            "subdivisions": self.subdivisions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncStats":
        return cls(
            total_spans=data.get("total_spans", 0),
            batches_completed=data.get("batches_completed", 0),
            batches_failed=data.get("batches_failed", 0),
            subdivisions=data.get("subdivisions", 0),
        )


@dataclass
class SyncConfig:
    """Configuration for a historical sync."""

    batch_hours: int | None = None
    batch_days: int = 7
    limit: int = 50000
    timeout: int = 60
    delay: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_hours": self.batch_hours,
            "batch_days": self.batch_days,
            "limit": self.limit,
            "timeout": self.timeout,
            "delay": self.delay,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncConfig":
        return cls(
            batch_hours=data.get("batch_hours"),
            batch_days=data.get("batch_days", 7),
            limit=data.get("limit", 50000),
            timeout=data.get("timeout", 60),
            delay=data.get("delay", 1.0),
        )

    @property
    def batch_duration(self) -> timedelta:
        """Get batch duration as timedelta."""
        if self.batch_hours:
            return timedelta(hours=self.batch_hours)
        return timedelta(days=self.batch_days)


@dataclass
class HistoricalSyncState:
    """
    State for a historical sync operation.

    Tracks progress and allows resume after interruption.
    """

    source: str
    target_start: datetime
    target_end: datetime
    completed_ranges: list[DateRange] = field(default_factory=list)
    current_batch: DateRange | None = None
    stats: SyncStats = field(default_factory=SyncStats)
    config: SyncConfig = field(default_factory=SyncConfig)
    started_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def state_file(self) -> Path:
        """Get path to the state file for this source."""
        return get_state_dir() / f"historical-sync-{self.source}.json"

    @property
    def is_complete(self) -> bool:
        """Check if the entire target range has been synced."""
        if not self.completed_ranges:
            return False

        # Check if completed ranges cover the entire target range
        return self._covers_range(self.target_start, self.target_end)

    @property
    def progress_percent(self) -> float:
        """Calculate progress as percentage."""
        if not self.completed_ranges:
            return 0.0

        total_duration = (self.target_end - self.target_start).total_seconds()
        if total_duration <= 0:
            return 100.0

        completed_seconds = sum(
            (r.end - r.start).total_seconds()
            for r in self.completed_ranges
        )
        return min(100.0, (completed_seconds / total_duration) * 100)

    def _covers_range(self, start: datetime, end: datetime) -> bool:
        """Check if completed ranges fully cover the given range."""
        if not self.completed_ranges:
            return False

        # Sort ranges by start time
        sorted_ranges = sorted(self.completed_ranges, key=lambda r: r.start)

        # Check for gaps
        current_pos = start
        for r in sorted_ranges:
            if r.start > current_pos:
                # Gap found
                return False
            current_pos = max(current_pos, r.end)

        return current_pos >= end

    def get_remaining_ranges(self) -> list[tuple[datetime, datetime]]:
        """Get date ranges that still need to be synced."""
        if not self.completed_ranges:
            return [(self.target_start, self.target_end)]

        # Sort completed ranges by start time
        sorted_ranges = sorted(self.completed_ranges, key=lambda r: r.start)

        remaining = []
        current_pos = self.target_start

        for r in sorted_ranges:
            if r.start > current_pos:
                # Gap before this range
                remaining.append((current_pos, r.start))
            current_pos = max(current_pos, r.end)

        # Check for remaining time after last completed range
        if current_pos < self.target_end:
            remaining.append((current_pos, self.target_end))

        return remaining

    def mark_batch_started(self, start: datetime, end: datetime) -> None:
        """Mark a batch as started (in progress)."""
        self.current_batch = DateRange(start=start, end=end)
        self.updated_at = datetime.now()
        self.save()

    def mark_batch_completed(self, start: datetime, end: datetime, spans: int) -> None:
        """Mark a batch as completed."""
        completed_range = DateRange(start=start, end=end, spans=spans)

        # Merge with existing ranges if they overlap
        self._merge_completed_range(completed_range)

        self.current_batch = None
        self.stats.total_spans += spans
        self.stats.batches_completed += 1
        self.updated_at = datetime.now()
        self.save()

    def mark_batch_failed(self) -> None:
        """Mark the current batch as failed."""
        self.current_batch = None
        self.stats.batches_failed += 1
        self.updated_at = datetime.now()
        self.save()

    def add_subdivision(self) -> None:
        """Increment subdivision count."""
        self.stats.subdivisions += 1

    def _merge_completed_range(self, new_range: DateRange) -> None:
        """Merge a new completed range with existing ones."""
        # Find overlapping or adjacent ranges
        non_overlapping = []
        merged = new_range

        for r in self.completed_ranges:
            # Check if ranges are adjacent (within 1 second) or overlapping
            if r.end >= merged.start and r.start <= merged.end:
                # Merge them
                merged = DateRange(
                    start=min(r.start, merged.start),
                    end=max(r.end, merged.end),
                    spans=r.spans + merged.spans,
                )
            else:
                non_overlapping.append(r)

        non_overlapping.append(merged)
        self.completed_ranges = non_overlapping

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "version": 1,
            "source": self.source,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "target_range": {
                "start": self.target_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": self.target_end.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "completed_ranges": [r.to_dict() for r in self.completed_ranges],
            "current_batch": self.current_batch.to_dict() if self.current_batch else None,
            "stats": self.stats.to_dict(),
            "config": self.config.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoricalSyncState":
        """Create from dictionary."""
        target_range = data.get("target_range", {})

        return cls(
            source=data["source"],
            target_start=datetime.strptime(target_range["start"], "%Y-%m-%d %H:%M:%S"),
            target_end=datetime.strptime(target_range["end"], "%Y-%m-%d %H:%M:%S"),
            completed_ranges=[
                DateRange.from_dict(r) for r in data.get("completed_ranges", [])
            ],
            current_batch=(
                DateRange.from_dict(data["current_batch"])
                if data.get("current_batch")
                else None
            ),
            stats=SyncStats.from_dict(data.get("stats", {})),
            config=SyncConfig.from_dict(data.get("config", {})),
            started_at=datetime.fromisoformat(data.get("started_at", datetime.now().isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now().isoformat())),
        )

    def save(self) -> None:
        """Save state to disk."""
        state_dir = get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)

        with open(self.state_file, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
            f.write("\n")

    @classmethod
    def load(cls, source: str) -> "HistoricalSyncState | None":
        """Load state from disk, or return None if not found."""
        state_file = get_state_dir() / f"historical-sync-{source}.json"

        if not state_file.exists():
            return None

        try:
            with open(state_file, "r") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    @classmethod
    def load_or_create(
        cls,
        source: str,
        target_start: datetime,
        target_end: datetime,
        config: SyncConfig | None = None,
    ) -> tuple["HistoricalSyncState", bool]:
        """
        Load existing state or create new one.

        Returns:
            Tuple of (state, is_resuming) where is_resuming is True if
            we loaded existing state.
        """
        existing = cls.load(source)

        if existing is not None:
            # Check if target range matches
            if (existing.target_start == target_start and
                existing.target_end == target_end):
                return existing, True

            # Range doesn't match - could extend or reset
            # For now, reset if range is different
            pass

        # Create new state
        new_state = cls(
            source=source,
            target_start=target_start,
            target_end=target_end,
            config=config or SyncConfig(),
        )
        new_state.save()
        return new_state, False

    def delete(self) -> bool:
        """Delete the state file."""
        if self.state_file.exists():
            self.state_file.unlink()
            return True
        return False

    def get_eta(self) -> timedelta | None:
        """Estimate time remaining based on current progress."""
        if self.stats.batches_completed == 0:
            return None

        elapsed = (self.updated_at - self.started_at).total_seconds()
        if elapsed <= 0:
            return None

        progress = self.progress_percent
        if progress <= 0:
            return None

        # Estimate remaining time based on progress rate
        remaining_percent = 100 - progress
        seconds_per_percent = elapsed / progress
        remaining_seconds = remaining_percent * seconds_per_percent

        return timedelta(seconds=remaining_seconds)


def list_historical_syncs() -> list[HistoricalSyncState]:
    """List all historical sync states."""
    state_dir = get_state_dir()
    if not state_dir.exists():
        return []

    states = []
    for f in state_dir.glob("historical-sync-*.json"):
        try:
            with open(f, "r") as file:
                data = json.load(file)
            states.append(HistoricalSyncState.from_dict(data))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    return states


def clear_historical_sync(source: str) -> bool:
    """Clear historical sync state for a source."""
    state = HistoricalSyncState.load(source)
    if state:
        return state.delete()
    return False
