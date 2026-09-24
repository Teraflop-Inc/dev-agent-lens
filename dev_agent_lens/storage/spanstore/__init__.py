"""Pluggable span-store backends.

    from dev_agent_lens.storage.spanstore import open_store
    store = open_store("s3://dal/spans?endpoint=127.0.0.1:9100&tls=0")
    store.ensure()
    print(store.probe())

A store URI must not contain credentials (those come from the environment) and is
validated at construction: anything that is not inert data — userinfo, quotes, statement
characters, an endpoint or bucket outside its legal alphabet — is rejected before it can
reach the engine.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from dev_agent_lens.storage.spanstore.base import (
    DEFAULT_ZSTD_LEVEL,
    SpanStore,
    StoreCapabilities,
    StoreHealth,
    WriteResult,
    dataset_of,
    quote_literal,
)
from dev_agent_lens.storage.spanstore.localfs import LocalSpanStore
from dev_agent_lens.storage.spanstore.s3 import S3SpanStore

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[SpanStore]] = {
    "file": LocalSpanStore,
    "": LocalSpanStore,  # a bare path is a local directory
    "s3": S3SpanStore,
    "s3a": S3SpanStore,
    "minio": S3SpanStore,  # alias, so a config can say what it means
    "gs": S3SpanStore,  # GCS via its S3-compatible endpoint
}

DEFAULT_STORE_ENV = "DAL_SPAN_STORE"


def open_store(uri: str | None = None) -> SpanStore:
    """Resolve a URI to a backend.

    With no URI, resolves exactly as `dal store show` does — environment, then config.json,
    then the default — via `config.get_span_store()`. The first version skipped config, so
    a library caller and the CLI disagreed the moment `dal store use` had been run.
    """
    if uri is None:
        from dev_agent_lens.config import get_span_store

        uri = get_span_store()
    scheme = urlparse(uri).scheme
    cls = _REGISTRY.get(scheme)
    if cls is None:
        raise ValueError(
            f"no span-store backend for scheme {scheme!r} (uri={uri!r}). "
            f"known: {sorted(k for k in _REGISTRY if k)}"
        )
    log.debug("[spanstore] %s -> %s", uri, cls.__name__)
    return cls(uri)


def register(scheme: str, cls: type[SpanStore]) -> None:
    """Add a backend. Exists so a deployment can bring its own without a fork."""
    _REGISTRY[scheme] = cls
    log.info("[spanstore] registered scheme=%s -> %s", scheme, cls.__name__)


__all__ = [
    "SpanStore",
    "StoreHealth",
    "StoreCapabilities",
    "WriteResult",
    "DEFAULT_ZSTD_LEVEL",
    "LocalSpanStore",
    "S3SpanStore",
    "open_store",
    "register",
    "quote_literal",
    "dataset_of",
    "DEFAULT_STORE_ENV",
]
