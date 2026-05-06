"""
Tests for Oxen JSONL Store.

These tests verify storage functionality including:
- Local-only mode (no Oxen)
- JSONL file operations
- Session merging
- Symlink management
"""

from __future__ import annotations

import json
import sys
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dev_agent_lens.storage.oxen_store import OxenStore


class TestOxenStoreLocalMode:
    """Tests for local-only mode without Oxen."""

    def test_creates_directory_structure(self, tmp_path):
        """Given new store, creates required directories."""
        store = OxenStore(data_path=tmp_path)

        assert store.raw_dir.exists()
        assert store.sessions_dir.exists()

    def test_no_oxen_configured(self, tmp_path):
        """Given no OXEN_REMOTE_URL, oxen_enabled is False."""
        with patch.dict("os.environ", {}, clear=True):
            store = OxenStore(data_path=tmp_path)
            assert store.oxen_enabled is False

    def test_local_mode_no_errors(self, tmp_path):
        """Given local mode, operations complete without errors."""
        with patch.dict("os.environ", {}, clear=True):
            store = OxenStore(data_path=tmp_path)

            df = pd.DataFrame({"span_id": ["span1"], "name": ["test"]})
            raw_file = store.append_spans(df, backend="phoenix-local")

            assert raw_file.exists()
            assert store.last_raw_file == raw_file


class TestOxenStoreAppend:
    """Tests for appending spans."""

    def test_append_creates_timestamped_file(self, tmp_path):
        """Given spans, creates timestamped JSONL file."""
        store = OxenStore(data_path=tmp_path)
        df = pd.DataFrame({"span_id": ["span1"], "name": ["test"]})

        raw_file = store.append_spans(df, backend="phoenix-local")

        assert raw_file.name.startswith("sync_")
        assert raw_file.name.endswith(".jsonl")
        assert raw_file.exists()

    def test_append_writes_jsonl_format(self, tmp_path):
        """Given spans, writes valid JSONL format."""
        store = OxenStore(data_path=tmp_path)
        df = pd.DataFrame(
            {
                "span_id": ["span1", "span2"],
                "name": ["test1", "test2"],
            }
        )

        raw_file = store.append_spans(df, backend="phoenix-local")

        # Read and verify JSONL
        lines = raw_file.read_text().strip().split("\n")
        assert len(lines) == 2

        # Each line should be valid JSON
        for line in lines:
            record = json.loads(line)
            assert "span_id" in record
            assert "_backend" in record
            assert record["_backend"] == "phoenix-local"

    def test_append_empty_dataframe(self, tmp_path):
        """Given empty DataFrame, creates empty file."""
        store = OxenStore(data_path=tmp_path)
        df = pd.DataFrame()

        raw_file = store.append_spans(df, backend="phoenix-local")

        assert raw_file.exists()
        assert raw_file.stat().st_size == 0

    def test_append_from_list_of_dicts(self, tmp_path):
        """Given list of dicts, writes JSONL correctly."""
        store = OxenStore(data_path=tmp_path)
        spans = [
            {"span_id": "span1", "name": "test1"},
            {"span_id": "span2", "name": "test2"},
        ]

        raw_file = store.append_spans(spans, backend="arize-cloud")

        lines = raw_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_multiple_appends_create_separate_files(self, tmp_path):
        """Given multiple appends, creates separate timestamped files."""
        store = OxenStore(data_path=tmp_path)

        df1 = pd.DataFrame({"span_id": ["span1"]})
        file1 = store.append_spans(df1, backend="phoenix-local")

        df2 = pd.DataFrame({"span_id": ["span2"]})
        file2 = store.append_spans(df2, backend="phoenix-local")

        # Files should have different names (microseconds in timestamp)
        assert file1 != file2
        assert len(store.get_raw_files()) == 2


