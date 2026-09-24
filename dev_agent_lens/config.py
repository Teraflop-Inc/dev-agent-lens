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
        p = Path(env_path).expanduser()
        # core/sources.py reads the same variable as a DIRECTORY and mkdirs it, so by the
        # time this runs the path may already be one. Use the file inside it rather than
        # failing with IsADirectoryError -- the two meanings coexisted for months and only
        # collided once `dal store use` started writing config after `add-source`.
        return p / "config.json" if p.is_dir() else p
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

    # 0o600: the file can hold remote URLs and (for oxen) is read next to credentials
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
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


# --- span store: WHERE the Parquet trace store lives, and HOW it is shaped ---------
#
# Deliberately separate from BACKENDS in cli/main.py, which names trace *sources*
# (Phoenix, Arize). This is the physical store underneath.
#
# Resolution order, same as Oxen above: environment first so a one-off run can override
# without editing config, then the config file, then a sane local default.

DEFAULT_SPAN_STORE = "~/.dal/spans"
DEFAULT_LAYOUT = "raw"


def get_span_store() -> str:
    """Where the span store lives, as a URI.

    file:///var/lib/dal/spans                     a local directory (needs nothing)
    s3://bucket/prefix                            AWS
    s3://bucket/prefix?endpoint=host:9000&tls=0   MinIO, SeaweedFS, Ceph, on-prem gateway

    Credentials are never part of the URI; they come from the environment. That keeps a
    store URI safe to commit, log and paste into a ticket.
    """
    env = os.getenv("DAL_SPAN_STORE")
    if env:
        return env
    cfg = load_config().get("span_store", {})
    return cfg.get("uri") or os.path.expanduser(DEFAULT_SPAN_STORE)


def span_store_configured() -> bool:
    """True only if someone chose a store explicitly (env or `dal store use`).

    `dal sync` writes to the span store only in that case. The implicit ~/.dal/spans
    default is never written to by sync, so upgrading DAL does not silently start a second
    copy of every trace on disk.
    """
    if os.getenv("DAL_SPAN_STORE"):
        return True
    return bool(load_config().get("span_store", {}).get("uri"))


def set_span_store(uri: str) -> None:
    config = load_config()
    config.setdefault("span_store", {})["uri"] = uri
    save_config(config)


def get_span_layout() -> str:
    """`raw` (producer shape) or `typed` (~30 typed columns + overflow + blob store)."""
    layout = (os.getenv("DAL_SPAN_LAYOUT")
              or load_config().get("span_store", {}).get("layout")
              or DEFAULT_LAYOUT)
    if layout not in ("raw", "typed"):
        raise ValueError(f"span layout must be 'raw' or 'typed', got {layout!r} "
                         "(DAL_SPAN_LAYOUT or config span_store.layout)")
    return layout


def set_span_layout(layout: str) -> None:
    config = load_config()
    config.setdefault("span_store", {})["layout"] = layout
    save_config(config)


def get_zstd_level() -> int:
    """Compression level for span-store writes.

    Pinned rather than left to the writer: an unpinned level is how a published 9.6x
    storage figure turned out to be pyarrow's default (1) measured against DuckDB's (3).
    """
    raw = os.getenv("DAL_ZSTD_LEVEL")
    if raw is None:
        raw = load_config().get("span_store", {}).get("zstd_level")
    if raw is None:
        from dev_agent_lens.storage.spanstore.base import DEFAULT_ZSTD_LEVEL
        return DEFAULT_ZSTD_LEVEL
    try:
        lvl = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"zstd level must be an integer 1-22, got {raw!r}") from None
    if not 1 <= lvl <= 22:
        raise ValueError(f"zstd level must be 1-22, got {lvl}")
    return lvl


def set_zstd_level(level: int) -> None:
    config = load_config()
    config.setdefault("span_store", {})["zstd_level"] = int(level)
    save_config(config)
