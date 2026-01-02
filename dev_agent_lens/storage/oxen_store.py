"""
Oxen JSONL Store Module

Provides JSONL file storage for trace data with optional Oxen version control.
Works in local-only mode when Oxen is not configured.

Storage Structure (v2 - per-source):
    ~/.dal/data/
    ├── raw/
    │   ├── <source-name>/            # Per-source raw files
    │   │   └── sync_YYYYMMDD_HHMMSS.jsonl
    │   └── _legacy/                  # Legacy files (pre-source migration)
    ├── sessions/
    │   ├── <source-name>/            # Per-source session files
    │   │   ├── sessions_current.jsonl    # Symlink to latest
    │   │   └── sessions_YYYYMMDD.jsonl
    │   ├── _legacy/                  # Legacy files (pre-source migration)
    │   └── combined/                 # Optional combined view
    │       └── sessions_current.jsonl
    ├── unified/                      # Unified session exports (for Oxen)
    │   └── <source-name>_sessions.jsonl
    └── state/
        └── sync_state.json           # Tracks last sync per source

Legacy Structure (v1 - flat):
    ~/.dal/data/
    ├── raw/
    │   └── sync_YYYYMMDD_HHMMSS.jsonl
    ├── sessions/
    │   ├── sessions_current.jsonl
    │   └── sessions_YYYYMMDD.jsonl
    └── state/
        └── sync_state.json

Oxen Integration:
    Only the unified/ directory is committed to Oxen. Raw sync files are
    too large for version control and are kept local-only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


def get_default_data_path() -> Path:
    """Get the default data path for DAL storage."""
    env_path = os.getenv("DAL_DATA_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".dal" / "data"


class OxenStore:
    """
    JSONL file storage with optional Oxen version control integration.

    Manages trace data storage in JSONL format, creating timestamped files
    for each sync operation. Supports optional Oxen integration for version
    control and remote push when OXEN_REMOTE_URL is configured.

    Supports both legacy (flat) and v2 (per-source) storage structures.

    Args:
        data_path: Base path for DAL data files. Defaults to ~/.dal/data.
        source_name: Optional source name for per-source storage.
            If provided, data is stored in source-specific subdirectories.

    Example:
        >>> # Legacy mode (flat storage)
        >>> store = OxenStore()
        >>> store.append_spans(spans_df, backend="phoenix-local")
        >>>
        >>> # Per-source mode
        >>> store = OxenStore(source_name="phoenix-alex")
        >>> store.append_spans(spans_df, backend="phoenix-alex")
    """

    LEGACY_DIR = "_legacy"
    COMBINED_DIR = "combined"

    def __init__(
        self,
        data_path: Path | str | None = None,
        source_name: str | None = None,
    ) -> None:
        if data_path is None:
            self._data_path = get_default_data_path()
        else:
            self._data_path = Path(data_path).expanduser()

        self._source_name = source_name

        # Base directories
        self._raw_base = self._data_path / "raw"
        self._sessions_base = self._data_path / "sessions"
        self._unified_base = self._data_path / "unified"

        # Source-specific directories (or legacy root)
        if source_name:
            self._raw_dir = self._raw_base / source_name
            self._sessions_dir = self._sessions_base / source_name
        else:
            # Legacy mode - use base directories directly
            self._raw_dir = self._raw_base
            self._sessions_dir = self._sessions_base

        # Ensure directories exist
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        # Track last written file
        self._last_raw_file: Path | None = None

        # Oxen configuration - check config module first, then env var
        self._oxen_remote_url = self._get_oxen_remote()
        self._oxen_initialized = False

    def _get_oxen_remote(self) -> str | None:
        """Get Oxen remote URL from config or environment."""
        # Try config module first
        try:
            from dev_agent_lens.config import get_oxen_remote

            remote = get_oxen_remote()
            if remote:
                return remote
        except ImportError:
            pass

        # Fall back to environment variable
        return os.getenv("OXEN_REMOTE_URL")

    @property
    def data_path(self) -> Path:
        """Get the base data path."""
        return self._data_path

    @property
    def source_name(self) -> str | None:
        """Get the source name (None for legacy mode)."""
        return self._source_name

    @property
    def raw_dir(self) -> Path:
        """Get the raw files directory (source-specific if source_name set)."""
        return self._raw_dir

    @property
    def sessions_dir(self) -> Path:
        """Get the sessions directory (source-specific if source_name set)."""
        return self._sessions_dir

    @property
    def unified_dir(self) -> Path:
        """Get the unified exports directory."""
        return self._unified_base

    @property
    def last_raw_file(self) -> Path | None:
        """Get the path to the last raw file written."""
        return self._last_raw_file

    @property
    def oxen_enabled(self) -> bool:
        """Check if Oxen integration is enabled."""
        return self._oxen_remote_url is not None

    @property
    def current_sessions_path(self) -> Path:
        """Get the path to the current sessions symlink."""
        return self._sessions_dir / "sessions_current.jsonl"

    def get_source_raw_dir(self, source_name: str) -> Path:
        """Get the raw directory for a specific source."""
        return self._raw_base / source_name

    def get_source_sessions_dir(self, source_name: str) -> Path:
        """Get the sessions directory for a specific source."""
        return self._sessions_base / source_name

    def list_sources(self) -> list[str]:
        """List all sources with data.

        Returns:
            List of source names that have data directories.
        """
        sources = []

        # Check sessions directory for source subdirectories
        if self._sessions_base.exists():
            for path in self._sessions_base.iterdir():
                if path.is_dir() and not path.name.startswith("_"):
                    # Skip special directories
                    if path.name not in (self.LEGACY_DIR, self.COMBINED_DIR):
                        sources.append(path.name)

        return sorted(sources)

    def _generate_raw_filename(self) -> str:
        """Generate a timestamped filename for a raw sync file."""
        # Include microseconds for uniqueness in rapid succession calls
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"sync_{timestamp}.jsonl"

    def _generate_sessions_filename(self) -> str:
        """Generate a dated filename for a sessions file."""
        date = datetime.now().strftime("%Y%m%d")
        return f"sessions_{date}.jsonl"

    def append_spans(
        self,
        spans: pd.DataFrame | list[dict[str, Any]],
        backend: str,
    ) -> Path:
        """
        Append spans to a new timestamped raw file.

        Each sync operation creates a new file in the raw directory.
        Spans are written in JSONL format (one JSON object per line).

        Args:
            spans: DataFrame or list of span dictionaries to store.
            backend: The backend identifier for metadata.

        Returns:
            Path to the created raw file.
        """
        if isinstance(spans, pd.DataFrame):
            if spans.empty:
                # Create empty file for consistency
                raw_file = self._raw_dir / self._generate_raw_filename()
                raw_file.touch()
                self._last_raw_file = raw_file
                return raw_file

            # Convert DataFrame to list of dicts
            records = spans.to_dict(orient="records")
        else:
            records = spans

        raw_file = self._raw_dir / self._generate_raw_filename()

        # Write JSONL format
        with open(raw_file, "w") as f:
            for record in records:
                # Add metadata
                record["_backend"] = backend
                record["_sync_time"] = datetime.now().isoformat()
                json.dump(record, f, default=str)
                f.write("\n")

        self._last_raw_file = raw_file
        return raw_file

    def get_raw_files(self) -> list[Path]:
        """
        Get all raw sync files sorted by modification time.

        Returns:
            List of raw file paths, newest first.
        """
        files = list(self._raw_dir.glob("sync_*.jsonl"))
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def read_raw_file(self, file_path: Path) -> pd.DataFrame:
        """
        Read a raw JSONL file into a DataFrame.

        Args:
            file_path: Path to the JSONL file.

        Returns:
            DataFrame containing the spans.
        """
        if not file_path.exists():
            return pd.DataFrame()

        if file_path.stat().st_size == 0:
            return pd.DataFrame()

        records = []
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))

        if not records:
            return pd.DataFrame()

        return pd.DataFrame(records)

    def merge_sessions(self, output_file: Path | None = None) -> Path:
        """
        Merge all raw files into a single sessions file.

        Creates a new sessions file and updates the sessions_current symlink.

        Args:
            output_file: Optional output path. Defaults to dated sessions file.

        Returns:
            Path to the created sessions file.
        """
        if output_file is None:
            output_file = self._sessions_dir / self._generate_sessions_filename()

        # Collect all spans from raw files
        all_spans = []
        for raw_file in self.get_raw_files():
            df = self.read_raw_file(raw_file)
            if not df.empty:
                all_spans.append(df)

        if all_spans:
            merged = pd.concat(all_spans, ignore_index=True)
            # Deduplicate by span_id if present
            if "span_id" in merged.columns:
                merged = merged.drop_duplicates(subset=["span_id"], keep="last")
        else:
            merged = pd.DataFrame()

        # Write merged file
        with open(output_file, "w") as f:
            if not merged.empty:
                for record in merged.to_dict(orient="records"):
                    json.dump(record, f, default=str)
                    f.write("\n")

        # Update symlink
        self._update_current_symlink(output_file)

        return output_file

    def _update_current_symlink(self, target: Path) -> None:
        """Update the sessions_current.jsonl symlink."""
        symlink = self._sessions_dir / "sessions_current.jsonl"

        # Remove existing symlink if present
        if symlink.exists() or symlink.is_symlink():
            symlink.unlink()

        # Create relative symlink
        symlink.symlink_to(target.name)

    def get_current_sessions(self) -> pd.DataFrame:
        """
        Read the current sessions file.

        Returns:
            DataFrame containing all current sessions.
        """
        symlink = self._sessions_dir / "sessions_current.jsonl"

        if not symlink.exists():
            return pd.DataFrame()

        return self.read_raw_file(symlink)

    def init_oxen(self) -> bool:
        """
        Initialize Oxen repository if configured.

        Returns:
            True if Oxen was initialized, False if not configured or failed.
        """
        if not self.oxen_enabled:
            return False

        if self._oxen_initialized:
            return True

        try:
            # Import oxen only when needed
            import oxen

            repo = oxen.Repo(str(self._data_path))
            if not (self._data_path / ".oxen").exists():
                repo.init()

            self._oxen_initialized = True
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def commit(self, message: str) -> bool:
        """
        Commit unified session files to Oxen.

        Only commits files from the unified/ directory. Raw sync files
        are too large for version control and are kept local-only.

        Args:
            message: Commit message.

        Returns:
            True if commit succeeded, False otherwise.
        """
        if not self.oxen_enabled:
            return False

        # Ensure unified directory exists
        if not self._unified_base.exists():
            self._unified_base.mkdir(parents=True, exist_ok=True)

        try:
            import oxen

            repo = oxen.Repo(str(self._data_path))
            # Only add the unified directory, not raw files
            repo.add(str(self._unified_base.relative_to(self._data_path)))
            repo.commit(message)
            return True
        except (ImportError, Exception):
            return False

    def push(self) -> bool:
        """
        Push to Oxen remote.

        Returns:
            True if push succeeded, False otherwise.
        """
        if not self.oxen_enabled or not self._oxen_remote_url:
            return False

        try:
            import oxen

            repo = oxen.Repo(str(self._data_path))
            repo.push()
            return True
        except (ImportError, Exception):
            return False

    def pull(self) -> bool:
        """
        Pull latest from Oxen remote.

        Fetches the latest unified session files from the remote repository.

        Returns:
            True if pull succeeded, False otherwise.
        """
        if not self.oxen_enabled or not self._oxen_remote_url:
            return False

        try:
            import oxen

            repo = oxen.Repo(str(self._data_path))
            repo.pull()
            return True
        except (ImportError, Exception):
            return False

    def set_remote(self, remote_url: str) -> bool:
        """
        Set the Oxen remote URL for the repository.

        Args:
            remote_url: The Oxen remote URL (e.g., hub.oxen.ai/team/repo)

        Returns:
            True if remote was set successfully, False otherwise.
        """
        try:
            import oxen

            repo = oxen.Repo(str(self._data_path))
            repo.set_remote("origin", remote_url)
            return True
        except (ImportError, Exception):
            return False

    def __repr__(self) -> str:
        oxen_status = "enabled" if self.oxen_enabled else "disabled"
        source_info = f", source='{self._source_name}'" if self._source_name else ""
        return f"OxenStore(path='{self._data_path}'{source_info}, oxen={oxen_status})"

    def has_legacy_data(self) -> bool:
        """Check if legacy (flat structure) data exists.

        Returns:
            True if there are session files directly in the sessions directory
            (not in source subdirectories).
        """
        if not self._sessions_base.exists():
            return False

        # Check for session files directly in sessions/ (not in subdirectories)
        for path in self._sessions_base.iterdir():
            if path.is_file() and path.suffix == ".jsonl":
                return True
            if path.is_symlink() and path.name == "sessions_current.jsonl":
                return True

        return False

    def migrate_legacy_to_source(self, source_name: str) -> dict[str, Any]:
        """Migrate legacy data to a named source.

        Moves flat session and raw files into source-specific directories.

        Args:
            source_name: The source name to migrate to.

        Returns:
            Dictionary with migration statistics.
        """
        import shutil

        stats = {
            "sessions_migrated": 0,
            "raw_migrated": 0,
            "errors": [],
        }

        # Create target directories
        target_sessions = self._sessions_base / source_name
        target_raw = self._raw_base / source_name
        target_sessions.mkdir(parents=True, exist_ok=True)
        target_raw.mkdir(parents=True, exist_ok=True)

        # Migrate session files
        if self._sessions_base.exists():
            for path in self._sessions_base.iterdir():
                # Skip directories and special files
                if path.is_dir():
                    continue
                if path.name == "sessions_current.jsonl" and path.is_symlink():
                    # Handle symlink - recreate in target
                    target = path.resolve().name
                    new_symlink = target_sessions / "sessions_current.jsonl"
                    if not new_symlink.exists():
                        new_symlink.symlink_to(target)
                    path.unlink()
                    stats["sessions_migrated"] += 1
                elif path.suffix == ".jsonl":
                    # Move regular session files
                    try:
                        shutil.move(str(path), str(target_sessions / path.name))
                        stats["sessions_migrated"] += 1
                    except Exception as e:
                        stats["errors"].append(f"Failed to migrate {path.name}: {e}")

        # Migrate raw files
        if self._raw_base.exists():
            for path in self._raw_base.iterdir():
                if path.is_dir():
                    continue
                if path.suffix == ".jsonl":
                    try:
                        shutil.move(str(path), str(target_raw / path.name))
                        stats["raw_migrated"] += 1
                    except Exception as e:
                        stats["errors"].append(f"Failed to migrate {path.name}: {e}")

        return stats

    @classmethod
    def for_source(cls, source_name: str, data_path: Path | str | None = None) -> "OxenStore":
        """Create an OxenStore for a specific source.

        Convenience factory method.

        Args:
            source_name: The source name.
            data_path: Optional custom data path.

        Returns:
            OxenStore configured for the specified source.
        """
        return cls(data_path=data_path, source_name=source_name)
