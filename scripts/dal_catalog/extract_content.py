"""ENG2-1470 phase 2a: extract a content sample per cataloged session, for
LLM categorization. Writes one JSON per session to ./session_samples/.

Sample = {session_id, who, project_path, started_at, n_llm_calls, first_prompt,
          last_output, rollout_task}
Sources in priority order: local JSONL (already has first_user_prompt in the
catalog), sandbox_agent_events (rollout task prompt), parquet archive
(earliest litellm span's first message + latest span's output).
"""

import ast
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import duckdb
import psycopg

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stderr)
log = logging.getLogger("extract")

DSN = sys.argv[1]
ARCHIVE = os.path.expanduser("~/dal-archive/phoenix-2026-08-03")
OUT = Path(__file__).parent / "session_samples"
OUT.mkdir(exist_ok=True)


def parse_convo(s):
    """input.value / message content — python-repr or json list of messages."""
    for loader in (json.loads, ast.literal_eval):
        try:
            v = loader(s)
            if isinstance(v, dict) and "messages" in v:
                v = v["messages"]
            if isinstance(v, list):
                return v
        except Exception:
            continue
    return None


def first_text(convo):
    for m in convo or []:
        if not isinstance(m, dict) or m.get("role") not in (None, "user"):
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()[:1500]
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text", "").strip():
                    return part["text"].strip()[:1500]
    return None


def main():
    t0 = time.perf_counter()
    with psycopg.connect(DSN, connect_timeout=15) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.session_id, coalesce(a.name, nullif(s.account_uuid,''), 'unknown'),
                   s.project_path, s.started_at::text, s.n_llm_calls, s.sources,
                   s.first_user_prompt, s.rollout
            FROM dal_catalog.sessions s
            LEFT JOIN dal_catalog.accounts a USING (account_uuid)
        """)
        sessions = {r[0]: dict(session_id=r[0], who=r[1], project_path=r[2],
                               started_at=r[3], n_llm_calls=r[4], sources=r[5],
                               first_prompt=r[6], rollout_task=None, last_output=None)
                    for r in cur.fetchall()}
        log.info("[extract] %d catalog sessions loaded", len(sessions))

        # rollout task prompts from sandbox_agent_events (first user/prompt event)
        cur.execute("""SELECT table_schema FROM information_schema.tables
                       WHERE table_name='sandbox_agent_events' AND table_schema LIKE 'workspace%'""")
        for (sc,) in cur.fetchall():
            try:
                cur.execute(f'''
                    SELECT session_id, payload::text FROM "{sc}".sandbox_agent_events
                    WHERE kind IN ('prompt','user_message','session_prompt')
                       OR payload::text ILIKE '%"prompt"%'
                    ORDER BY session_id, seq LIMIT 500''')
                for sid, payload in cur.fetchall():
                    s = sessions.get(sid)
                    if s is not None and not s["rollout_task"]:
                        m = re.search(r'"(?:prompt|text|content)"\s*:\s*"(.{20,1200}?)(?<!\\)"', payload)
                        if m:
                            s["rollout_task"] = m.group(1)[:1200]
            except Exception as e:
                conn.rollback()
                log.warning("[extract] %s events: %s", sc, str(e)[:100])

    # archive: earliest litellm span input + latest output per session
    need = [sid for sid, s in sessions.items()
            if not s["first_prompt"] and not s["rollout_task"] and "phoenix_archive" in (s["sources"] or [])]
    log.info("[extract] pulling archive content for %d sessions", len(need))
    db = duckdb.connect()
    rows = db.execute(f"""
        WITH lit AS (
          SELECT CASE WHEN json_valid(json_extract_string(attributes, '$.metadata.user_api_key_end_user_id'))
                      THEN json_extract_string(json_extract_string(attributes, '$.metadata.user_api_key_end_user_id'), '$.session_id') END AS sid,
                 start_time,
                 json_extract_string(attributes, '$.input.value') AS input_value,
                 json_extract_string(attributes, '$.output.value') AS output_value
          FROM read_parquet('{ARCHIVE}/spans_*.parquet') WHERE name = 'litellm_request')
        SELECT sid,
               min_by(input_value, start_time) AS first_input,
               max_by(output_value, start_time) AS last_output
        FROM lit WHERE sid IS NOT NULL GROUP BY sid
    """).fetchall()
    log.info("[extract] archive aggregation done in %.0fs", time.perf_counter() - t0)
    hit = 0
    for sid, first_input, last_output in rows:
        s = sessions.get(sid)
        if s is None:
            continue
        if not s["first_prompt"] and isinstance(first_input, str):
            s["first_prompt"] = first_text(parse_convo(first_input)) or first_input[:800]
            hit += 1
        if isinstance(last_output, str) and last_output.strip():
            s["last_output"] = last_output.strip()[:800]

    n = 0
    for sid, s in sessions.items():
        (OUT / f"{sid}.json").write_text(json.dumps(s, default=str))
        n += 1
    with_content = sum(1 for s in sessions.values()
                       if s["first_prompt"] or s["rollout_task"] or s["last_output"])
    print(f"wrote {n} samples ({with_content} with content, archive fills: {hit}) "
          f"in {time.perf_counter() - t0:.0f}s -> {OUT}")


main()
