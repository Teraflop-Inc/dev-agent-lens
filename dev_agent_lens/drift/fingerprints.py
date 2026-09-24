"""Fingerprint I/O: the ONE way per-day structural fingerprints are read and written.

A fingerprint line is {"day": "YYYY-MM-DD", "paths": [{"path", "jtype", "n", "rows"}]}.
A path may appear more than once per day with different jsonb types ('string' on most
rows, 'null' on a few). Every consumer must sum `rows` across those entries; reading a
single entry produced a false coverage collapse on 2026-09-04. That summing lives here and
nowhere else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

THIN_DAY_ROWS = 20  # below this a day is noise, not evidence


@dataclass
class PathDay:
    types: set[str] = field(default_factory=set)
    rows: int = 0  # SUMMED across jsonb types: is the key present at all
    rows_nonnull: int = 0  # SUMMED across non-null types: is there anything to read
    cov: float = 0.0  # rows / the day's total. Presence.
    cov_nonnull: float = 0.0  # rows_nonnull / the day's total. Population. THIS is what a
    # typed column's coverage floor must be built on: a column that
    # went all-NULL is 100% present and 0% readable, and the first
    # version of this tracked only presence, so a null flood was
    # invisible to both classify and the detector. Review 2026-09-04.


@dataclass
class Day:
    day: str
    total: int
    paths: dict[str, PathDay]

    @property
    def live(self) -> bool:
        return self.total >= THIN_DAY_ROWS


def load(path: Path | str) -> dict[str, Day]:
    out: dict[str, Day] = {}
    for line in Path(path).open():
        if not line.strip():
            continue
        r = json.loads(line)
        acc: dict[str, PathDay] = {}
        for p in r["paths"]:
            pd_ = acc.setdefault(p["path"], PathDay())
            pd_.types.add(p["jtype"])
            pd_.rows += p["rows"]
            if p["jtype"] != "null":
                pd_.rows_nonnull += p["rows"]
        # total AFTER summing: a path split across types would otherwise be capped at 1.0
        # against its own largest fragment and quietly mis-scale every other path.
        total = max((pd_.rows for pd_ in acc.values()), default=0)
        for path, pd_ in acc.items():
            if total and pd_.rows > total:
                log.warning(
                    "[drift:fingerprints] %s %s rows=%d > total=%d", r["day"], path, pd_.rows, total
                )
            pd_.cov = pd_.rows / total if total else 0.0
            pd_.cov_nonnull = pd_.rows_nonnull / total if total else 0.0
        out[r["day"]] = Day(day=r["day"], total=total, paths=acc)
    log.info(
        "[drift:fingerprints] loaded %d days from %s (%d live)",
        len(out),
        path,
        sum(1 for d in out.values() if d.live),
    )
    return out


def live_days(days: dict[str, Day], *, drop_last: bool = True) -> list[str]:
    """Ordered live days. The last day is dropped by default because a sweep that runs
    mid-day records a partial day, and a partial day makes every path look like it vanished."""
    ordered = sorted(days)
    if drop_last and ordered:
        ordered = ordered[:-1]
    return [d for d in ordered if days[d].live]


def real_types(types: set[str]) -> set[str]:
    """'null' is nullability, not a type."""
    return {t for t in types if t != "null"}
