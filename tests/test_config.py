"""
Tests for DAL Configuration Module.

These tests verify configuration management including:
- Loading and saving config
- Oxen remote configuration
- Environment variable fallback
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from dev_agent_lens.config import (
    get_config_path,
    get_oxen_remote,
    is_oxen_configured,
    load_config,
    save_config,
    set_oxen_remote,
)


class TestConfigPath:
    """Tests for config path resolution."""

    def test_default_config_path(self, tmp_path, monkeypatch):
        """Given no env var, uses ~/.dal/config.json."""
        monkeypatch.delenv("DAL_CONFIG_PATH", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        path = get_config_path()

        assert path == tmp_path / ".dal" / "config.json"

    def test_env_var_override(self, tmp_path, monkeypatch):
        """Given DAL_CONFIG_PATH, uses that path."""
        custom_path = tmp_path / "custom" / "config.json"
        monkeypatch.setenv("DAL_CONFIG_PATH", str(custom_path))

        path = get_config_path()

        assert path == custom_path


class TestLoadSaveConfig:
    """Tests for loading and saving configuration."""

    def test_load_empty_when_no_file(self, tmp_path, monkeypatch):
        """Given no config file, returns empty dict."""
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        config = load_config()

        assert config == {}

    def test_load_existing_config(self, tmp_path, monkeypatch):
        """Given config file exists, loads it."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"oxen": {"remote_url": "test.com/repo"}}')
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        config = load_config()

        assert config == {"oxen": {"remote_url": "test.com/repo"}}

    def test_load_invalid_json_returns_empty(self, tmp_path, monkeypatch):
        """Given invalid JSON, returns empty dict."""
        config_path = tmp_path / "config.json"
        config_path.write_text("not valid json {{{")
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        config = load_config()

        assert config == {}

    def test_save_creates_parent_directory(self, tmp_path, monkeypatch):
        """Given non-existent parent, creates it."""
        config_path = tmp_path / "subdir" / "config.json"
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        save_config({"test": "value"})

        assert config_path.exists()
        assert json.loads(config_path.read_text()) == {"test": "value"}

    def test_save_overwrites_existing(self, tmp_path, monkeypatch):
        """Given existing config, overwrites it."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"old": "value"}')
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        save_config({"new": "value"})

        assert json.loads(config_path.read_text()) == {"new": "value"}


class TestOxenRemote:
    """Tests for Oxen remote configuration."""

    def test_get_oxen_remote_from_env(self, tmp_path, monkeypatch):
        """Given OXEN_REMOTE_URL env var, returns it."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "hub.oxen.ai/env/repo")
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config.json"))

        remote = get_oxen_remote()

        assert remote == "hub.oxen.ai/env/repo"

    def test_get_oxen_remote_from_config(self, tmp_path, monkeypatch):
        """Given config but no env, returns from config."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"oxen": {"remote_url": "hub.oxen.ai/config/repo"}}')
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        remote = get_oxen_remote()

        assert remote == "hub.oxen.ai/config/repo"

    def test_get_oxen_remote_env_takes_precedence(self, tmp_path, monkeypatch):
        """Given both env and config, env takes precedence."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "hub.oxen.ai/env/repo")
        config_path = tmp_path / "config.json"
        config_path.write_text('{"oxen": {"remote_url": "hub.oxen.ai/config/repo"}}')
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        remote = get_oxen_remote()

        assert remote == "hub.oxen.ai/env/repo"

    def test_get_oxen_remote_none_when_not_configured(self, tmp_path, monkeypatch):
        """Given no env or config, returns None."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config.json"))

        remote = get_oxen_remote()

        assert remote is None

    def test_set_oxen_remote_creates_config(self, tmp_path, monkeypatch):
        """Given no existing config, creates it with oxen remote."""
        config_path = tmp_path / "config.json"
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)

        set_oxen_remote("hub.oxen.ai/new/repo")

        config = json.loads(config_path.read_text())
        assert config == {"oxen": {"remote_url": "hub.oxen.ai/new/repo"}}

    def test_set_oxen_remote_preserves_other_config(self, tmp_path, monkeypatch):
        """Given existing config, preserves other keys."""
        config_path = tmp_path / "config.json"
        config_path.write_text('{"other": "value", "oxen": {"old": "key"}}')
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)

        set_oxen_remote("hub.oxen.ai/new/repo")

        config = json.loads(config_path.read_text())
        assert config["other"] == "value"
        assert config["oxen"]["remote_url"] == "hub.oxen.ai/new/repo"


class TestIsOxenConfigured:
    """Tests for checking if Oxen is configured."""

    def test_is_configured_with_env(self, tmp_path, monkeypatch):
        """Given OXEN_REMOTE_URL, returns True."""
        monkeypatch.setenv("OXEN_REMOTE_URL", "hub.oxen.ai/test")
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config.json"))

        assert is_oxen_configured() is True

    def test_is_configured_with_config(self, tmp_path, monkeypatch):
        """Given config file with remote, returns True."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"oxen": {"remote_url": "hub.oxen.ai/test"}}')
        monkeypatch.setenv("DAL_CONFIG_PATH", str(config_path))

        assert is_oxen_configured() is True

    def test_is_not_configured(self, tmp_path, monkeypatch):
        """Given no env or config, returns False."""
        monkeypatch.delenv("OXEN_REMOTE_URL", raising=False)
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config.json"))

        assert is_oxen_configured() is False
