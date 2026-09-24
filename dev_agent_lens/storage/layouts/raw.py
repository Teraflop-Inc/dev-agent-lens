"""Raw layout: spans as the producer emits them, `attributes` as a JSON string.

The incumbent shape, and the thing any migration has to beat. Kept as a first-class layout
rather than "the fixture" so a comparison against it is re-runnable at pinned settings.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from dev_agent_lens.storage.layouts.base import BuildResult, Layout
from dev_agent_lens.storage.spanstore.base import quote_literal

log = logging.getLogger(__name__)


class RawLayout(Layout):
    name = "raw"

    def build(
        self, con: Any, source_glob: str, store: Any, *, zstd_level: int, deterministic: bool = True
    ) -> BuildResult:
        t0 = time.perf_counter()
        w = store.copy_from_glob(
            con,
            source_glob,
            dataset="spans_raw",
            partition_by="day",
            compression_level=zstd_level,
            deterministic=deterministic,
        )
        r = BuildResult(
            layout=self.name,
            rows=w.rows,
            bytes_hot=w.bytes_written,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            zstd_level=zstd_level,
            detail={"partitions": w.partitions},
        )
        log.info(
            "[layout:raw] built rows=%d hot=%.1fMB level=%d in %.1fs",
            r.rows,
            r.bytes_hot / 1e6,
            zstd_level,
            r.elapsed_ms / 1000,
        )
        return r

    def attach(self, con: Any, store: Any) -> None:
        store.attach_duckdb(con)
        con.execute("DROP VIEW IF EXISTS spans")
        con.execute(f"""CREATE VIEW spans AS SELECT * FROM read_parquet(
            {quote_literal(store.read_glob("spans_raw"))},
            hive_partitioning=true, union_by_name=true)""")
