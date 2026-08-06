"""ENG2-1470 phase 2b: apply subagent categorization results to dal_catalog.sessions.

Reads categorized/batch_*.jsonl, validates, updates category/summary (+confidence
in meta). Reports coverage and category distribution.
"""

import glob
import json
import logging
import sys
import time
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stderr)
log = logging.getLogger("apply")

DSN = sys.argv[1]
HERE = Path(__file__).parent

t0 = time.perf_counter()
rows, bad = {}, 0
files = sorted(glob.glob(str(HERE / "categorized" / "batch_*.jsonl")))
for f in files:
    for line in open(f):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            assert r["session_id"] and r["category"]
            rows[r["session_id"]] = r  # last write wins on dupes
        except Exception:
            bad += 1
log.info("loaded %d results from %d files (%d bad lines)", len(rows), len(files), bad)

with psycopg.connect(DSN, connect_timeout=15) as conn, conn.cursor() as cur:
    n = 0
    for sid, r in rows.items():
        cur.execute(
            """UPDATE dal_catalog.sessions
               SET category = %s, summary = %s,
                   meta = coalesce(meta, '{}'::jsonb) || %s
               WHERE session_id = %s""",
            (r["category"][:80], (r.get("summary") or "")[:300],
             Jsonb({"category_confidence": r.get("confidence")}), sid))
        n += cur.rowcount
    conn.commit()
    log.info("updated %d rows in %.1fs", n, time.perf_counter() - t0)

    cur.execute("""SELECT coalesce(category,'(uncategorized)'), count(*)
                   FROM dal_catalog.sessions GROUP BY 1 ORDER BY 2 DESC""")
    print("\n=== category distribution ===")
    for cat, cnt in cur.fetchall():
        print(f"  {cnt:5d}  {cat}")
    cur.execute("SELECT count(*) FROM dal_catalog.sessions WHERE category IS NULL")
    print("uncategorized:", cur.fetchone()[0])
