#!/usr/bin/env python3
"""Content-presence sentinel for the span store (ENG2-1611, strand 3 "stable").

The same check AIT's `workspace_runner.capture_health` runs against Phoenix Postgres
every hour, pointed at the new store instead. Same thresholds, same exit codes, same
blind spots, so a verdict here means what it means there:

    0  healthy        1  breach        2  inconclusive (too few spans)        3  operational

What it measures, over the spans of the last N hours that carry an input value:

- the share whose input is the literal ``redacted-by-litellm`` (an EXACT match; a
  substring match once reported 6.8% when the truth was 0.00%, because the operator's
  own sessions discussed the marker);
- per span name, the p95 input length. Truncation moves a p95; a burst of tiny probe
  spans cannot. A name with fewer than ``--min-samples`` spans does not vote.

Reads `spans_raw` (verbatim attributes JSON) so it does not depend on the typed layout
being rebuilt. The store is resolved as `dal store show` does unless ``--store`` is given.

    uv run python scripts/store_health.py --hours 24
    uv run python scripts/store_health.py --store 's3://dal/spans?endpoint=minio:9000&tls=0'
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time

log = logging.getLogger("store_health")

REDACTION_MARKER = "redacted-by-litellm"
EXIT_HEALTHY, EXIT_BREACH, EXIT_INCONCLUSIVE, EXIT_OPERATIONAL = 0, 1, 2, 3

_INPUT = "json_extract_string(attributes, '$.input.value')"


def _query(store_uri: str | None, since: dt.datetime, marker: str) -> list[tuple]:
    import duckdb

    from dev_agent_lens.storage.spanstore import open_store

    t0 = time.perf_counter()
    store = open_store(store_uri)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    store.attach_duckdb(con)
    glob = store.read_glob("spans_raw")
    log.info("[store_health] store=%s since=%s", store.uri, since.isoformat())
    sql = f"""
        WITH sp AS (
            SELECT name, {_INPUT} AS input_value
            FROM read_parquet(?, hive_partitioning=true, union_by_name=true)
            WHERE start_time > ?::TIMESTAMPTZ
              AND {_INPUT} IS NOT NULL
        )
        SELECT name,
               count(*)                                     AS with_input,
               count(*) FILTER (WHERE input_value = ?)      AS redacted,
               quantile_disc(length(input_value), 0.5)      AS median_chars,
               quantile_disc(length(input_value), 0.95)     AS p95_chars
        FROM sp GROUP BY name ORDER BY count(*) DESC
    """
    rows = con.execute(sql, [glob, since.isoformat(), marker]).fetchall()
    log.info("[store_health] %d span names in %.1fms", len(rows), (time.perf_counter() - t0) * 1000)
    return rows


def evaluate(
    rows: list[tuple],
    *,
    max_redacted_pct: float,
    min_p95_chars: int,
    min_samples: int,
) -> tuple[int, list[str]]:
    """Same decision procedure as AIT capture_health.evaluate; kept in step by hand."""
    msgs: list[str] = []
    total = sum(r[1] for r in rows)
    if total == 0:
        return EXIT_INCONCLUSIVE, ["inconclusive: no input-bearing spans in the window"]
    redacted = sum(r[2] for r in rows)
    pct = 100.0 * redacted / total
    worst_name, worst_p95, worst_median = None, None, None
    for name, n, red, med, p95 in rows:
        share = 100.0 * red / n if n else 0.0
        med_s = med if med is not None else "-"
        p95_s = p95 if p95 is not None else "-"
        msgs.append(f"  {name:<40} n={n:<6} redacted={share:5.1f}%  median={med_s:<7} p95={p95_s}")
        if n >= min_samples and p95 is not None and (worst_p95 is None or p95 < worst_p95):
            worst_name, worst_p95, worst_median = name, p95, med
    rc = EXIT_HEALTHY
    if pct > max_redacted_pct:
        rc = EXIT_BREACH
        msgs.append(
            f"BREACH: {pct:.1f}% of input-bearing spans are redacted "
            f"(threshold {max_redacted_pct:.1f}%) - content capture is degraded"
        )
    if worst_p95 is not None and worst_p95 < min_p95_chars:
        rc = EXIT_BREACH
        msgs.append(
            f"BREACH: {worst_name} p95 input length {worst_p95} chars "
            f"(threshold {min_p95_chars}; median {worst_median}) - "
            "content is being truncated or replaced"
        )
    if rc == EXIT_HEALTHY:
        msgs.append(f"healthy: {pct:.1f}% redacted, {total} input-bearing spans")
    return rc, msgs


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--store", default=None, help="store URI; default resolves like `dal store show`"
    )
    ap.add_argument("--hours", type=float, default=24.0, help="lookback window in hours")
    ap.add_argument(
        "--since",
        default=os.environ.get("CAPTURE_HEALTH_SINCE") or None,
        help="absolute ISO-8601 floor; overrides --hours",
    )
    ap.add_argument("--max-redacted-pct", type=float, default=5.0)
    ap.add_argument("--min-p95-chars", type=int, default=100)
    ap.add_argument("--min-samples", type=int, default=30)
    ap.add_argument("--marker", default=REDACTION_MARKER)
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    args = _build_parser().parse_args(argv)
    if args.since:
        since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)
    try:
        rows = _query(args.store, since, args.marker)
    except Exception as e:  # noqa: BLE001 - an operational failure is its own exit code
        print(f"operational: {type(e).__name__}: {e}")
        return EXIT_OPERATIONAL
    rc, msgs = evaluate(
        rows,
        max_redacted_pct=args.max_redacted_pct,
        min_p95_chars=args.min_p95_chars,
        min_samples=args.min_samples,
    )
    print(f"store content presence since {since.isoformat()}:")
    print("\n".join(msgs))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
