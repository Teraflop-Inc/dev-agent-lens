"""
Historical Sync State Module

Manages checkpoint/resume state for historical sync operations.
This allows interrupted syncs to resume from where they left off.

Storage:
    ~/.dal/state/historical-sync-{source}.json

State Format (v4):
    {
        "version": 4,
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
        "partial_ranges": {
            "2025-12-11 00:00:00": [
                {"start": "2025-12-11 00:00:00", "end": "2025-12-11 06:00:00", "spans": 5000},
                {"start": "2025-12-11 06:00:00", "end": "2025-12-11 12:00:00", "spans": 6000}
            ]
        },
        "current_batch": {
            "start": "2025-12-11",
            "end": "2025-12-18"
        },
        "current_run": {
            "run_id": "arize-ax-alex-20250101-120000-a7b3",
            "pid": 12345,
            "started_at": "2025-01-01T12:00:00"
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

Changes in v4:
    - Added partial_ranges for tracking successful sub-batches within failed days
    - Atomic state writes to prevent corruption on crash
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Default staleness threshold (5 minutes) - if updated_at is older than this
# and current_batch is set, the sync is considered stale (process likely died)
DEFAULT_STALENESS_THRESHOLD_SECONDS = 300

logger = logging.getLogger(__name__)


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
    retries_attempted: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total_spans": self.total_spans,
            "batches_completed": self.batches_completed,
            "batches_failed": self.batches_failed,
            "subdivisions": self.subdivisions,
            "retries_attempted": self.retries_attempted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncStats":
        return cls(
            total_spans=data.get("total_spans", 0),
            batches_completed=data.get("batches_completed", 0),
            batches_failed=data.get("batches_failed", 0),
            subdivisions=data.get("subdivisions", 0),
            retries_attempted=data.get("retries_attempted", 0),
        )


@dataclass
class SyncConfig:
    """Configuration for a historical sync."""

    batch_hours: int | None = None
    batch_days: int = 7
    limit: int | None = 50000  # None means unlimited (SQLite mode)
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
            limit=data.get("limit", 50000),  # Default to 50000 for backward compat
            timeout=data.get("timeout", 60),
            delay=data.get("delay", 1.0),
        )

    @property
    def batch_duration(self) -> timedelta:
        """Get batch duration as timedelta."""
        if self.batch_hours:
            return timedelta(hours=self.batch_hours)
        return timedelta(days=self.batch_days)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks if process exists
        return True
    except ProcessLookupError:
        return False  # Process doesn't exist
    except PermissionError:
        return True  # Process exists but we don't have permission to signal it


def generate_run_id(source: str) -> str:
    """Generate a unique run ID for a sync operation.

    Format: {source}-{YYYYMMDD}-{HHMMSS}-{random_hex}
    Example: phoenix-local-alex-20260106-143022-a7b3
    """
    now = datetime.now()
    random_suffix = secrets.token_hex(2)  # 4 hex chars
    return f"{source}-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}-{random_suffix}"


@dataclass
class SyncRun:
    """Information about a currently running sync process.

    Tracks the PID and run_id to enable:
    - Staleness detection (PID check + updated_at threshold)
    - Out-of-band status queries (reference by run_id)
    - Audit trail (who started when)
    """

    run_id: str
    pid: int
    started_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "started_at": self.started_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncRun":
        return cls(
            run_id=data["run_id"],
            pid=data["pid"],
            started_at=datetime.fromisoformat(data.get("started_at", datetime.now().isoformat())),
        )

    def is_alive(self) -> bool:
        """Check if the process that started this run is still running."""
        return _is_pid_alive(self.pid)


class SyncStatus:
    """Enum-like class for sync status."""
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"  # Finished pass but has gaps from failures
    IN_PROGRESS = "in_progress"
    STALE = "stale"  # Process died with current_batch set (stale state)
    PAUSED = "paused"


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
    failed_ranges: list[DateRange] = field(default_factory=list)  # Track failed batches for retry
    partial_ranges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # Track successful sub-batches within failed days
    batch_failures: dict[str, int] = field(default_factory=dict)  # Track consecutive failures per batch (key: batch_key)
    current_batch: DateRange | None = None
    current_run: SyncRun | None = None  # Track the running process (PID, run_id)
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
    def has_gaps(self) -> bool:
        """Check if there are gaps in the completed ranges (from failed batches)."""
        if self.is_complete:
            return False
        # If we have some completed ranges but the target isn't covered, we have gaps
        return len(self.completed_ranges) > 0 and len(self.get_remaining_ranges()) > 0

    def is_stale(self, threshold_seconds: int = DEFAULT_STALENESS_THRESHOLD_SECONDS) -> bool:
        """Check if the sync state appears stale (process likely died).

        A sync is considered stale if:
        1. current_batch is set (meaning it was mid-sync), AND
        2. Either:
           a. current_run exists and the PID is no longer alive, OR
           b. current_run doesn't exist and updated_at is older than threshold

        Args:
            threshold_seconds: How many seconds since last update before
                             considering the state stale (default: 5 minutes)

        Returns:
            True if the state appears stale, False otherwise.
        """
        if not self.current_batch:
            return False

        # If we have run tracking, check PID first
        if self.current_run:
            return not self.current_run.is_alive()

        # Fallback to timestamp-based staleness detection for v2 state files
        # (which don't have current_run)
        time_since_update = (datetime.now() - self.updated_at).total_seconds()
        return time_since_update > threshold_seconds

    def get_status(self) -> str:
        """Get the current sync status.

        Returns one of:
        - SyncStatus.COMPLETE: Target range fully covered
        - SyncStatus.INCOMPLETE: Pass finished but has gaps from failed batches
        - SyncStatus.IN_PROGRESS: Currently syncing (has current_batch and process alive)
        - SyncStatus.STALE: Process died with current_batch set (stale state)
        - SyncStatus.PAUSED: Interrupted mid-run (clean shutdown)
        """
        if self.is_complete:
            return SyncStatus.COMPLETE

        if self.current_batch:
            # Check if the process is still alive
            if self.is_stale():
                return SyncStatus.STALE
            return SyncStatus.IN_PROGRESS

        # Check if we have gaps due to failed batches
        if self.stats.batches_failed > 0 or len(self.failed_ranges) > 0:
            return SyncStatus.INCOMPLETE
        # If we have some progress but no current batch and no failures recorded,
        # could be paused or just starting
        return SyncStatus.PAUSED

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

    def start_run(self, run_id: str | None = None) -> str:
        """Start a new sync run and register the current process.

        Args:
            run_id: Optional run ID to use. If not provided, one will be generated.

        Returns:
            The run ID for this run.
        """
        if run_id is None:
            run_id = generate_run_id(self.source)

        self.current_run = SyncRun(
            run_id=run_id,
            pid=os.getpid(),
            started_at=datetime.now(),
        )
        self.updated_at = datetime.now()
        self.save()
        return run_id

    def end_run(self) -> None:
        """End the current sync run (clean shutdown)."""
        self.current_run = None
        self.current_batch = None
        self.updated_at = datetime.now()
        self.save()

    def mark_batch_started(self, start: datetime, end: datetime) -> None:
        """Mark a batch as started (in progress).

        If no run is active, automatically starts one.
        """
        # Auto-start a run if not already running
        if self.current_run is None:
            self.start_run()

        self.current_batch = DateRange(start=start, end=end)
        self.updated_at = datetime.now()
        self.save()

    def mark_batch_completed(self, start: datetime, end: datetime, spans: int) -> None:
        """Mark a batch as completed."""
        completed_range = DateRange(start=start, end=end, spans=spans)

        # Merge with existing ranges if they overlap
        self._merge_completed_range(completed_range)

        # Remove from failed_ranges if this was a retry
        self.failed_ranges = [
            r for r in self.failed_ranges
            if not (r.start == start and r.end == end)
        ]

        # Clear failure count since batch succeeded
        batch_key = start.strftime("%Y-%m-%d %H:%M:%S")
        if batch_key in self.batch_failures:
            del self.batch_failures[batch_key]

        self.current_batch = None
        self.stats.total_spans += spans
        self.stats.batches_completed += 1
        self.updated_at = datetime.now()
        self.save()

    def mark_batch_failed(self, start: datetime | None = None, end: datetime | None = None) -> None:
        """Mark the current batch as failed and track for retry.

        Args:
            start: Batch start time (uses current_batch if not provided)
            end: Batch end time (uses current_batch if not provided)
        """
        # Get batch bounds from current_batch if not provided
        if start is None or end is None:
            if self.current_batch:
                start = start or self.current_batch.start
                end = end or self.current_batch.end

        # Track failed range for potential retry (if we have bounds)
        if start is not None and end is not None:
            # Only add if not already tracked
            already_tracked = any(
                r.start == start and r.end == end
                for r in self.failed_ranges
            )
            if not already_tracked:
                self.failed_ranges.append(DateRange(start=start, end=end))

            # Increment failure count for this batch (for aggressive subdivision)
            batch_key = start.strftime("%Y-%m-%d %H:%M:%S")
            self.batch_failures[batch_key] = self.batch_failures.get(batch_key, 0) + 1
            logger.debug(f"Batch {batch_key}: failure count now {self.batch_failures[batch_key]}")

        self.current_batch = None
        self.stats.batches_failed += 1
        self.updated_at = datetime.now()
        self.save()

    def clear_failed_range(self, start: datetime, end: datetime) -> None:
        """Remove a range from failed_ranges (e.g., when retrying)."""
        self.failed_ranges = [
            r for r in self.failed_ranges
            if not (r.start == start and r.end == end)
        ]
        self.stats.retries_attempted += 1
        self.save()

    def add_subdivision(self) -> None:
        """Increment subdivision count."""
        self.stats.subdivisions += 1

    def get_failure_count(self, batch_key: str) -> int:
        """Get the number of consecutive failures for a batch.

        Args:
            batch_key: The batch identifier (batch_start timestamp string)

        Returns:
            Number of consecutive failures, 0 if never failed
        """
        return self.batch_failures.get(batch_key, 0)

    def clear_failure_count(self, batch_key: str) -> None:
        """Clear the failure count for a batch (e.g., when it succeeds).

        Args:
            batch_key: The batch identifier (batch_start timestamp string)
        """
        if batch_key in self.batch_failures:
            del self.batch_failures[batch_key]
            self.save()

    def get_recommended_initial_window(self, batch_key: str, batch_duration: timedelta) -> timedelta:
        """Get recommended initial time window based on failure history.

        For batches that have repeatedly failed, start with smaller time windows
        to help Phoenix handle high-volume days. The window shrinks exponentially
        with each failure.

        Formula: window = batch_duration / (2 ^ failure_count)
        - 0 failures: full batch (e.g., 24h)
        - 1 failure: half batch (e.g., 12h)
        - 2 failures: quarter batch (e.g., 6h)
        - 3 failures: eighth batch (e.g., 3h)
        - 4+ failures: sixteenth batch (e.g., 1.5h) - minimum

        Args:
            batch_key: The batch identifier (batch_start timestamp string)
            batch_duration: The full duration of the batch

        Returns:
            Recommended initial time window for the first query
        """
        failures = self.get_failure_count(batch_key)
        if failures == 0:
            return batch_duration

        # Cap at 4 failures worth of shrinking (1/16th of original)
        shrink_factor = 2 ** min(failures, 4)
        recommended = batch_duration / shrink_factor

        # Minimum window of 1 hour
        min_window = timedelta(hours=1)
        if recommended < min_window:
            return min_window

        logger.debug(
            f"Batch {batch_key}: {failures} failures, recommending "
            f"{recommended} window (vs {batch_duration} full)"
        )
        return recommended

    # --- Partial Range Tracking (v4) ---
    # These methods track successful sub-batches within a day that hasn't fully completed.
    # This allows resuming from where we left off when a later sub-batch fails.

    def add_partial_range(
        self,
        batch_key: str,
        start: datetime,
        end: datetime,
        spans: int,
    ) -> None:
        """Record a successful sub-batch within a day.

        Args:
            batch_key: The day identifier (batch_start timestamp string)
            start: Sub-batch start time
            end: Sub-batch end time
            spans: Number of spans fetched
        """
        if batch_key not in self.partial_ranges:
            self.partial_ranges[batch_key] = []

        self.partial_ranges[batch_key].append({
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "spans": spans,
        })

        logger.debug(
            f"Added partial range for {batch_key}: {start.strftime('%H:%M')}-{end.strftime('%H:%M')} ({spans:,} spans)"
        )
        self.updated_at = datetime.now()
        self.save()

    def get_partial_ranges(self, batch_key: str) -> list[dict[str, Any]]:
        """Get all partial ranges for a day.

        Args:
            batch_key: The day identifier (batch_start timestamp string)

        Returns:
            List of partial range dicts with start, end, spans keys
        """
        return self.partial_ranges.get(batch_key, [])

    def get_unfetched_ranges(
        self,
        batch_start: datetime,
        batch_end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """Get time ranges within a batch that still need fetching.

        Looks up partial_ranges for this batch and returns the gaps.

        Args:
            batch_start: Batch start time
            batch_end: Batch end time

        Returns:
            List of (start, end) tuples for unfetched time windows
        """
        batch_key = batch_start.strftime("%Y-%m-%d %H:%M:%S")
        partials = self.partial_ranges.get(batch_key, [])

        if not partials:
            return [(batch_start, batch_end)]

        # Sort existing ranges by start time
        sorted_partials = sorted(partials, key=lambda x: x["start"])

        # Find gaps between existing ranges
        gaps = []
        current = batch_start
        for r in sorted_partials:
            r_start = datetime.strptime(r["start"], "%Y-%m-%d %H:%M:%S")
            r_end = datetime.strptime(r["end"], "%Y-%m-%d %H:%M:%S")
            if r_start > current:
                gaps.append((current, r_start))
            current = max(current, r_end)

        if current < batch_end:
            gaps.append((current, batch_end))

        return gaps

    def get_partial_spans_count(self, batch_key: str) -> int:
        """Get total spans already fetched for partial ranges in a batch.

        Args:
            batch_key: The day identifier (batch_start timestamp string)

        Returns:
            Total span count from partial ranges
        """
        partials = self.partial_ranges.get(batch_key, [])
        return sum(p.get("spans", 0) for p in partials)

    def complete_partial_day(self, batch_key: str, batch_start: datetime, batch_end: datetime) -> int:
        """Finalize a day that was completed via partial ranges.

        Moves partial ranges to completed_ranges and clears them.

        Args:
            batch_key: The day identifier
            batch_start: Batch start time
            batch_end: Batch end time

        Returns:
            Total spans from the partial ranges
        """
        partials = self.partial_ranges.pop(batch_key, [])
        total_spans = sum(p.get("spans", 0) for p in partials)

        if total_spans > 0:
            # Add to completed ranges
            completed_range = DateRange(start=batch_start, end=batch_end, spans=total_spans)
            self._merge_completed_range(completed_range)

            # Remove from failed_ranges if present
            self.failed_ranges = [
                r for r in self.failed_ranges
                if not (r.start == batch_start and r.end == batch_end)
            ]

            logger.info(
                f"Completed partial day {batch_key}: {total_spans:,} spans from {len(partials)} sub-batches"
            )

        self.updated_at = datetime.now()
        self.save()
        return total_spans

    def clear_partial_ranges(self, batch_key: str) -> None:
        """Clear partial ranges for a batch (e.g., on reset or full refetch).

        Args:
            batch_key: The day identifier
        """
        if batch_key in self.partial_ranges:
            del self.partial_ranges[batch_key]
            self.save()

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
            "version": 4,  # v4: Added partial_ranges for incremental sub-batch persistence
            "source": self.source,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "target_range": {
                "start": self.target_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": self.target_end.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "completed_ranges": [r.to_dict() for r in self.completed_ranges],
            "failed_ranges": [r.to_dict() for r in self.failed_ranges],
            "partial_ranges": self.partial_ranges,  # Already dict format
            "batch_failures": self.batch_failures,  # Already dict format
            "current_batch": self.current_batch.to_dict() if self.current_batch else None,
            "current_run": self.current_run.to_dict() if self.current_run else None,
            "stats": self.stats.to_dict(),
            "config": self.config.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoricalSyncState":
        """Create from dictionary.

        Handles migration from v3 (no partial_ranges) to v4 automatically.
        """
        target_range = data.get("target_range", {})

        return cls(
            source=data["source"],
            target_start=datetime.strptime(target_range["start"], "%Y-%m-%d %H:%M:%S"),
            target_end=datetime.strptime(target_range["end"], "%Y-%m-%d %H:%M:%S"),
            completed_ranges=[
                DateRange.from_dict(r) for r in data.get("completed_ranges", [])
            ],
            failed_ranges=[
                DateRange.from_dict(r) for r in data.get("failed_ranges", [])
            ],
            partial_ranges=data.get("partial_ranges", {}),  # v4: defaults to empty for v3 migration
            batch_failures=data.get("batch_failures", {}),  # v4: defaults to empty for migration
            current_batch=(
                DateRange.from_dict(data["current_batch"])
                if data.get("current_batch")
                else None
            ),
            current_run=(
                SyncRun.from_dict(data["current_run"])
                if data.get("current_run")
                else None
            ),
            stats=SyncStats.from_dict(data.get("stats", {})),
            config=SyncConfig.from_dict(data.get("config", {})),
            started_at=datetime.fromisoformat(data.get("started_at", datetime.now().isoformat())),
            updated_at=datetime.fromisoformat(data.get("updated_at", datetime.now().isoformat())),
        )

    def save(self) -> None:
        """Save state to disk atomically.

        Uses write-to-temp-then-rename pattern to ensure state is never
        corrupted by crashes during write. Also calls fsync() to ensure
        data hits disk before rename.
        """
        state_dir = get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)

        # Write to temp file in same directory (ensures same filesystem for atomic rename)
        fd, tmp_path = tempfile.mkstemp(
            dir=state_dir,
            prefix=f".{self.source}-",
            suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())  # Force write to disk

            # Atomic rename (POSIX guarantees this is atomic on same filesystem)
            os.rename(tmp_path, self.state_file)
            logger.debug(f"State saved atomically to {self.state_file}")
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

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
        force_resume: bool = False,
    ) -> tuple["HistoricalSyncState", bool]:
        """
        Load existing state or create new one.

        Args:
            source: Source identifier
            target_start: Target range start
            target_end: Target range end
            config: Sync configuration
            force_resume: If True, always resume existing checkpoint regardless
                         of date range. The existing range is preserved.

        Returns:
            Tuple of (state, is_resuming) where is_resuming is True if
            we loaded existing state.
        """
        existing = cls.load(source)

        if existing is not None:
            # Force resume uses existing checkpoint as-is
            if force_resume:
                # Update config if provided
                if config:
                    existing.config = config
                    existing.save()
                return existing, True

            # Check if target range matches (using date-only comparison)
            existing_start_date = existing.target_start.date()
            existing_end_date = existing.target_end.date()
            new_start_date = target_start.date()
            new_end_date = target_end.date()

            if existing_start_date == new_start_date and existing_end_date == new_end_date:
                # Same date range - resume
                return existing, True

            # Check if new range extends existing range (smart merging)
            # Only extend if new range contains or overlaps existing
            if new_start_date <= existing_start_date and new_end_date >= existing_end_date:
                # New range contains existing - extend the range and keep progress
                existing.target_start = target_start
                existing.target_end = target_end
                if config:
                    existing.config = config
                existing.save()
                return existing, True

            if new_start_date <= existing_end_date and new_end_date >= existing_start_date:
                # Ranges overlap - merge them
                existing.target_start = min(existing.target_start, target_start)
                existing.target_end = max(existing.target_end, target_end)
                if config:
                    existing.config = config
                existing.save()
                return existing, True

            # Ranges don't overlap - need to reset
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
