"""
Tests for DAL CLI.

These tests use Click's test runner to verify CLI functionality.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from click.testing import CliRunner

from dev_agent_lens.cli.main import (
    BACKENDS,
    get_configured_backends,
    get_default_backend,
    main,
    sync,
)


@pytest.fixture
def runner():
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_env_phoenix(monkeypatch):
    """Set up Phoenix environment."""
    monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")


@pytest.fixture
def mock_env_arize(monkeypatch):
    """Set up Arize environment."""
    monkeypatch.setenv("ARIZE_API_KEY", "test-key")
    monkeypatch.setenv("ARIZE_SPACE_KEY", "test-space")


class TestMainCommand:
    """Tests for main command group."""

    def test_main_help(self, runner):
        """Main command shows help."""
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "DAL - Dev Agent Lens CLI" in result.output

    def test_main_version(self, runner):
        """Main command shows version."""
        result = runner.invoke(main, ["--version"])

        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestConfiguredBackends:
    """Tests for backend configuration detection."""

    def test_no_backends_configured(self, monkeypatch):
        """Given no env vars, returns empty list."""
        monkeypatch.delenv("DAL_PHOENIX_URL", raising=False)
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = get_configured_backends()

        assert result == []

    def test_phoenix_configured(self, monkeypatch):
        """Given Phoenix env var, returns phoenix-local."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = get_configured_backends()

        assert "phoenix-local" in result

    def test_arize_configured(self, monkeypatch):
        """Given Arize env var, returns arize-cloud."""
        monkeypatch.delenv("DAL_PHOENIX_URL", raising=False)
        monkeypatch.setenv("ARIZE_API_KEY", "test-key")

        result = get_configured_backends()

        assert "arize-cloud" in result

    def test_both_configured(self, monkeypatch):
        """Given both env vars, returns both backends."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("ARIZE_API_KEY", "test-key")

        result = get_configured_backends()

        assert "phoenix-local" in result
        assert "arize-cloud" in result


class TestDefaultBackend:
    """Tests for default backend selection."""

    def test_no_default_no_configured(self, monkeypatch):
        """Given no config, returns None."""
        monkeypatch.delenv("DAL_DEFAULT_BACKEND", raising=False)
        monkeypatch.delenv("DAL_PHOENIX_URL", raising=False)
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = get_default_backend()

        assert result is None

    def test_explicit_default(self, monkeypatch):
        """Given DAL_DEFAULT_BACKEND, returns that."""
        monkeypatch.setenv("DAL_DEFAULT_BACKEND", "arize-cloud")
        monkeypatch.setenv("ARIZE_API_KEY", "test-key")

        result = get_default_backend()

        assert result == "arize-cloud"

    def test_first_configured_as_default(self, monkeypatch):
        """Given configured backends, first one is default."""
        monkeypatch.delenv("DAL_DEFAULT_BACKEND", raising=False)
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")

        result = get_default_backend()

        assert result == "phoenix-local"


class TestSyncCommand:
    """Tests for sync command."""

    def test_sync_help(self, runner):
        """Sync command shows help."""
        result = runner.invoke(main, ["sync", "--help"])

        assert result.exit_code == 0
        assert "--full" in result.output
        assert "--backend" in result.output
        assert "--push" in result.output

    def test_sync_no_backends_configured(self, runner, monkeypatch):
        """Given no backends, sync fails with error."""
        monkeypatch.delenv("DAL_PHOENIX_URL", raising=False)
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = runner.invoke(main, ["sync"])

        assert result.exit_code == 1
        assert "No backends configured" in result.output

    def test_sync_backend_not_configured(self, runner, monkeypatch):
        """Given unconfigured backend, sync fails."""
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = runner.invoke(main, ["sync", "--backend", "arize-cloud"])

        assert result.exit_code == 1
        assert "not configured" in result.output

    def test_sync_incremental_mode_shown(self, runner, monkeypatch, tmp_path):
        """Sync shows incremental mode by default."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        # Mock the client to return empty DataFrame
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.get_spans_dataframe.return_value = pd.DataFrame()
            mock_client.return_value = mock_instance

            result = runner.invoke(main, ["sync"])

        assert "Mode: incremental" in result.output

    def test_sync_full_mode_shown(self, runner, monkeypatch, tmp_path):
        """Sync --full shows full mode."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.get_spans_dataframe.return_value = pd.DataFrame()
            mock_client.return_value = mock_instance

            result = runner.invoke(main, ["sync", "--full"])

        assert "Mode: full" in result.output

    def test_sync_fetches_spans(self, runner, monkeypatch, tmp_path):
        """Sync fetches and stores spans."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        mock_spans = pd.DataFrame({
            "span_id": ["span1", "span2"],
            "name": ["test1", "test2"],
            "context.span_id": ["span1", "span2"],
            "context.trace_id": ["trace1", "trace1"],
            "start_time": ["2025-01-01T12:00:00", "2025-01-01T12:01:00"],
            "metadata": [{"user_id": "session_abc"}, {"user_id": "session_abc"}],
        })

        # Mock the internal Phoenix client to bypass import check
        with patch("dev_agent_lens.clients.phoenix._PhoenixClient", MagicMock()):
            with patch.object(
                __import__("dev_agent_lens.clients.phoenix", fromlist=["PhoenixClient"]).PhoenixClient,
                "get_spans_dataframe",
                return_value=mock_spans,
            ):
                result = runner.invoke(main, ["sync"])

        assert "Fetched 2 spans" in result.output
        assert "Sync complete!" in result.output

    def test_sync_updates_state(self, runner, monkeypatch, tmp_path):
        """After successful sync, state is updated."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        mock_spans = pd.DataFrame({
            "span_id": ["span1"],
            "name": ["test"],
            "context.span_id": ["span1"],
            "context.trace_id": ["trace1"],
        })

        # Mock the internal Phoenix client to bypass import check
        with patch("dev_agent_lens.clients.phoenix._PhoenixClient", MagicMock()):
            with patch.object(
                __import__("dev_agent_lens.clients.phoenix", fromlist=["PhoenixClient"]).PhoenixClient,
                "get_spans_dataframe",
                return_value=mock_spans,
            ):
                runner.invoke(main, ["sync"])

        # Check state file was created
        state_file = tmp_path / "state" / "sync_state.json"
        assert state_file.exists()

        with open(state_file) as f:
            state_data = json.load(f)

        assert "phoenix-local" in state_data.get("backends", {})

    def test_sync_no_spans_found(self, runner, monkeypatch, tmp_path):
        """When no spans, shows warning."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        # Mock the internal Phoenix client to bypass import check
        with patch("dev_agent_lens.clients.phoenix._PhoenixClient", MagicMock()):
            with patch.object(
                __import__("dev_agent_lens.clients.phoenix", fromlist=["PhoenixClient"]).PhoenixClient,
                "get_spans_dataframe",
                return_value=pd.DataFrame(),
            ):
                result = runner.invoke(main, ["sync"])

        assert "No new spans found" in result.output

    def test_sync_push_no_oxen(self, runner, monkeypatch, tmp_path):
        """Sync --push without Oxen shows warning."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)

        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.get_spans_dataframe.return_value = pd.DataFrame()
            mock_client.return_value = mock_instance

            result = runner.invoke(main, ["sync", "--push"])

        assert "OXEN_REMOTE_URL not set" in result.output

    def test_sync_error_no_state_update(self, runner, monkeypatch, tmp_path):
        """If sync fails, state is not updated."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.get_spans_dataframe.side_effect = Exception("Connection failed")
            mock_client.return_value = mock_instance

            result = runner.invoke(main, ["sync"])

        # State file should not have phoenix-local entry (or file shouldn't exist)
        state_file = tmp_path / "state" / "sync_state.json"
        if state_file.exists():
            with open(state_file) as f:
                state_data = json.load(f)
            assert "phoenix-local" not in state_data.get("backends", {})


class TestConfigCommand:
    """Tests for config command."""

    def test_config_shows_backends(self, runner, monkeypatch):
        """Config command shows backend status."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = runner.invoke(main, ["config"])

        assert result.exit_code == 0
        assert "phoenix-local" in result.output
        assert "arize-cloud" in result.output

    def test_config_shows_oxen_status(self, runner, monkeypatch):
        """Config command shows Oxen status."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)

        result = runner.invoke(main, ["config"])

        assert "Oxen remote" in result.output


class TestStatusCommand:
    """Tests for status command."""

    def test_status_no_history(self, runner, monkeypatch, tmp_path):
        """Status shows message when no sync history."""
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "No sync history" in result.output

    def test_status_shows_last_sync(self, runner, monkeypatch, tmp_path):
        """Status shows last sync time."""
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))

        # Create state file with sync history
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "sync_state.json"
        state_file.write_text(json.dumps({
            "backends": {
                "phoenix-local": {"last_sync": "2025-01-01T12:00:00"}
            }
        }))

        result = runner.invoke(main, ["status"])

        assert "phoenix-local" in result.output
        assert "2025-01-01" in result.output
