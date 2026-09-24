"""
Tests for DAL CLI.

These tests use Click's test runner to verify CLI functionality.
"""

from __future__ import annotations

import json
import os
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
    """Tests for the sync command.

    Rewritten 2026-09-04. The originals predated the move to named sources: they set
    DAL_PHOENIX_URL and expected `--backend` and `--push`, none of which the command has
    any more, so 8 of 10 failed with "No sources configured" or "No such option". Every
    assertion below is against strings the command prints today, captured by running it.
    """

    @pytest.fixture
    def phoenix_source(self, runner, monkeypatch, tmp_path):
        """A configured Phoenix source in an isolated config + data dir."""
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        r = runner.invoke(
            main,
            ["config", "add-source", "test-phoenix", "--type", "phoenix", "--url", "localhost:6006"],
        )
        assert r.exit_code == 0, r.output
        return "test-phoenix"

    @staticmethod
    def _spans(n: int = 2) -> pd.DataFrame:
        return pd.DataFrame({
            "span_id": [f"span{i}" for i in range(n)],
            "name": [f"test{i}" for i in range(n)],
            "context.span_id": [f"span{i}" for i in range(n)],
            "context.trace_id": ["trace1"] * n,
            "start_time": [f"2025-01-01T12:0{i}:00" for i in range(n)],
        })

    @staticmethod
    def _state(tmp_path):
        f = tmp_path / "state" / "sync_state.json"
        return json.load(open(f)) if f.exists() else None

    def test_sync_help(self, runner):
        """Sync command shows its current options."""
        result = runner.invoke(main, ["sync", "--help"])

        assert result.exit_code == 0
        for opt in ("--source", "--all-sources", "--full", "--days"):
            assert opt in result.output, opt
        # removed when sources became named; a regression would resurrect them
        assert "--backend" not in result.output
        assert "--push" not in result.output

    def test_sync_no_sources_configured(self, runner, monkeypatch, tmp_path):
        """Given no sources, sync fails with an actionable error."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(main, ["sync"])

        assert result.exit_code == 1
        assert "No sources configured" in result.output
        assert "dal config add-source" in result.output

    def test_sync_unknown_source(self, runner, monkeypatch, tmp_path):
        """Given a source name that does not exist, sync fails and names it."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(main, ["sync", "--source", "nope"])

        assert result.exit_code == 1
        assert "Source 'nope' not found" in result.output

    def test_sync_one_day_is_lightweight(self, runner, phoenix_source):
        """A one-day sync runs in lightweight mode. (The earlier version asserted
        'robust OR lightweight', which the command always prints: a tautology.)"""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])

        assert result.exit_code == 0, result.output
        assert "Mode: lightweight" in result.output
        assert "Mode: robust" not in result.output

    def test_sync_multi_day_is_robust_and_batches(self, runner, phoenix_source, tmp_path):
        """A multi-day window takes the checkpointing path: robust mode, several batches,
        state advanced, checkpoint file cleaned up. --delay 0 keeps it fast."""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "7",
                                          "--delay", "0", "--batch-days", "7"])
        assert result.exit_code == 0, result.output
        assert "Mode: robust" in result.output
        assert phoenix_source in self._state(tmp_path)["backends"]

        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--full",
                                          "--days", "1", "--batch-hours", "12", "--delay", "0"])
        assert result.exit_code == 0, result.output
        assert "Processing 2 batches" in result.output
        assert mock_client.return_value.get_spans_dataframe.call_count == 2

    def test_sync_full_ignores_last_sync(self, runner, phoenix_source):
        """--full re-syncs the default window instead of resuming from last_sync.

        (The earlier version passed --full together with --days 1, and --days takes
        priority, so --full was never exercised.)
        """
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])   # seeds last_sync
            incremental = runner.invoke(main, ["sync", "--source", phoenix_source,
                                               "--delay", "0", "--batch-days", "30"])
            full = runner.invoke(main, ["sync", "--source", phoenix_source, "--full",
                                        "--delay", "0", "--batch-days", "30"])
        assert "incremental from last_sync" in incremental.output, incremental.output
        assert "default 30 days" in full.output, full.output

    def test_sync_fetches_spans(self, runner, phoenix_source):
        """Sync fetches spans from the source and completes."""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = self._spans(2)
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])

        assert result.exit_code == 0, result.output
        assert "2 spans" in result.output
        assert "Sync complete" in result.output

    def test_sync_updates_state(self, runner, phoenix_source, tmp_path):
        """After a successful sync, state records the source by NAME."""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = self._spans(1)
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])

        assert result.exit_code == 0, result.output
        state = self._state(tmp_path)
        assert state is not None
        assert phoenix_source in state["backends"]
        assert state["backends"][phoenix_source]["last_sync"] is not None

    def test_sync_no_spans_still_updates_state(self, runner, phoenix_source, tmp_path):
        """An empty but successful sync is still a successful sync: state advances."""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])

        assert result.exit_code == 0, result.output
        assert "Total: 0 spans" in result.output
        assert f"Updated sync state for '{phoenix_source}'" in result.output
        assert phoenix_source in self._state(tmp_path)["backends"]

    def test_sync_does_not_mutate_process_environment(self, runner, phoenix_source):
        """sync must not write the source's url/project into os.environ.

        It used to, so that a bare PhoenixClient() downstream would pick them up. The cost
        was that source B silently inherited source A's URL under --all-sources whenever
        B had none, and one test's source leaked into the next test's client default.
        """
        assert "DAL_PHOENIX_URL" not in os.environ          # conftest guarantees this
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])
            assert result.exit_code == 0, result.output
            # and the client received the source explicitly rather than via the environment
            _, kwargs = mock_client.call_args
            assert kwargs.get("base_url") == "localhost:6006"

        assert "DAL_PHOENIX_URL" not in os.environ
        assert "DAL_PHOENIX_PROJECT" not in os.environ

    def test_sync_lands_batches_in_the_span_store_when_one_is_chosen(self, runner, phoenix_source, tmp_path):
        """With `dal store use` done, every synced batch is appended to spans_raw."""
        r = runner.invoke(main, ["store", "use", f"file://{tmp_path}/store"])
        assert r.exit_code == 0, r.output
        frame = pd.DataFrame({
            "context.span_id": ["s1", "s2"], "context.trace_id": ["t1", "t1"],
            "parent_id": [None, "s1"], "name": ["litellm_request", "Bash"], "span_kind": ["LLM", "TOOL"],
            "start_time": pd.to_datetime(["2025-01-01T12:00:00Z", "2025-01-01T12:00:01Z"]),
            "end_time": pd.to_datetime(["2025-01-01T12:00:01Z", "2025-01-01T12:00:02Z"]),
            "status_code": ["OK", "OK"], "status_message": ["", ""],
            "attributes": ['{"llm":{"model_name":"m"}}', '{}'], "events": ["[]", "[]"],
            "cumulative_error_count": [0, 0], "cumulative_llm_token_count_prompt": [1, 0],
            "cumulative_llm_token_count_completion": [1, 0],
            "llm_token_count_prompt": [1, 0], "llm_token_count_completion": [1, 0],
        })
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = frame
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])
        assert result.exit_code == 0, result.output
        assert "-> span store: +2 rows" in result.output
        assert "Span store: +2 rows, 0 failed" in result.output
        files = list((tmp_path / "store" / "spans_raw").rglob("*.parquet"))
        assert len(files) == 1 and "day=2025-01-01" in str(files[0])
        import pyarrow.parquet as pq
        landed = pq.read_table(files[0]).to_pylist()
        assert {r["source"] for r in landed} == {phoenix_source}   # stamped per batch

    def test_sync_leaves_the_store_alone_when_none_is_chosen(self, runner, phoenix_source, tmp_path):
        """No `dal store use`, no store writes: upgrading must not start a second copy."""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1"])
        assert result.exit_code == 0, result.output
        assert "span store" not in result.output.lower()
        assert not (tmp_path / "spans").exists()

    def test_sync_warns_when_the_project_does_not_exist(self, runner, monkeypatch, tmp_path):
        """A project name absent from the database filters every span out. That must not
        read as an ordinary empty sync: the first live run against Supabase landed nothing
        because the client's default project does not exist there."""
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        r = runner.invoke(main, ["config", "add-source", "pg", "--type", "phoenix-postgres",
                                 "--connection-url", "postgresql://u:p@db.example/phoenix",
                                 "--project", "dev-agent-lens"])
        assert r.exit_code == 0, r.output
        with patch("dev_agent_lens.cli.main.PhoenixPostgresClient") as mock_cls:
            inst = mock_cls.return_value
            inst.project = "dev-agent-lens"
            inst.list_projects.return_value = ["claude-code-surfaces", "sf-workspaces"]
            inst.test_connection.return_value = True
            inst.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", "pg", "--days", "1"])
        assert "project 'dev-agent-lens' does not exist" in result.output, result.output
        assert "sf-workspaces" in result.output

    def test_sync_arize_does_not_mutate_process_environment(self, runner, monkeypatch, tmp_path):
        """The Arize branch had the same leak and was missed by the first fix."""
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        monkeypatch.setenv("ARIZE_API_KEY", "test-key")
        r = runner.invoke(main, ["config", "add-source", "test-arize", "--type", "arize",
                                 "--space-key", "sk-1", "--model-id", "m-1"])
        assert r.exit_code == 0, r.output
        with patch("dev_agent_lens.cli.main.ArizeClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.return_value = pd.DataFrame()
            result = runner.invoke(main, ["sync", "--source", "test-arize", "--days", "1"])
            assert result.exit_code == 0, result.output
            _, kwargs = mock_client.call_args
            assert kwargs.get("space_key") == "sk-1" and kwargs.get("model_id") == "m-1"
        assert "ARIZE_SPACE_KEY" not in os.environ
        assert "ARIZE_MODEL_ID" not in os.environ

    def test_sync_error_no_state_update(self, runner, phoenix_source, tmp_path):
        """If the source fails: the batch is reported failed, state is NOT advanced, and
        the exit code is non-zero. (It used to exit 0 with 0 of 1 batches completed.)
        --retries 1 avoids 6 s of real backoff sleep."""
        with patch("dev_agent_lens.cli.main.PhoenixClient") as mock_client:
            mock_client.return_value.get_spans_dataframe.side_effect = Exception("Connection failed")
            result = runner.invoke(main, ["sync", "--source", phoenix_source, "--days", "1",
                                          "--retries", "1"])

        assert "FAILED: Connection failed" in result.output
        assert "Batches failed: 1" in result.output
        assert result.exit_code != 0
        state = self._state(tmp_path)
        assert state is None or phoenix_source not in state.get("backends", {})


class TestConfigCommand:
    """Tests for config command."""

    def test_config_shows_backends(self, runner, monkeypatch, tmp_path):
        """Config show command shows backend status."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "http://localhost:6006")
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(main, ["config", "show"])

        assert result.exit_code == 0
        assert "phoenix-local" in result.output
        assert "arize-cloud" in result.output

    def test_config_shows_oxen_status(self, runner, monkeypatch, tmp_path):
        """Config show command shows Oxen status."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(main, ["config", "show"])

        assert "Oxen remote" in result.output

    def test_config_add_source_phoenix(self, runner, monkeypatch, tmp_path):
        """Config add-source creates a Phoenix source."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(
            main,
            ["config", "add-source", "my-phoenix", "--type", "phoenix", "--url", "localhost:6006"],
        )

        assert result.exit_code == 0
        assert "Added source: my-phoenix" in result.output

    def test_config_add_source_arize(self, runner, monkeypatch, tmp_path):
        """Config add-source creates an Arize source."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(
            main,
            [
                "config", "add-source", "my-arize",
                "--type", "arize",
                "--space-key", "ABC123",
                "--model-id", "test-model",
                "--shared",
            ],
        )

        assert result.exit_code == 0
        assert "Added source: my-arize" in result.output

    def test_config_add_source_validates(self, runner, monkeypatch, tmp_path):
        """Config add-source validates required fields."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        # Arize without space-key
        result = runner.invoke(
            main,
            ["config", "add-source", "bad-arize", "--type", "arize"],
        )

        assert result.exit_code == 1
        assert "Error" in result.output

    def test_config_add_source_phoenix_postgres_explicit(
        self, runner, monkeypatch, tmp_path
    ):
        """add-source --type phoenix-postgres saves connection_url + schema."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        # Make sure env-var fallback isn't masking missing-flag bugs
        monkeypatch.delenv("PHOENIX_SQL_DATABASE_URL", raising=False)
        monkeypatch.delenv("PHOENIX_SQL_DATABASE_SCHEMA", raising=False)

        result = runner.invoke(
            main,
            [
                "config", "add-source", "phoenix-pg",
                "--type", "phoenix-postgres",
                "--connection-url", "postgresql://u:p@h:5432/d",
                "--schema", "phoenix",
                "--project", "dev-agent-lens",
                "--shared",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Added source: phoenix-pg" in result.output
        assert "phoenix-postgres" in result.output

        # Round-trip the saved file
        from dev_agent_lens.core.sources import SourceManager, SourceType

        saved = SourceManager().get_source("phoenix-pg")
        assert saved is not None
        assert saved.source_type == SourceType.PHOENIX_POSTGRES
        assert saved.connection_url == "postgresql://u:p@h:5432/d"
        assert saved.schema == "phoenix"
        assert saved.project == "dev-agent-lens"
        assert saved.local_only is False

    def test_config_add_source_phoenix_postgres_env_fallback(
        self, runner, monkeypatch, tmp_path
    ):
        """phoenix-postgres falls back to PHOENIX_SQL_DATABASE_URL env var."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        monkeypatch.setenv("PHOENIX_SQL_DATABASE_URL", "postgresql://u:p@env:5432/d")
        monkeypatch.setenv("PHOENIX_SQL_DATABASE_SCHEMA", "phoenix_custom")

        result = runner.invoke(
            main,
            [
                "config", "add-source", "phoenix-pg-env",
                "--type", "phoenix-postgres",
                "--project", "dev-agent-lens",
                "--shared",
            ],
        )

        assert result.exit_code == 0, result.output

        from dev_agent_lens.core.sources import SourceManager

        saved = SourceManager().get_source("phoenix-pg-env")
        assert saved is not None
        assert saved.connection_url == "postgresql://u:p@env:5432/d"
        assert saved.schema == "phoenix_custom"

    def test_config_add_source_phoenix_postgres_missing_url(
        self, runner, monkeypatch, tmp_path
    ):
        """phoenix-postgres without connection_url errors out."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        monkeypatch.delenv("PHOENIX_SQL_DATABASE_URL", raising=False)

        result = runner.invoke(
            main,
            [
                "config", "add-source", "bad-pg",
                "--type", "phoenix-postgres",
            ],
        )

        assert result.exit_code == 1
        assert "connection_url" in result.output

    def test_config_list_sources_empty(self, runner, monkeypatch, tmp_path):
        """Config list-sources shows message when no sources."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        result = runner.invoke(main, ["config", "list-sources"])

        assert result.exit_code == 0
        assert "No sources configured" in result.output

    def test_config_list_sources_shows_sources(self, runner, monkeypatch, tmp_path):
        """Config list-sources shows configured sources."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        # Add a source first
        runner.invoke(
            main,
            ["config", "add-source", "test-source", "--type", "phoenix", "--url", "localhost:6006"],
        )

        result = runner.invoke(main, ["config", "list-sources"])

        assert result.exit_code == 0
        assert "test-source" in result.output

    def test_config_remove_source(self, runner, monkeypatch, tmp_path):
        """Config remove-source removes a source."""
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))

        # Add then remove
        runner.invoke(
            main,
            ["config", "add-source", "to-remove", "--type", "phoenix", "--url", "localhost:6006"],
        )
        result = runner.invoke(main, ["config", "remove-source", "to-remove", "--force"])

        assert result.exit_code == 0
        assert "Removed source: to-remove" in result.output


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
