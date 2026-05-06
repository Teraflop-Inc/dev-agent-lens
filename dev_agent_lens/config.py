"""
DAL Configuration Module

Manages DAL configuration stored in ~/.dal/config.json.
Handles Oxen remote settings and other user preferences.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def get_config_path() -> Path:
    """Get the path to the DAL config file."""
    env_path = os.getenv("DAL_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".dal" / "config.json"


def load_config() -> dict[str, Any]:
    """Load DAL configuration from disk.

    Returns:
        Configuration dictionary. Empty dict if no config exists.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return {}

    try:
        with open(config_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    """Save DAL configuration to disk.

    Args:
        config: Configuration dictionary to save.
    """
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def get_oxen_remote() -> str | None:
    """Get the configured Oxen remote URL.

    Returns:
        Oxen remote URL if configured, None otherwise.
    """
    # Check environment variable first
    env_remote = os.getenv("OXEN_REMOTE_URL")
    if env_remote:
        return env_remote

    # Fall back to config file
    config = load_config()
    return config.get("oxen", {}).get("remote_url")


def set_oxen_remote(remote_url: str) -> None:
    """Set the Oxen remote URL in config.

    Args:
        remote_url: The Oxen remote URL (e.g., hub.oxen.ai/team/repo)
    """
    config = load_config()
    if "oxen" not in config:
        config["oxen"] = {}
    config["oxen"]["remote_url"] = remote_url
    save_config(config)


def is_oxen_configured() -> bool:
    """Check if Oxen is configured.

    Returns:
        True if Oxen remote URL is set (via env or config).
    """
    return get_oxen_remote() is not None
