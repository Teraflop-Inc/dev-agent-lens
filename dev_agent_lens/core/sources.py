"""
Source Configuration Module

Manages named source profiles for Phoenix and Arize backends.
Each source represents a specific backend instance with its configuration.

Storage Structure:
    ~/.dal/config/sources.json - Source configuration file

Example sources.json:
    {
        "version": 1,
        "sources": {
            "phoenix-alex": {
                "type": "phoenix",
                "url": "localhost:6006",
                "project": "dev-agent-lens",
                "local_only": true
            },
            "arize-team": {
                "type": "arize",
                "space_key": "U3BhY2U6...",
                "model_id": "dev-agent-lens",
                "local_only": false
            }
        }
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


class SourceType(str, Enum):
    """Types of trace data sources."""

    PHOENIX = "phoenix"
    PHOENIX_POSTGRES = "phoenix-postgres"
    ARIZE = "arize"
    CLAUDE = "claude"


@dataclass
class SourceConfig:
    """Configuration for a named source.

    Attributes:
        name: Unique identifier for this source (e.g., "phoenix-alex")
        source_type: Type of backend (phoenix or arize)
        local_only: If True, this source won't be synced to Oxen remote
        url: For Phoenix - the server URL
        project: For Phoenix - the project name
        space_key: For Arize - the space key
        model_id: For Arize - the model ID
        api_key_env: Environment variable name for API key (optional override)
    """

    name: str
    source_type: SourceType
    local_only: bool = True

    # Phoenix-specific
    url: str | None = None
    project: str | None = None
    sqlite_container: str | None = None  # Docker container name for direct SQLite access

    # Phoenix-Postgres-specific
    connection_url: str | None = None  # postgres://user:pass@host:port/db
    schema: str | None = None  # default 'phoenix'

    # Arize-specific
    space_key: str | None = None
    model_id: str | None = None

    # Claude-specific
    claude_dir: str | None = None  # Custom ~/.claude path, defaults to ~/.claude/projects

    # Optional API key override
    api_key_env: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        data = {
            "type": self.source_type.value,
            "local_only": self.local_only,
        }

        if self.source_type == SourceType.PHOENIX:
            if self.url:
                data["url"] = self.url
            if self.project:
                data["project"] = self.project
            if self.sqlite_container:
                data["sqlite_container"] = self.sqlite_container
        elif self.source_type == SourceType.PHOENIX_POSTGRES:
            if self.connection_url:
                data["connection_url"] = self.connection_url
            if self.project:
                data["project"] = self.project
            if self.schema:
                data["schema"] = self.schema
        elif self.source_type == SourceType.ARIZE:
            if self.space_key:
                data["space_key"] = self.space_key
            if self.model_id:
                data["model_id"] = self.model_id
        elif self.source_type == SourceType.CLAUDE:
            if self.claude_dir:
                data["claude_dir"] = self.claude_dir

        if self.api_key_env:
            data["api_key_env"] = self.api_key_env

        return data

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> SourceConfig:
        """Create SourceConfig from dictionary."""
        source_type = SourceType(data.get("type", "phoenix"))

        return cls(
            name=name,
            source_type=source_type,
            local_only=data.get("local_only", True),
            url=data.get("url"),
            project=data.get("project"),
            sqlite_container=data.get("sqlite_container"),
            connection_url=data.get("connection_url"),
            schema=data.get("schema"),
            space_key=data.get("space_key"),
            model_id=data.get("model_id"),
            claude_dir=data.get("claude_dir"),
            api_key_env=data.get("api_key_env"),
        )

    def validate(self) -> list[str]:
        """Validate the source configuration.

        Returns:
            List of validation error messages (empty if valid).
        """
        errors = []

        if not self.name:
            errors.append("Source name is required")

        if self.source_type == SourceType.PHOENIX:
            if not self.url:
                errors.append("Phoenix source requires 'url'")
        elif self.source_type == SourceType.PHOENIX_POSTGRES:
            if not self.connection_url:
                errors.append("Phoenix-Postgres source requires 'connection_url'")
        elif self.source_type == SourceType.ARIZE:
            if not self.space_key:
                errors.append("Arize source requires 'space_key'")
            if not self.model_id:
                errors.append("Arize source requires 'model_id'")
        elif self.source_type == SourceType.CLAUDE:
            # Claude source has no required fields - defaults to ~/.claude/projects
            pass

        return errors

    def get_display_info(self) -> str:
        """Get a human-readable display string."""
        if self.source_type == SourceType.PHOENIX:
            return f"Phoenix @ {self.url or 'localhost:6006'}"
        elif self.source_type == SourceType.PHOENIX_POSTGRES:
            host = "?"
            if self.connection_url and "@" in self.connection_url:
                host = self.connection_url.rsplit("@", 1)[-1]
            return f"Phoenix-Postgres @ {host} (schema={self.schema or 'phoenix'})"
        elif self.source_type == SourceType.ARIZE:
            return f"Arize ({self.model_id or 'unknown'})"
        elif self.source_type == SourceType.CLAUDE:
            return f"Claude ({self.claude_dir or '~/.claude/projects'})"
        return f"{self.source_type.value}"


def get_default_config_path() -> Path:
    """Get the default config path for DAL."""
    env_path = os.getenv("DAL_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".dal" / "config"


@dataclass
class SourceManager:
    """Manages source configurations.

    Handles loading, saving, and querying source configurations.
    """

    config_path: Path = field(default_factory=get_default_config_path)
    _sources: dict[str, SourceConfig] = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)

    def __post_init__(self):
        """Ensure config directory exists."""
        self.config_path.mkdir(parents=True, exist_ok=True)

    @property
    def sources_file(self) -> Path:
        """Get the path to the sources configuration file."""
        return self.config_path / "sources.json"

    def _load(self) -> None:
        """Load sources from disk."""
        if self._loaded:
            return

        if not self.sources_file.exists():
            self._sources = {}
            self._loaded = True
            return

        try:
            with open(self.sources_file, "r") as f:
                data = json.load(f)

            sources_data = data.get("sources", {})
            self._sources = {
                name: SourceConfig.from_dict(name, config)
                for name, config in sources_data.items()
            }
            self._loaded = True

        except (json.JSONDecodeError, IOError) as e:
            # Log warning but don't fail - start with empty sources
            self._sources = {}
            self._loaded = True

    def _save(self) -> None:
        """Save sources to disk."""
        data = {
            "version": 1,
            "sources": {
                name: source.to_dict()
                for name, source in self._sources.items()
            },
        }

        with open(self.sources_file, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    def add_source(self, source: SourceConfig) -> None:
        """Add or update a source configuration.

        Args:
            source: The source configuration to add.

        Raises:
            ValueError: If the source configuration is invalid.
        """
        self._load()

        errors = source.validate()
        if errors:
            raise ValueError(f"Invalid source configuration: {', '.join(errors)}")

        self._sources[source.name] = source
        self._save()

    def remove_source(self, name: str) -> bool:
        """Remove a source by name.

        Args:
            name: The source name to remove.

        Returns:
            True if the source was removed, False if it didn't exist.
        """
        self._load()

        if name not in self._sources:
            return False

        del self._sources[name]
        self._save()
        return True

    def get_source(self, name: str) -> SourceConfig | None:
        """Get a source by name.

        Args:
            name: The source name.

        Returns:
            The source configuration, or None if not found.
        """
        self._load()
        return self._sources.get(name)

    def list_sources(self) -> list[SourceConfig]:
        """List all configured sources.

        Returns:
            List of all source configurations.
        """
        self._load()
        return list(self._sources.values())

    def get_sources_by_type(self, source_type: SourceType) -> list[SourceConfig]:
        """Get all sources of a specific type.

        Args:
            source_type: The type to filter by.

        Returns:
            List of matching source configurations.
        """
        self._load()
        return [s for s in self._sources.values() if s.source_type == source_type]

    def get_syncable_sources(self) -> list[SourceConfig]:
        """Get sources that can be synced to Oxen (local_only=False).

        Returns:
            List of sources that are not local-only.
        """
        self._load()
        return [s for s in self._sources.values() if not s.local_only]

    def has_sources(self) -> bool:
        """Check if any sources are configured.

        Returns:
            True if at least one source is configured.
        """
        self._load()
        return len(self._sources) > 0

    def reload(self) -> None:
        """Force reload from disk."""
        self._loaded = False
        self._load()


def create_source_from_env() -> list[SourceConfig]:
    """Create source configurations from environment variables.

    This provides backward compatibility with the old env-based configuration.
    Creates sources based on DAL_PHOENIX_URL, PHOENIX_SQL_DATABASE_URL, and
    ARIZE_API_KEY.

    Returns:
        List of sources created from environment variables.
    """
    sources = []

    # Check for Phoenix-Postgres (preferred when both REST + Postgres are set)
    pg_url = os.getenv("PHOENIX_SQL_DATABASE_URL")
    if pg_url:
        pg_schema = os.getenv("PHOENIX_SQL_DATABASE_SCHEMA", "phoenix")
        pg_project = os.getenv("DAL_PHOENIX_PROJECT", "dev-agent-lens")
        sources.append(
            SourceConfig(
                name="phoenix-postgres-default",
                source_type=SourceType.PHOENIX_POSTGRES,
                connection_url=pg_url,
                schema=pg_schema,
                project=pg_project,
                local_only=False,  # Shared Postgres is shareable
            )
        )

    # Check for Phoenix REST
    phoenix_url = os.getenv("DAL_PHOENIX_URL")
    if phoenix_url:
        phoenix_project = os.getenv("DAL_PHOENIX_PROJECT", "default")
        sources.append(
            SourceConfig(
                name="phoenix-default",
                source_type=SourceType.PHOENIX,
                url=phoenix_url,
                project=phoenix_project,
                local_only=True,  # Assume local Phoenix is local-only
            )
        )

    # Check for Arize
    arize_key = os.getenv("ARIZE_API_KEY")
    if arize_key:
        space_key = os.getenv("ARIZE_SPACE_KEY")
        model_id = os.getenv("ARIZE_MODEL_ID")
        if space_key and model_id:
            sources.append(
                SourceConfig(
                    name="arize-default",
                    source_type=SourceType.ARIZE,
                    space_key=space_key,
                    model_id=model_id,
                    local_only=False,  # Arize cloud is shareable
                )
            )

    return sources
