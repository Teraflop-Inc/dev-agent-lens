"""ENG2-1461 step 2: de-bloat phoenix.spans — Option A (approved 2026-08-03).

Deletes the two duplicate span classes (all preserved in the verified
parquet archive + its S3 copy):
  - name = 'raw_gen_ai_request'   (~49k rows / 19 GB — OTEL raw-request dupes)
  - name LIKE 'Claude_Code%'      (~1.19M rows / 4.4 GB — pre-May L² children)
plus traces left with zero spans afterwards (~340 span-less stubs).

Safety design:
  - DRY-RUN by default: counts + a sample, zero writes. `--execute` to act.
  - Refuses to run unless the archive dir contains a verify_report.json
    with every check ok (the proof the data exists elsewhere).
  - Batched deletes (id-keyset, small chunks, per-chunk commit) — no long
    locks; phoenix ingest keeps flowing throughout.
  - Only rows matching the two name predicates are EVER touched; the kept
    classes are never in any statement.
  - Disk reclaim (VACUUM FULL / pg_repack) is NOT here — run separately.

Usage:
  uv run --with 'psycopg[binary]' python scripts/debloat_spans.py \
      --archive-dir ~/dal-archive/phoenix-2026-08-03            # dry-run
  ... --execute                                                  # the real thing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import psycopg

log = logging.getLogger("debloat")

DELETE_PRED = "(name = 'raw_gen_ai_request' OR name LIKE 'Claude_Code%')"


def dsn_from_env() -> str:
    dsn = os.environ.get("PHOENIX_SQL_DATABASE_URL")
    if dsn:
        return dsn
    env_path = Path(__file__).resolve().parent.parent / ".env"
    m = re.search(
        r'^\s*PHOENIX_SQL_DATABASE_URL=("?)(.+?)\1\s*$', env_path.read_text(), re.M
    )
    if not m:
        sys.exit("[debloat] PHOENIX_SQL_DATABASE_URL not in env or ../.env")
    return m.group(2)


def require_verified_archive(archive_dir: Path) -> None:
    report_path = archive_dir / "verify_report.json"
    if not report_path.exists():
        sys.exit(f"[debloat] REFUSING: no verify_report.json in {archive_dir}")
    report = json.loads(report_path.read_text())
    if not report.get("ok") or not all(c["ok"] for c in report.get("checks", [])):
        sys.exit("[debloat] REFUSING: archive verify_report is not fully green")
    log.info("[debloat] archive verified green: %s (%d checks)",
             report_path, len(report["checks"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive-dir", required=True)
    ap.add_argument("--execute", action="store_true",
                    help="Actually delete. Default is dry-run (read-only).")
    ap.add_argument("--batch-rows", type=int, default=2000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    require_verified_archive(Path(args.archive_dir).expanduser())

    conn = psycopg.connect(dsn_from_env(), connect_timeout=15, autocommit=True)
    conn.execute("SET statement_timeout = '120s'")

    n_spans, sz = conn.execute(
        f"SELECT count(*), pg_size_pretty(sum(pg_column_size(attributes))::bigint) "
        f"FROM phoenix.spans WHERE {DELETE_PRED}"
    ).fetchone()
    n_traces = conn.execute(f"""
        SELECT count(*) FROM phoenix.traces t
        WHERE NOT EXISTS (SELECT 1 FROM phoenix.spans s
                          WHERE s.trace_rowid = t.id AND NOT {DELETE_PRED})
          AND EXISTS (SELECT 1 FROM phoenix.spans s WHERE s.trace_rowid = t.id)
    """).fetchone()[0]
    log.info("[debloat] delete set: %d spans (%s payload), %d empty-after traces",
             n_spans, sz, n_traces)

    if not args.execute:
        log.info("[debloat] DRY-RUN — nothing deleted. Re-run with --execute.")
        return

    log.info("[debloat] EXECUTE: batched delete, %d rows/batch", args.batch_rows)
    deleted = 0
    t0 = time.perf_counter()
    while True:
        t = time.perf_counter()
        # No bind params on purpose: psycopg only placeholder-parses SQL when
        # params are passed, and DELETE_PRED's LIKE '%' would then be read as
        # a broken placeholder. int() makes the inlined LIMIT injection-safe.
        n = conn.execute(f"""
            DELETE FROM phoenix.spans WHERE id IN (
              SELECT id FROM phoenix.spans WHERE {DELETE_PRED}
              LIMIT {int(args.batch_rows)}
            )
        """).rowcount
        if n == 0:
            break
        deleted += n
        if deleted % (args.batch_rows * 25) < args.batch_rows:
            rate = deleted / max(time.perf_counter() - t0, 1e-6)
            log.info("[debloat] %d/%d spans deleted (%.0f rows/s, ~%.0f min left, batch %.1fs)",
                     deleted, n_spans, rate, (n_spans - deleted) / max(rate, 1) / 60,
                     time.perf_counter() - t)
    log.info("[debloat] spans done: %d deleted in %.1f min",
             deleted, (time.perf_counter() - t0) / 60)

    n = conn.execute("""
        DELETE FROM phoenix.traces t
        WHERE NOT EXISTS (SELECT 1 FROM phoenix.spans s WHERE s.trace_rowid = t.id)
    """).rowcount
    log.info("[debloat] empty traces deleted: %d", n)

    remain, rsz = conn.execute(
        "SELECT count(*), pg_size_pretty(pg_total_relation_size('phoenix.spans')) "
        "FROM phoenix.spans"
    ).fetchone()
    log.info("[debloat] remaining: %d spans; table on-disk still %s "
             "(space is reusable — run VACUUM FULL / pg_repack to hand it back)",
             remain, rsz)


if __name__ == "__main__":
    main()