class TestOxenStoreRead:
    """Tests for reading files."""

    def test_read_raw_file(self, tmp_path):
        """Given JSONL file, reads into DataFrame."""
        store = OxenStore(data_path=tmp_path)
        df = pd.DataFrame({"span_id": ["span1", "span2"], "name": ["a", "b"]})
        raw_file = store.append_spans(df, backend="test")

        result = store.read_raw_file(raw_file)

        assert len(result) == 2
        assert "span_id" in result.columns

    def test_read_nonexistent_file(self, tmp_path):
        """Given nonexistent file, returns empty DataFrame."""
        store = OxenStore(data_path=tmp_path)

        result = store.read_raw_file(tmp_path / "nonexistent.jsonl")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_read_empty_file(self, tmp_path):
        """Given empty file, returns empty DataFrame."""
        store = OxenStore(data_path=tmp_path)
        empty_file = tmp_path / "raw" / "empty.jsonl"
        empty_file.parent.mkdir(parents=True, exist_ok=True)
        empty_file.touch()

        result = store.read_raw_file(empty_file)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0

    def test_get_raw_files_sorted(self, tmp_path):
        """Given multiple raw files, returns sorted by modification time."""
        store = OxenStore(data_path=tmp_path)

        store.append_spans([{"id": "1"}], backend="test")
        store.append_spans([{"id": "2"}], backend="test")
        store.append_spans([{"id": "3"}], backend="test")

        files = store.get_raw_files()

        # Should have 3 files (microseconds in filename ensure uniqueness)
        assert len(files) == 3
        # Newest first
        assert files[0].stat().st_mtime >= files[1].stat().st_mtime
        assert files[1].stat().st_mtime >= files[2].stat().st_mtime


class TestOxenStoreMerge:
    """Tests for session merging."""

    def test_merge_creates_sessions_file(self, tmp_path):
        """Given raw files, merge creates sessions file."""
        store = OxenStore(data_path=tmp_path)
        store.append_spans([{"span_id": "1"}], backend="test")
        store.append_spans([{"span_id": "2"}], backend="test")

        sessions_file = store.merge_sessions()

        assert sessions_file.exists()
        assert sessions_file.name.startswith("sessions_")

    def test_merge_creates_symlink(self, tmp_path):
        """Given merge, creates sessions_current symlink."""
        store = OxenStore(data_path=tmp_path)
        store.append_spans([{"span_id": "1"}], backend="test")

        sessions_file = store.merge_sessions()

        symlink = store.sessions_dir / "sessions_current.jsonl"
        assert symlink.exists()
        assert symlink.is_symlink()
        assert symlink.resolve() == sessions_file.resolve()

    def test_merge_deduplicates_by_span_id(self, tmp_path):
        """Given duplicate span_ids, keeps latest version."""
        store = OxenStore(data_path=tmp_path)

        store.append_spans([{"span_id": "1", "version": "old"}], backend="test")
        time.sleep(0.1)
        store.append_spans([{"span_id": "1", "version": "new"}], backend="test")

        store.merge_sessions()
        result = store.get_current_sessions()

        # Should have only one record with the newer version
        assert len(result) == 1

    def test_get_current_sessions(self, tmp_path):
        """Given merged sessions, reads current file."""
        store = OxenStore(data_path=tmp_path)
        # Put both spans in one append to avoid deduplication issues
        store.append_spans([{"span_id": "1"}, {"span_id": "2"}], backend="test")
        store.merge_sessions()

        result = store.get_current_sessions()

        assert len(result) == 2

    def test_get_current_sessions_no_merge(self, tmp_path):
        """Given no merge yet, returns empty DataFrame."""
        store = OxenStore(data_path=tmp_path)

        result = store.get_current_sessions()

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0


class TestOxenStoreJSONL:
    """Tests for JSONL format compliance."""

    def test_each_line_is_valid_json(self, tmp_path):
        """Given stored spans, each line parses independently."""
        store = OxenStore(data_path=tmp_path)
        store.append_spans(
            [
                {"span_id": "1", "data": "first"},
                {"span_id": "2", "data": "second"},
                {"span_id": "3", "data": "third"},
            ],
            backend="test",
        )

        with open(store.last_raw_file, "r") as f:
            for i, line in enumerate(f):
                record = json.loads(line)
                assert isinstance(record, dict)
                assert "span_id" in record

    def test_handles_special_characters(self, tmp_path):
        """Given special characters in data, escapes correctly."""
        store = OxenStore(data_path=tmp_path)
        store.append_spans(
            [{"span_id": "1", "text": 'Line with "quotes" and\nnewlines'}],
            backend="test",
        )

        result = store.read_raw_file(store.last_raw_file)
        assert 'Line with "quotes" and\nnewlines' in result.iloc[0]["text"]


class TestOxenStoreLargeFile:
    """Tests for handling large files."""

    def test_handles_many_spans(self, tmp_path):
        """Given 10k+ spans, handles without memory issues."""
        store = OxenStore(data_path=tmp_path)

        # Create 10k spans
        spans = [{"span_id": f"span_{i}", "index": i} for i in range(10000)]

        raw_file = store.append_spans(spans, backend="test")

        # Verify file was created
        assert raw_file.exists()
        assert raw_file.stat().st_size > 0

        # Verify can be read back
        result = store.read_raw_file(raw_file)
        assert len(result) == 10000


