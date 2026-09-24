"""Physical layouts for the span store: how the same trace data is shaped on disk.

Two exist, and DAL needs both runnable side by side:

  raw    spans as the producer emits them, `attributes` as a JSON string. What we have
         today, and the thing a migration has to beat.
  typed  ~30 typed columns, a verbatim JSON overflow for the tail, and the large payload
         moved to a content-addressed blob store. ENG2-1589's proposal.

They live behind one interface so a comparison between them can be a single command at
pinned settings. That is not incidental. ENG2-1589 published a **9.6x** storage win for the
typed layout that turned out to be an artefact: the raw side was written by pyarrow at zstd
level 1 and the typed side by DuckDB at level 3, in two different scratch scripts, and
nobody could re-run them together. At a matched level the honest figures are:

    level 1   raw 2112.6 MB   typed 174.5 MB + 163.8 MB blobs   12.1x hot / 6.2x total
    level 3   raw  443.7 MB   typed 137.7 MB + 100.9 MB blobs    3.2x hot / 1.9x total
    level 9   raw  204.8 MB   typed 123.4 MB +  82.9 MB blobs    1.7x hot / 1.0x total

At level 9 the typed layout is **size-neutral**. Content-addressing the payload removes by
hand exactly the redundancy a strong compressor already finds across a column, so the
storage case for typing evaporates once compression is set properly.

The query case does not. Typing removes a per-row JSON parse, which is worth 128x on
DuckDB and is the difference between DataFusion expressing our queries and not. Type for
speed and expressiveness; do not sell it on bytes.
"""

from __future__ import annotations

from dev_agent_lens.storage.layouts.base import BuildResult, Layout
from dev_agent_lens.storage.layouts.raw import RawLayout
from dev_agent_lens.storage.layouts.typed import TypedLayout

LAYOUTS: dict[str, type[Layout]] = {"raw": RawLayout, "typed": TypedLayout}


def get_layout(name: str) -> Layout:
    if name not in LAYOUTS:
        raise ValueError(f"unknown layout {name!r}; known: {sorted(LAYOUTS)}")
    return LAYOUTS[name]()


__all__ = ["Layout", "BuildResult", "RawLayout", "TypedLayout", "LAYOUTS", "get_layout"]
