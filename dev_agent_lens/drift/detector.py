"""Producer-drift detector: a contract of path + type + MINIMUM coverage.

Coverage is what catches a rename. A rename raises no type error and removes no key the
checker knows about: the old key vanishes (looking like a harmless deletion) and the typed
column silently goes NULL. Only a coverage floor sees it.

Known weakness, found 2026-09-04: the floor is the minimum over the baseline window, so a
collapse that BEGINS inside the baseline is learned as normal and never fires. Build the
baseline from a window `classify` has already called clean, not from an arbitrary range.
Not yet enforced in code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from dev_agent_lens.drift.classify import typed_column
from dev_agent_lens.drift.fingerprints import Day, live_days, real_types

log = logging.getLogger(__name__)


def build_contract(days: dict[str, Day], lo: str, hi: str) -> dict:
    window = [d for d in sorted(days) if lo <= d <= hi and days[d].live]
    if not window:
        raise ValueError(f"no live days in {lo}..{hi}")
    acc: dict[str, dict] = {}
    for d in window:
        for path, pd_ in days[d].paths.items():
            a = acc.setdefault(path, {"types": set(), "min_cov": 1.0, "days": 0})
            a["types"] |= real_types(pd_.types)
            a["min_cov"] = min(a["min_cov"], pd_.cov_nonnull)  # population, not presence
            a["days"] += 1
    n = len(window)
    paths = {
        p: {
            "types": sorted(a["types"]),
            "min_cov": round(a["min_cov"], 4),
            "stable": a["days"] == n,
            "typed": typed_column(p) is not None,
        }
        for p, a in acc.items()
    }
    log.info(
        "[drift:contract] %s..%s %d days, %d paths (%d typed, %d stable)",
        window[0],
        window[-1],
        n,
        len(paths),
        sum(v["typed"] for v in paths.values()),
        sum(v["stable"] for v in paths.values()),
    )
    return {"baseline": {"from": window[0], "to": window[-1], "days": n}, "paths": paths}


def check(day: Day, contract: dict, *, drop_factor: float = 0.5) -> list[tuple[str, str, str, str]]:
    """(severity, kind, path, note). ERROR = a typed column is now unreadable or unpopulated."""
    f = []
    for path, spec in contract["paths"].items():
        if not spec["stable"]:
            continue
        sev = "ERROR" if spec["typed"] else "WARN"
        seen = day.paths.get(path)
        if seen is None:
            f.append(
                (
                    sev,
                    "DISAPPEARED",
                    path,
                    "typed column reads NULL for every row" if spec["typed"] else "path absent",
                )
            )
            continue
        got = real_types(seen.types)
        if spec["types"] and not got:
            f.append(
                (
                    sev,
                    "NULLED",
                    path,
                    f"was {'|'.join(spec['types'])}, now null on every row "
                    f"({seen.rows}/{day.total} rows carry the key)",
                )
            )
        elif got and spec["types"] and not got & set(spec["types"]):
            f.append(
                (
                    sev,
                    "RETYPED",
                    path,
                    f"{'|'.join(spec['types'])} -> {'|'.join(sorted(got))}"
                    + ("; TRY_CAST returns NULL" if spec["typed"] else ""),
                )
            )
        elif got and not spec["types"]:
            f.append(
                (
                    "INFO",
                    "POPULATED",
                    path,
                    f"was null-only in baseline, now {'|'.join(sorted(got))}",
                )
            )
        if spec["min_cov"] > 0 and seen.cov_nonnull < spec["min_cov"] * drop_factor:
            f.append(
                (
                    sev,
                    "COVERAGE_DROP",
                    path,
                    f"{spec['min_cov']:.1%} baseline -> {seen.cov_nonnull:.1%} non-null "
                    f"({seen.rows_nonnull}/{day.total} rows)",
                )
            )
    for path in day.paths:
        if path not in contract["paths"]:
            f.append(("INFO", "NEW_PATH", path, "lands in attributes_rest verbatim"))
    return f


def backtest(days: dict[str, Day], contract: dict) -> dict[str, list]:
    hi = contract["baseline"]["to"]
    out = {}
    # live_days drops the partial last day; checking it made every path look DISAPPEARED
    # on any mid-day sweep, which is the documented normal case.
    for d in live_days(days):
        if d <= hi:
            continue
        findings = check(days[d], contract)
        if findings:
            out[d] = findings
    return out


def save(contract: dict, path: Path | str) -> None:
    Path(path).write_text(json.dumps(contract, indent=1))


def load(path: Path | str) -> dict:
    return json.loads(Path(path).read_text())
