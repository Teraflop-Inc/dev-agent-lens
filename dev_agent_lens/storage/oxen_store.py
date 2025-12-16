"""
Oxen JSONL Store Module

Provides JSONL file storage for trace data with optional Oxen version control.
Works in local-only mode when Oxen is not configured.

Storage Structure:
    ~/.dal/data/
    ├── raw/                          # Raw sync files (append-only archive)
    │   └── sync_YYYYMMDD_HHMMSS.jsonl
    ├── sessions/                     # Merged session files
    │   ├── sessions_current.jsonl    # Symlink to latest
    │   └── sessions_YYYYMMDD.jsonl
    └── state/
        └── sync_state.json           # Tracks last sync per backend
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

    Args:
        data_path: Base path for DAL data files. Defaults to ~/.dal/data.

    Example:
        >>> store = OxenStore()
        >>> store.append_spans(spans_df, backend="phoenix-local")
        >>> print(f"Stored in {store.last_raw_file}")
    """

    def __init__(self, data_path: Path | str | None = None) -> None:
        if data_path is None:
            self._data_path = get_default_data_path()
        else:
            self._data_path = Path(data_path).expanduser()

        self._raw_dir = self._data_path / "raw"
        self._sessions_dir = self._data_path / "sessions"

        # Ensure directories exist
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        # Track last written file
        self._last_raw_file: Path | None = None

        # Oxen configuration
        self._oxen_remote_url = os.getenv("OXEN_REMOTE_URL")
        self._oxen_initialized = False

    @property
    def data_path(self) -> Path:
        """Get the base data path."""
        return self._data_path

    @property
    def raw_dir(self) -> Path:
        """Get the raw files directory."""
        return self._raw_dir

    @property
    def sessions_dir(self) -> Path:
        """Get the sessions directory."""
        return self._sessions_dir

    @property
    def last_raw_file(self) -> Path | None:
        """Get the path to the last raw file written."""
        return self._last_raw_file

    @property
    def oxen_enabled(self) -> bool:
        """Check if Oxen integration is enabled."""
        return self._oxen_remote_url is not None

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
        Commit current changes to Oxen.

        Args:
            message: Commit message.

        Returns:
            True if commit succeeded, False otherwise.
        """
        if not self.oxen_enabled:
            return False

        try:
            import oxen

            repo = oxen.Repo(str(self._data_path))
            repo.add(".")
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

    def __repr__(self) -> str:
        oxen_status = "enabled" if self.oxen_enabled else "disabled"
        return f"OxenStore(path='{self._data_path}', oxen={oxen_status})"
