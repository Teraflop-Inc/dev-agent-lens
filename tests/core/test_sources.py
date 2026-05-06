"""
Tests for Source Configuration Module.

These tests verify source configuration storage, validation, and management.
"""

from __future__ import annotations

import json

import pytest

from dev_agent_lens.core.sources import (
    SourceConfig,
    SourceManager,
    SourceType,
    create_source_from_env,
)


class TestSourceConfig:
    """Tests for SourceConfig dataclass."""

    def test_phoenix_source_to_dict(self):
        """Given a Phoenix source, to_dict returns expected structure."""
        source = SourceConfig(
            name="phoenix-test",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
            project="test-project",
            local_only=True,
        )

        result = source.to_dict()

        assert result["type"] == "phoenix"
        assert result["url"] == "localhost:6006"
        assert result["project"] == "test-project"
        assert result["local_only"] is True

    def test_arize_source_to_dict(self):
        """Given an Arize source, to_dict returns expected structure."""
        source = SourceConfig(
            name="arize-test",
            source_type=SourceType.ARIZE,
            space_key="ABC123",
            model_id="my-model",
            local_only=False,
        )

        result = source.to_dict()

        assert result["type"] == "arize"
        assert result["space_key"] == "ABC123"
        assert result["model_id"] == "my-model"
        assert result["local_only"] is False

    def test_from_dict_phoenix(self):
        """Given Phoenix dict, from_dict creates correct source."""
        data = {
            "type": "phoenix",
            "url": "localhost:6006",
            "project": "dev-project",
            "local_only": True,
        }

        source = SourceConfig.from_dict("my-phoenix", data)

        assert source.name == "my-phoenix"
        assert source.source_type == SourceType.PHOENIX
        assert source.url == "localhost:6006"
        assert source.project == "dev-project"
        assert source.local_only is True

    def test_from_dict_arize(self):
        """Given Arize dict, from_dict creates correct source."""
        data = {
            "type": "arize",
            "space_key": "XYZ789",
            "model_id": "test-model",
            "local_only": False,
        }

        source = SourceConfig.from_dict("my-arize", data)

        assert source.name == "my-arize"
        assert source.source_type == SourceType.ARIZE
        assert source.space_key == "XYZ789"
        assert source.model_id == "test-model"
        assert source.local_only is False

    def test_validate_phoenix_valid(self):
        """Given valid Phoenix source, validate returns no errors."""
        source = SourceConfig(
            name="phoenix-test",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        )

        errors = source.validate()

        assert errors == []

    def test_validate_phoenix_missing_url(self):
        """Given Phoenix source without URL, validate returns error."""
        source = SourceConfig(
            name="phoenix-test",
            source_type=SourceType.PHOENIX,
        )

        errors = source.validate()

        assert len(errors) == 1
        assert "url" in errors[0].lower()

    def test_validate_arize_valid(self):
        """Given valid Arize source, validate returns no errors."""
        source = SourceConfig(
            name="arize-test",
            source_type=SourceType.ARIZE,
            space_key="ABC",
            model_id="model",
        )

        errors = source.validate()

        assert errors == []

    def test_validate_arize_missing_space_key(self):
        """Given Arize source without space_key, validate returns error."""
        source = SourceConfig(
            name="arize-test",
            source_type=SourceType.ARIZE,
            model_id="model",
        )

        errors = source.validate()

        assert len(errors) == 1
        assert "space_key" in errors[0].lower()

    def test_validate_arize_missing_model_id(self):
        """Given Arize source without model_id, validate returns error."""
        source = SourceConfig(
            name="arize-test",
            source_type=SourceType.ARIZE,
            space_key="ABC",
        )

        errors = source.validate()

        assert len(errors) == 1
        assert "model_id" in errors[0].lower()

    def test_get_display_info_phoenix(self):
        """Phoenix source displays correctly."""
        source = SourceConfig(
            name="phoenix-test",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        )

        assert "Phoenix" in source.get_display_info()
        assert "localhost:6006" in source.get_display_info()

    def test_get_display_info_arize(self):
        """Arize source displays correctly."""
        source = SourceConfig(
            name="arize-test",
            source_type=SourceType.ARIZE,
            model_id="my-model",
        )

        assert "Arize" in source.get_display_info()
        assert "my-model" in source.get_display_info()


