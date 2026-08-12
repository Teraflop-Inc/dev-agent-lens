"""ENG2-1510: content-presence sentinel for the litellm->Phoenix capture path.

The companion to span_size_sentinel.py, pointed the other way.

span_size_sentinel only alarms when spans get BIGGER. The 2026-08-03 outage
made them smaller: `callback_settings.arize_phoenix.message_logging: false`
replaced every request body with the literal "redacted-by-litellm" (19 chars).
The size sentinel scored that as an improvement and stayed quiet, which is why
a total content blackout ran for nine days with a sentinel already deployed.

This checks the floor instead of the ceiling: are we still capturing content at
all? It is deliberately the inverse assertion, not an extension of ENG2-1476 --
the two must both be green for the pipeline to be healthy, and neither can
detect the other's failure.

IMPORTANT -- denominator. Only spans that actually carry an `input.value` field
are counted. `Claude_Code_Final_Output_0` is an output span with no input field
at all; including it makes a total blackout look like a 66% one, which is
exactly the misreading that happened during the ENG2-1510 investigation.

Usage:
  uv run python scripts/capture_health.py                 # last 24 hours
  uv run python scripts/capture_health.py --hours 1       # post-deploy check
  uv run python scripts/capture_health.py --max-redacted-pct 5 --min-median-chars 100

DSN comes from $PHOENIX_SQL_DATABASE_URL (fallback: ../.env). Read-only.
Exit codes: 0 = healthy, 1 = capture breach, 2 = no spans in window (inconclusive).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import psycopg

log = logging.getLogger("capture_health")

REDACTION_MARKER = "redacted-by-litellm"

# Only spans that carry an input field participate. See the docstring.
_HAS_INPUT = "attributes -> 'input' ->> 'value' IS NOT NULL"
_IS_REDACTED = f"attributes -> 'input' ->> 'value' LIKE '%%{REDACTION_MARKER}%%'"

_SQL = f"""
    SELECT name,
           count(*)                                              AS with_input,
           count(*) FILTER (WHERE {_IS_REDACTED})                AS redacted,
           percentile_disc(0.5) WITHIN GROUP (
               ORDER BY length(attributes -> 'input' ->> 'value')
           )                                                     AS median_chars
    FROM phoenix.spans
    WHERE start_time > now() - (%(window)s)::interval
      AND {_HAS_INPUT}
    GROUP BY name
    ORDER BY count(*) DESC
"""


def dsn_from_env() -> str:
    dsn = os.environ.get("PHOENIX_SQL_DATABASE_URL")
    if dsn:
        return dsn
    env_path = Path(__file__).resolve().parent.parent / ".env"
    m = re.search(
        r'^\s*PHOENIX_SQL_DATABASE_URL=("?)(.+?)\1\s*$', env_path.read_text(), re.M
    )
    if not m:
        raise SystemExit("PHOENIX_SQL_DATABASE_URL not in env or ../.env")
    return m.group(2)


def evaluate(
    rows: list[tuple[str, int, int, int | None]],
    *,
    max_redacted_pct: float,
    min_median_chars: int,
) -> tuple[int, list[str]]:
    """Pure verdict logic so it can be unit-tested without a database.

    Returns (exit_code, messages).
    """
    msgs: list[str] = []
    if not rows:
        return 2, ["no spans with an input field in the window - inconclusive"]

    total = sum(r[1] for r in rows)
    redacted = sum(r[2] for r in rows)
    pct = 100.0 * redacted / total if total else 0.0
    # Weighted-ish: report the worst median across span types, which is the one
    # that would be hiding a partial regression.
    medians = [r[3] for r in rows if r[3] is not None]
    worst_median = min(medians) if medians else 0

    for name, with_input, red, med in rows:
        share = 100.0 * red / with_input if with_input else 0.0
        msgs.append(
            f"  {name[:38]:<40} input-bearing={with_input:<7} "
            f"redacted={share:5.1f}%  median={med if med is not None else '-'}"
        )

    breach = False
    if pct > max_redacted_pct:
        breach = True
        msgs.append(
            f"BREACH: {pct:.1f}% of input-bearing spans are redacted "
            f"(threshold {max_redacted_pct:.1f}%) - content capture is degraded"
        )
    if worst_median < min_median_chars:
        breach = True
        msgs.append(
            f"BREACH: worst median input length is {worst_median} chars "
            f"(threshold {min_median_chars}) - inputs are being truncated or replaced"
        )
    if not breach:
        msgs.append(
            f"healthy: {pct:.1f}% redacted, worst median {worst_median} chars, "
            f"{total} input-bearing spans"
        )
    return (1 if breach else 0), msgs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24.0, help="lookback window in hours")
    ap.add_argument(
        "--max-redacted-pct",
        type=float,
        default=5.0,
        help="fail if more than this %% of input-bearing spans are redacted",
    )
    ap.add_argument(
        "--min-median-chars",
        type=int,
        default=100,
        help="fail if the worst per-span-name median input length falls below this",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    t0 = time.perf_counter()
    log.info(
        "[capture] start window=%.1fh thresholds redacted<=%.1f%% median>=%d",
        args.hours, args.max_redacted_pct, args.min_median_chars,
    )

    with psycopg.connect(dsn_from_env()) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '120s'")
        cur.execute(_SQL, {"window": f"{args.hours} hours"})
        rows = cur.fetchall()
    log.info("[capture] query done in %.1fms", (time.perf_counter() - t0) * 1000)

    code, msgs = evaluate(
        rows,
        max_redacted_pct=args.max_redacted_pct,
        min_median_chars=args.min_median_chars,
    )
    for m in msgs:
        (log.error if m.startswith("BREACH") else log.info)("[capture] %s", m)
    log.info("[capture] verdict exit=%d in %.1fms", code, (time.perf_counter() - t0) * 1000)
    return code


if __name__ == "__main__":
    sys.exit(main())
