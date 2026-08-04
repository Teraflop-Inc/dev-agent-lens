"""ENG2-1476: regression sentinel for Phoenix span bloat.

The litellm->Phoenix emitter has now been de-bloated twice (ENG2-1036 message
history, ENG2-1461 raw_gen_ai_request, ENG2-1476 invocation_parameters.tools).
Each regression came back silently via an image rebuild. This sentinel makes
the third return loud: it checks the stored size of recent `litellm_request`
spans and exits non-zero if the average or p95 jumps past threshold.

Intended cadence: weekly (cron / scheduled agent), or manually after any
sf-litellm image rebuild + deploy.

Usage:
  uv run python scripts/span_size_sentinel.py                # last 7 days
  uv run python scripts/span_size_sentinel.py --days 1       # post-deploy check
  uv run python scripts/span_size_sentinel.py --avg-kb 10 --p95-kb 50

DSN comes from $PHOENIX_SQL_DATABASE_URL (fallback: ../.env). Read-only.
Exit codes: 0 = healthy, 1 = size breach, 2 = no spans in window (inconclusive).
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

log = logging.getLogger("span_size_sentinel")


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7.0, help="lookback window in days")
    ap.add_argument("--avg-kb", type=float, default=10.0, help="avg stored-KB threshold")
    ap.add_argument("--p95-kb", type=float, default=50.0, help="p95 stored-KB threshold")
    ap.add_argument("--span-name", default="litellm_request")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    t0 = time.perf_counter()
    log.info(
        "[sentinel] start name=%s window=%.1fd thresholds avg=%.0fKB p95=%.0fKB",
        args.span_name, args.days, args.avg_kb, args.p95_kb,
    )

    with psycopg.connect(dsn_from_env()) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout = '120s'")
        # start_time-windowed so the (indexed) scan stays small and historical
        # pre-debloat fat spans never pollute the signal.
        cur.execute(
            """
            SELECT count(*),
                   avg(octet_length(attributes::text)),
                   percentile_cont(0.95) WITHIN GROUP
                       (ORDER BY octet_length(attributes::text)),
                   max(octet_length(attributes::text))
            FROM phoenix.spans
            WHERE name = %s
              AND start_time > now() - make_interval(secs => %s)
            """,
            (args.span_name, args.days * 86400),
        )
        n, avg_b, p95_b, max_b = cur.fetchone()
        elapsed = time.perf_counter() - t0

        if not n:
            log.warning("[sentinel] no %s spans in window — inconclusive (%.1fs)",
                        args.span_name, elapsed)
            return 2

        avg_kb, p95_kb, max_kb = avg_b / 1024, p95_b / 1024, max_b / 1024
        log.info(
            "[sentinel] %d spans: avg=%.1fKB p95=%.1fKB max=%.1fKB in %.1fs",
            n, avg_kb, p95_kb, max_kb, elapsed,
        )

        breach = avg_kb > args.avg_kb or p95_kb > args.p95_kb
        if breach:
            # Diagnose: fattest recent spans + which attribute key carries the bytes.
            cur.execute(
                """
                SELECT s.id, octet_length(s.attributes::text) AS total,
                       kv.key, octet_length(kv.value::text) AS key_bytes
                FROM (
                    SELECT id, attributes
                    FROM phoenix.spans
                    WHERE name = %s
                      AND start_time > now() - make_interval(secs => %s)
                    ORDER BY octet_length(attributes::text) DESC
                    LIMIT 3
                ) s, LATERAL (
                    SELECT key, value FROM jsonb_each(s.attributes)
                    ORDER BY octet_length(value::text) DESC LIMIT 1
                ) kv
                """,
                (args.span_name, args.days * 86400),
            )
            for span_id, total, key, key_bytes in cur.fetchall():
                log.error(
                    "[sentinel] BREACH sample: span id=%s total=%.1fKB fattest_key=%r (%.1fKB)",
                    span_id, total / 1024, key, key_bytes / 1024,
                )
            log.error(
                "[sentinel] SIZE BREACH — span bloat is back (3rd-regression guard, "
                "see ENG2-1476/1461/1036). Check the sf-litellm image for a lost patch."
            )
            return 1

        log.info("[sentinel] healthy")
        return 0


if __name__ == "__main__":
    sys.exit(main())