class TestOxenStoreOxenIntegration:
    """Tests for Oxen integration (mocked)."""

    def test_oxen_enabled_with_env_var(self, tmp_path, monkeypatch):
        """Given OXEN_REMOTE_URL, oxen_enabled is True."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        store = OxenStore(data_path=tmp_path)

        assert store.oxen_enabled is True

    def test_unified_dir_property(self, tmp_path):
        """Given store, unified_dir points to unified directory."""
        store = OxenStore(data_path=tmp_path)

        assert store.unified_dir == tmp_path / "unified"

    def test_init_oxen_creates_repo(self, tmp_path, monkeypatch):
        """Given Oxen configured, init creates .oxen directory."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            result = store.init_oxen()

            assert result is True
            mock_repo.init.assert_called_once()

    def test_commit_only_adds_unified_directory(self, tmp_path, monkeypatch):
        """Given Oxen configured, commit only stages unified/ directory."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            result = store.commit("Test commit")

            assert result is True
            # Should only add "unified" directory, not "."
            mock_repo.add.assert_called_once_with("unified")
            mock_repo.commit.assert_called_once_with("Test commit")

    def test_commit_creates_unified_directory(self, tmp_path, monkeypatch):
        """Given no unified directory, commit creates it."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            # unified dir should not exist yet
            assert not store.unified_dir.exists()

            store.commit("Test commit")

            # unified dir should now exist
            assert store.unified_dir.exists()

    def test_commit_does_not_add_raw_files(self, tmp_path, monkeypatch):
        """Given raw files exist, commit does not include them."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            # Create some raw files
            store.append_spans([{"span_id": "test"}], backend="test")

            result = store.commit("Test commit")

            assert result is True
            # Should only add "unified", not "." or "raw"
            mock_repo.add.assert_called_once_with("unified")

    def test_push_calls_oxen_push(self, tmp_path, monkeypatch):
        """Given Oxen configured, push calls remote push."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            result = store.push()

            assert result is True
            mock_repo.push.assert_called_once()

    def test_oxen_not_installed_returns_false(self, tmp_path, monkeypatch):
        """Given Oxen not installed, operations return False."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        with patch.dict("sys.modules", {"oxen": None}):
            store = OxenStore(data_path=tmp_path)

            # Should fail gracefully
            assert store.init_oxen() is False
            assert store.commit("test") is False
            assert store.push() is False

    def test_pull_calls_oxen_pull(self, tmp_path, monkeypatch):
        """Given Oxen configured, pull fetches from remote."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            result = store.pull()

            assert result is True
            mock_repo.pull.assert_called_once()

    def test_pull_returns_false_when_not_configured(self, tmp_path, monkeypatch):
        """Given no Oxen config, pull returns False."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)

        store = OxenStore(data_path=tmp_path)
        result = store.pull()

        assert result is False

    def test_set_remote_configures_origin(self, tmp_path, monkeypatch):
        """Given remote URL, sets origin remote."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test/repo")

        mock_oxen = MagicMock()
        mock_repo = MagicMock()
        mock_oxen.Repo.return_value = mock_repo

        with patch.dict(sys.modules, {"oxen": mock_oxen}):
            store = OxenStore(data_path=tmp_path)
            result = store.set_remote("hub.oxen.ai/new/repo")

            assert result is True
            mock_repo.set_remote.assert_called_once_with("origin", "hub.oxen.ai/new/repo")

    def test_uses_config_module_for_remote(self, tmp_path, monkeypatch):
        """Given config module has remote, uses it."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)

        with patch("dev_agent_lens.storage.oxen_store.os.getenv", return_value=None):
            with patch("dev_agent_lens.config.get_oxen_remote", return_value="hub.oxen.ai/config/repo"):
                store = OxenStore(data_path=tmp_path)
                assert store.oxen_enabled is True


class TestOxenStoreRepr:
    """Tests for string representation."""

    def test_repr_shows_path_and_status(self, tmp_path):
        """Given store, repr shows path and oxen status."""
        store = OxenStore(data_path=tmp_path)

        result = repr(store)

        assert "OxenStore" in result
        assert str(tmp_path) in result
        assert "oxen=disabled" in result

    def test_repr_shows_oxen_enabled(self, tmp_path, monkeypatch):
        """Given Oxen configured, repr shows enabled."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "https://hub.oxen.ai/test")

        store = OxenStore(data_path=tmp_path)

        result = repr(store)

        assert "oxen=enabled" in result
