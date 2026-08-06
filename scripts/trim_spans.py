"""ENG2-1469: dedupe-trim historical fat litellm_request spans in phoenix.spans.

Per fat span (all eras):
  - llm.input_messages: keep only the NEWEST message; if its content embeds the
    whole conversation as one string, keep only the newest element; cap at 32KB.
  - input.value: marker (duplicate of input_messages — content kept once).
  - invocation_parameters tools (dict or string form): marker (schema blob,
    identical across calls).
Originals: twice-verified parquet archive at ~/dal-archive/phoenix-2026-08-03.

Usage:
  python trim_spans.py <DSN> dry-run [N]
  python trim_spans.py <DSN> execute
"""

import ast
import json
import logging
import sys
import time
from pathlib import Path

import psycopg

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stderr)
log = logging.getLogger("trim")

DSN = sys.argv[1]
MODE = sys.argv[2]
SAMPLE_N = int(sys.argv[3]) if len(sys.argv) > 3 else 20

ARCHIVE_MAX_ID = 1327224      # max id in the 2026-08-03 parquet archive — hard scope ceiling
FAT_THRESHOLD = 20_000        # stored bytes; selection predicate
CAP = 32_000                  # max kept content bytes per span
BATCH = 40
STATE = Path(__file__).parent / "trim_state.json"
TRIM_MARK = "dal_trim"
IV_MARKER = "[dal_trim: deduped - see llm.input_messages or parquet archive]"


def parse_seq(s):
    for loader in (json.loads, ast.literal_eval):
        try:
            v = loader(s)
            if isinstance(v, list):
                return v
        except Exception:
            continue
    return None


def trim_attrs(attrs, stored_size):
    stats = {"dropped_msgs": 0, "tools_trimmed": False, "truncated": False}
    changed = False

    llm = attrs.get("llm")
    if not isinstance(llm, dict):
        return None

    # 1. input_messages -> newest message only, content capped
    ims = llm.get("input_messages")
    if isinstance(ims, list) and ims:
        if len(ims) > 1:
            stats["dropped_msgs"] += len(ims) - 1
            llm["input_messages"] = ims = [ims[-1]]
            changed = True
        im = ims[0]
        msg = im.get("message") if isinstance(im, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str) and len(content) > CAP:
            seq = parse_seq(content)
            if seq and len(seq) > 1:
                stats["dropped_msgs"] += len(seq) - 1
                content = repr([seq[-1]])
            if len(content) > CAP:
                content = "[dal_trim: older content truncated] ..." + content[-CAP:]
                stats["truncated"] = True
            msg["content"] = content
            changed = True

    # 2. input.value duplicates input_messages — keep content once
    inp = attrs.get("input")
    if isinstance(inp, dict) and isinstance(inp.get("value"), str) and len(inp["value"]) > 2000:
        inp["value"] = IV_MARKER
        changed = True

    # 3. tools schema blob (dict-form or whole-string invocation_parameters)
    ip = llm.get("invocation_parameters")
    if isinstance(ip, dict):
        if len(json.dumps(ip.get("tools", ""), default=str)) > 4000:
            tools = ip["tools"]
            n = len(tools) if isinstance(tools, list) else "?"
            ip["tools"] = f"[dal_trim: {n} tool schemas removed]"
            stats["tools_trimmed"] = True
            changed = True
    elif isinstance(ip, str) and len(ip) > 4000:
        llm["invocation_parameters"] = "[dal_trim: invocation parameters removed (mostly tool schemas); see archive]"
        stats["tools_trimmed"] = True
        changed = True

    if not changed:
        return None
    attrs[TRIM_MARK] = {"v": 1, "date": "2026-08-04", "orig_stored_bytes": stored_size, **stats}
    return attrs, stats


def candidates(cur, after_id, limit):
    cur.execute(
        """
        SELECT id, pg_column_size(attributes), length(attributes::text), attributes
        FROM phoenix.spans
        WHERE name = 'litellm_request' AND id > %s AND id <= %s
          AND pg_column_size(attributes) > %s
          AND NOT attributes ? %s
        ORDER BY id
        LIMIT %s
        """,
        (after_id, ARCHIVE_MAX_ID, FAT_THRESHOLD, TRIM_MARK, limit),
    )
    return cur.fetchall()


def main():
    last_id = 0
    if MODE == "execute" and STATE.exists():
        last_id = json.loads(STATE.read_text())["last_id"]
        log.info("resuming after id %s", last_id)

    tot_rows = skipped = tot_raw_before = tot_raw_after = 0
    t_start = time.perf_counter()

    with psycopg.connect(DSN, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 300000")
            while True:
                limit = SAMPLE_N if MODE == "dry-run" else BATCH
                rows = candidates(cur, last_id, limit)
                if not rows:
                    break
                for span_id, stored, raw, attrs in rows:
                    last_id = span_id
                    result = trim_attrs(attrs, stored)
                    if result is None:
                        skipped += 1
                        continue
                    new_attrs, stats = result
                    new_json = json.dumps(new_attrs, default=str)
                    tot_rows += 1
                    tot_raw_before += raw
                    tot_raw_after += len(new_json)
                    if MODE == "execute":
                        cur.execute(
                            "UPDATE phoenix.spans SET attributes = %s::jsonb WHERE id = %s",
                            (new_json, span_id),
                        )
                    elif tot_rows <= 8:
                        log.info("sample id=%s raw %dKB -> %dKB (dropped_msgs=%s tools=%s trunc=%s)",
                                 span_id, raw // 1024, len(new_json) // 1024,
                                 stats["dropped_msgs"], stats["tools_trimmed"], stats["truncated"])
                if MODE == "execute":
                    conn.commit()
                    STATE.write_text(json.dumps({"last_id": last_id}))
                    if tot_rows and tot_rows % 1000 < BATCH:
                        el = time.perf_counter() - t_start
                        log.info("progress: %d trimmed (%d skipped), raw %.2fGB -> %.0fMB, %.0fs",
                                 tot_rows, skipped, tot_raw_before / 1e9, tot_raw_after / 1e6, el)
                if MODE == "dry-run":
                    break

    el = time.perf_counter() - t_start
    print(f"\n=== {MODE} RESULT ===")
    print(f"spans trimmed   : {tot_rows}  (skipped/no-change: {skipped})")
    print(f"raw attr bytes  : {tot_raw_before/1e6:.1f} MB -> {tot_raw_after/1e6:.1f} MB "
          f"({(1 - tot_raw_after/max(tot_raw_before,1)) * 100:.1f}% reduction)")
    print(f"elapsed         : {el:.0f}s")


main()
