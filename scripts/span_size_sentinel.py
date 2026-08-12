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


def evaluate(
    n: int | None,
    avg_b: float | None,
    p95_b: float | None,
    max_b: float | None,
    *,
    avg_kb_threshold: float,
    p95_kb_threshold: float,
) -> tuple[int, list[str]]:
    """Pure verdict so it can be unit-tested without a database.

    Returns (exit_code, messages). 0 healthy, 1 size breach, 2 inconclusive.

    NOTE (ENG2-1510): this check is DIRECTIONAL — it only fires when spans get
    bigger. It cannot detect content loss, because losing content makes spans
    smaller. See test_span_size_sentinel.py::test_cannot_detect_content_loss;
    scripts/capture_health.py is the inverse check and both must be green.
    """
    if not n:
        return 2, ["no spans in window - inconclusive"]
    avg_kb, p95_kb, max_kb = (avg_b or 0) / 1024, (p95_b or 0) / 1024, (max_b or 0) / 1024
    msgs = [f"{n} spans: avg={avg_kb:.1f}KB p95={p95_kb:.1f}KB max={max_kb:.1f}KB"]
    if avg_kb > avg_kb_threshold or p95_kb > p95_kb_threshold:
        msgs.append(
            "SIZE BREACH - span bloat is back (3rd-regression guard, see "
            "ENG2-1476/1461/1036). Check the sf-litellm image for a lost patch."
        )
        return 1, msgs
    msgs.append("healthy")
    return 0, msgs


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

        code, msgs = evaluate(
            n, avg_b, p95_b, max_b,
            avg_kb_threshold=args.avg_kb, p95_kb_threshold=args.p95_kb,
        )
        for m in msgs:
            (log.error if "BREACH" in m else log.info)("[sentinel] %s (%.1fs)", m, elapsed)
        if code == 2:
            return 2

        if code == 1:
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
            return 1

        return 0


if __name__ == "__main__":
    sys.exit(main())
