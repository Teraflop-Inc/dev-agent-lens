"""Local-directory span store: the tier-0 case, and the air-gap floor.

ENG2-1589's deployability target was "degrades to a directory for single-node." This is
that directory. It needs no extension, no network and no credentials, which makes it the
only backend guaranteed to work in a fully air-gapped install with nothing vendored.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from dev_agent_lens.storage.spanstore.base import (
    SpanStore,
    StoreCapabilities,
    StoreHealth,
    dataset_of,
)

log = logging.getLogger(__name__)


class LocalSpanStore(SpanStore):
    scheme = "file"

    def __init__(self, uri: str) -> None:
        super().__init__(uri)
        parsed = urlparse(uri)
        if parsed.scheme == "file" and parsed.netloc not in ("", "localhost"):
            # file://~/x and file://relative/x put the first segment in netloc and would
            # silently root at /x. Only an empty or 'localhost' authority is a path.
            raise ValueError(
                f"file:// URI must be file:///absolute/path (got authority {parsed.netloc!r})"
            )
        raw = parsed.path if parsed.scheme == "file" else uri
        if "'" in raw:
            raise ValueError("store path may not contain a quote character")
        self.root = Path(raw).expanduser().resolve()
        log.debug("[spanstore:file] root=%s", self.root)

    def read_glob(self, dataset: str = "spans") -> str:
        return f"{self.root / dataset}/**/*.parquet"

    def write_target(self, dataset: str = "spans") -> str:
        return str(self.root / dataset)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        log.info("[spanstore:file] ensured root=%s", self.root)

    def prepare_write(self, dataset: str) -> None:
        target = self.root / dataset
        target.mkdir(parents=True, exist_ok=True)
        log.debug("[spanstore:file] prepared %s", target)

    def clear(self, dataset: str = "spans") -> int:
        base = self.root / dataset
        if not base.exists():
            return 0
        n = sum(1 for _ in base.rglob("*.parquet"))
        shutil.rmtree(base)
        log.info("[spanstore:file] cleared %d file(s) under %s", n, base)
        return n

    def probe(self) -> StoreHealth:
        t0 = time.perf_counter()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".dal-probe"
            probe.write_text("ok")
            readable = probe.read_text() == "ok"
            probe.unlink()
            free = shutil.disk_usage(self.root).free
            el = (time.perf_counter() - t0) * 1000
            log.info(
                "[spanstore:file] probe ok root=%s free=%.1fGB in %.1fms", self.root, free / 1e9, el
            )
            return StoreHealth(
                ok=True,
                uri=self.uri,
                readable=readable,
                writable=True,
                detail=f"{free / 1e9:.1f} GB free",
                elapsed_ms=el,
            )
        except OSError as e:
            el = (time.perf_counter() - t0) * 1000
            log.warning("[spanstore:file] probe FAILED root=%s err=%s", self.root, e)
            return StoreHealth(ok=False, uri=self.uri, detail=str(e), elapsed_ms=el)

    def capabilities(self) -> StoreCapabilities:
        return StoreCapabilities(
            name="local directory",
            needs_network=False,
            needs_duckdb_extension=None,
            airgap_ready=True,
            notes="Tier 0. No extension, no credentials, no network. "
            "Backup and retention are the operator's problem.",
        )

    def size_bytes(self, dataset: str = "spans") -> int:
        base = self.root / dataset
        if not base.exists():
            return 0
        return sum(p.stat().st_size for p in base.rglob("*.parquet"))

    def list_datasets(self) -> list[str]:
        if not self.root.exists():
            return []
        found = {dataset_of(p.relative_to(self.root).parts) for p in self.root.rglob("*.parquet")}
        found.discard("")
        log.debug("[spanstore:file] datasets=%s", sorted(found))
        return sorted(found)
