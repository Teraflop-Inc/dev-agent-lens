"""Storage modules for trace data persistence."""

from pathlib import Path

from dev_agent_lens.storage.oxen_store import OxenStore, get_default_data_path


def get_storage_path() -> Path:
    """Get the default storage path for DAL data.

    Returns:
        Path to the data storage directory.
    """
    return get_default_data_path()


__all__ = ["OxenStore", "get_storage_path"]
