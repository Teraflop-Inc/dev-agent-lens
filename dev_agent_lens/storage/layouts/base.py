"""Common interface for span-store layouts."""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class BuildResult:
    layout: str
    rows: int
    bytes_hot: int
    bytes_cold: int = 0
    elapsed_ms: float = 0.0
    zstd_level: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def bytes_total(self) -> int:
        return self.bytes_hot + self.bytes_cold


class Layout(abc.ABC):
    name: str = ""

    @abc.abstractmethod
    def build(
        self, con: Any, source_glob: str, store: Any, *, zstd_level: int, deterministic: bool = True
    ) -> BuildResult:
        """Materialise this layout from `source_glob` into `store`.

        `zstd_level` is required, not defaulted, because an unpinned level is how the
        published 9.6x figure went wrong. `deterministic` pins one writer thread so a size
        figure is reproducible.
        """

    @abc.abstractmethod
    def attach(self, con: Any, store: Any) -> None:
        """Create the views a recipe queries. Every layout must expose `spans`."""

    def _timed(self, label: str):
        log.info("[layout:%s] %s start", self.name, label)
        return time.perf_counter()