class TestSourceManager:
    """Tests for SourceManager class."""

    def test_add_source_creates_file(self, tmp_path):
        """Adding source creates sources.json file."""
        manager = SourceManager(config_path=tmp_path)
        source = SourceConfig(
            name="test-source",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        )

        manager.add_source(source)

        assert (tmp_path / "sources.json").exists()

    def test_add_source_persists(self, tmp_path):
        """Added source persists to disk."""
        manager = SourceManager(config_path=tmp_path)
        source = SourceConfig(
            name="persistent-source",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        )
        manager.add_source(source)

        # Create new manager to reload from disk
        manager2 = SourceManager(config_path=tmp_path)
        loaded = manager2.get_source("persistent-source")

        assert loaded is not None
        assert loaded.name == "persistent-source"
        assert loaded.url == "localhost:6006"

    def test_get_source_not_found(self, tmp_path):
        """Getting non-existent source returns None."""
        manager = SourceManager(config_path=tmp_path)

        result = manager.get_source("nonexistent")

        assert result is None

    def test_list_sources_empty(self, tmp_path):
        """Listing sources when none configured returns empty list."""
        manager = SourceManager(config_path=tmp_path)

        result = manager.list_sources()

        assert result == []

    def test_list_sources_multiple(self, tmp_path):
        """Listing sources returns all configured sources."""
        manager = SourceManager(config_path=tmp_path)
        manager.add_source(SourceConfig(
            name="source1",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        ))
        manager.add_source(SourceConfig(
            name="source2",
            source_type=SourceType.ARIZE,
            space_key="ABC",
            model_id="model",
        ))

        result = manager.list_sources()

        assert len(result) == 2
        names = [s.name for s in result]
        assert "source1" in names
        assert "source2" in names

    def test_remove_source(self, tmp_path):
        """Removing source deletes it."""
        manager = SourceManager(config_path=tmp_path)
        manager.add_source(SourceConfig(
            name="to-remove",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        ))

        removed = manager.remove_source("to-remove")

        assert removed is True
        assert manager.get_source("to-remove") is None

    def test_remove_source_not_found(self, tmp_path):
        """Removing non-existent source returns False."""
        manager = SourceManager(config_path=tmp_path)

        removed = manager.remove_source("nonexistent")

        assert removed is False

    def test_add_source_validates(self, tmp_path):
        """Adding invalid source raises ValueError."""
        manager = SourceManager(config_path=tmp_path)
        invalid_source = SourceConfig(
            name="invalid",
            source_type=SourceType.PHOENIX,
            # Missing required url
        )

        with pytest.raises(ValueError):
            manager.add_source(invalid_source)

    def test_get_sources_by_type(self, tmp_path):
        """Getting sources by type filters correctly."""
        manager = SourceManager(config_path=tmp_path)
        manager.add_source(SourceConfig(
            name="phoenix1",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        ))
        manager.add_source(SourceConfig(
            name="arize1",
            source_type=SourceType.ARIZE,
            space_key="ABC",
            model_id="model",
        ))

        phoenix_sources = manager.get_sources_by_type(SourceType.PHOENIX)
        arize_sources = manager.get_sources_by_type(SourceType.ARIZE)

        assert len(phoenix_sources) == 1
        assert phoenix_sources[0].name == "phoenix1"
        assert len(arize_sources) == 1
        assert arize_sources[0].name == "arize1"

    def test_get_syncable_sources(self, tmp_path):
        """Getting syncable sources returns only non-local-only."""
        manager = SourceManager(config_path=tmp_path)
        manager.add_source(SourceConfig(
            name="local",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
            local_only=True,
        ))
        manager.add_source(SourceConfig(
            name="shared",
            source_type=SourceType.ARIZE,
            space_key="ABC",
            model_id="model",
            local_only=False,
        ))

        syncable = manager.get_syncable_sources()

        assert len(syncable) == 1
        assert syncable[0].name == "shared"

    def test_has_sources(self, tmp_path):
        """has_sources returns correct boolean."""
        manager = SourceManager(config_path=tmp_path)

        assert manager.has_sources() is False

        manager.add_source(SourceConfig(
            name="test",
            source_type=SourceType.PHOENIX,
            url="localhost:6006",
        ))

        assert manager.has_sources() is True


class TestCreateSourceFromEnv:
    """Tests for create_source_from_env function."""

    def test_no_env_vars_returns_empty(self, monkeypatch):
        """With no env vars set, returns empty list."""
        monkeypatch.delenv("DAL_PHOENIX_URL", raising=False)
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = create_source_from_env()

        assert result == []

    def test_phoenix_env_creates_source(self, monkeypatch):
        """With DAL_PHOENIX_URL set, creates Phoenix source."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "localhost:6006")
        monkeypatch.setenv("DAL_PHOENIX_PROJECT", "test-project")
        monkeypatch.delenv("ARIZE_API_KEY", raising=False)

        result = create_source_from_env()

        assert len(result) == 1
        assert result[0].name == "phoenix-default"
        assert result[0].source_type == SourceType.PHOENIX
        assert result[0].url == "localhost:6006"
        assert result[0].project == "test-project"

    def test_arize_env_creates_source(self, monkeypatch):
        """With ARIZE_API_KEY set, creates Arize source."""
        monkeypatch.delenv("DAL_PHOENIX_URL", raising=False)
        monkeypatch.setenv("ARIZE_API_KEY", "test-key")
        monkeypatch.setenv("ARIZE_SPACE_KEY", "ABC123")
        monkeypatch.setenv("ARIZE_MODEL_ID", "test-model")

        result = create_source_from_env()

        assert len(result) == 1
        assert result[0].name == "arize-default"
        assert result[0].source_type == SourceType.ARIZE
        assert result[0].space_key == "ABC123"
        assert result[0].model_id == "test-model"

    def test_both_env_creates_both_sources(self, monkeypatch):
        """With both env vars set, creates both sources."""
        monkeypatch.setenv("DAL_PHOENIX_URL", "localhost:6006")
        monkeypatch.setenv("ARIZE_API_KEY", "test-key")
        monkeypatch.setenv("ARIZE_SPACE_KEY", "ABC123")
        monkeypatch.setenv("ARIZE_MODEL_ID", "test-model")

        result = create_source_from_env()

        assert len(result) == 2
        names = [s.name for s in result]
        assert "phoenix-default" in names
        assert "arize-default" in names
