"""ENG2-1470: catalog all historical DAL / Claude session usage into Postgres.

Creates dal_catalog.sessions (droppable) and ingests three sources, merged
by session_id:
  A. phoenix_archive  — litellm_request spans in the 2026-08-03 parquet archive,
                        grouped by the session_id tag (cross-machine, May-Aug)
  B. sandbox_rollout  — sandbox_agent_sessions across all workspace_* schemas
  C. local_jsonl      — ~/.claude/projects/*/<uuid>.jsonl on this machine

Usage: python catalog_ingest.py <DSN> [archive|rollouts|local|all]
"""

import glob
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb
import psycopg
from psycopg.types.json import Jsonb

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] %(message)s",
                    datefmt="%H:%M:%S", stream=sys.stderr)
log = logging.getLogger("ingest")

DSN = sys.argv[1]
WHAT = sys.argv[2] if len(sys.argv) > 2 else "all"
ARCHIVE = os.path.expanduser("~/dal-archive/phoenix-2026-08-03")
PROJECTS = os.path.expanduser("~/.claude/projects")
TICKET_RE = re.compile(r"\b(?:ENG2?|CWORK|GTM)-\d+\b")

DDL = """
CREATE SCHEMA IF NOT EXISTS dal_catalog;
CREATE TABLE IF NOT EXISTS dal_catalog.sessions (
  session_id  text PRIMARY KEY,
  sources     text[] NOT NULL,
  account_uuid text,
  device_id   text,
  project_path text,
  started_at  timestamptz,
  ended_at    timestamptz,
  n_llm_calls int,
  n_user_turns int,
  n_tool_calls int,
  models      text[],
  tools_top   jsonb,
  git_branch  text,
  first_user_prompt text,
  summary     text,
  category    text,
  tickets     text[],
  rollout     jsonb,
  meta        jsonb,
  ingested_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_started_idx ON dal_catalog.sessions (started_at);
CREATE INDEX IF NOT EXISTS sessions_category_idx ON dal_catalog.sessions (category);
"""

UPSERT = """
INSERT INTO dal_catalog.sessions AS s
  (session_id, sources, account_uuid, device_id, project_path, started_at, ended_at,
   n_llm_calls, n_user_turns, n_tool_calls, models, tools_top, git_branch,
   first_user_prompt, tickets, rollout, meta)
VALUES (%(session_id)s, %(sources)s, %(account_uuid)s, %(device_id)s, %(project_path)s,
        %(started_at)s, %(ended_at)s, %(n_llm_calls)s, %(n_user_turns)s, %(n_tool_calls)s,
        %(models)s, %(tools_top)s, %(git_branch)s, %(first_user_prompt)s, %(tickets)s,
        %(rollout)s, %(meta)s)
ON CONFLICT (session_id) DO UPDATE SET
  sources = (SELECT array_agg(DISTINCT x) FROM unnest(s.sources || excluded.sources) x),
  account_uuid = COALESCE(excluded.account_uuid, s.account_uuid),
  device_id = COALESCE(excluded.device_id, s.device_id),
  project_path = COALESCE(excluded.project_path, s.project_path),
  started_at = LEAST(s.started_at, excluded.started_at),
  ended_at = GREATEST(s.ended_at, excluded.ended_at),
  n_llm_calls = GREATEST(coalesce(s.n_llm_calls,0), coalesce(excluded.n_llm_calls,0)),
  n_user_turns = COALESCE(excluded.n_user_turns, s.n_user_turns),
  n_tool_calls = COALESCE(excluded.n_tool_calls, s.n_tool_calls),
  models = COALESCE(excluded.models, s.models),
  tools_top = COALESCE(excluded.tools_top, s.tools_top),
  git_branch = COALESCE(excluded.git_branch, s.git_branch),
  first_user_prompt = COALESCE(excluded.first_user_prompt, s.first_user_prompt),
  tickets = COALESCE(excluded.tickets, s.tickets),
  rollout = COALESCE(excluded.rollout, s.rollout),
  meta = coalesce(s.meta, '{}'::jsonb) || coalesce(excluded.meta, '{}'::jsonb)
"""

BASE = dict(account_uuid=None, device_id=None, project_path=None, started_at=None,
            ended_at=None, n_llm_calls=None, n_user_turns=None, n_tool_calls=None,
            models=None, tools_top=None, git_branch=None, first_user_prompt=None,
            tickets=None, rollout=None, meta=None)


def upsert_many(conn, rows):
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(UPSERT, {**BASE, **r})
    conn.commit()


def ingest_archive(conn):
    t0 = time.perf_counter()
    log.info("[archive] aggregating litellm_request spans by session tag")
    db = duckdb.connect()
    rows = db.execute(f"""
        WITH raw AS (
          SELECT json_extract_string(attributes, '$.metadata.user_api_key_end_user_id') AS eu,
                 json_extract_string(attributes, '$.llm.model_name') AS model,
                 start_time, end_time
          FROM read_parquet('{ARCHIVE}/spans_*.parquet') WHERE name = 'litellm_request'),
        p AS (
          SELECT CASE WHEN json_valid(eu) THEN json_extract_string(eu, '$.session_id') END AS sid,
                 CASE WHEN json_valid(eu) THEN json_extract_string(eu, '$.account_uuid') END AS acct,
                 CASE WHEN json_valid(eu) THEN json_extract_string(eu, '$.device_id') ELSE eu END AS dev,
                 model, start_time, end_time
          FROM raw)
        SELECT sid, any_value(acct), any_value(dev), min(start_time), max(end_time),
               count(*), list_distinct(list(model))
        FROM p WHERE sid IS NOT NULL GROUP BY sid
    """).fetchall()
    log.info("[archive] %d sessions aggregated in %.1fs", len(rows), time.perf_counter() - t0)
    payload = [dict(session_id=sid, sources=["phoenix_archive"], account_uuid=acct,
                    device_id=dev, started_at=st, ended_at=en, n_llm_calls=n,
                    models=[m for m in (models or []) if m])
               for sid, acct, dev, st, en, n, models in rows]
    upsert_many(conn, payload)
    log.info("[archive] upserted %d rows in %.1fs total", len(payload), time.perf_counter() - t0)


def ingest_rollouts(conn):
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("""SELECT table_schema FROM information_schema.tables
                       WHERE table_name='sandbox_agent_sessions' AND table_schema LIKE 'workspace%'""")
        schemas = [r[0] for r in cur.fetchall()]
        wanted = ["id", "agent", "sandbox_id", "mode", "started_at", "ended_at", "stop_reason",
                  "pr_url", "rollout_id", "label_tier", "seed_name", "parent_session_id"]
        payload = []
        for sc in schemas:
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_schema=%s AND table_name='sandbox_agent_sessions'""", (sc,))
            have = {r[0] for r in cur.fetchall()}
            cols = [c for c in wanted if c in have]
            cur.execute(f'SELECT {", ".join(cols)} FROM "{sc}".sandbox_agent_sessions')
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                payload.append(dict(
                    session_id=d["id"], sources=["sandbox_rollout"],
                    started_at=d.get("started_at"), ended_at=d.get("ended_at"),
                    project_path=f"workspace:{sc.removeprefix('workspace_')}",
                    rollout=Jsonb({"schema": sc, **{k: str(v) if v is not None else None
                                                    for k, v in d.items()
                                                    if k not in ("id", "started_at", "ended_at")}})))
    upsert_many(conn, payload)
    log.info("[rollouts] upserted %d rows from %d schemas in %.1fs",
             len(payload), len(schemas), time.perf_counter() - t0)


SKIP_PROMPT_PREFIXES = ("<command-name>", "<local-command-stdout", "Caveat:",
                        "<system-reminder>", "[Request interrupted")


def parse_jsonl(path):
    sid = Path(path).stem
    first_ts = last_ts = cwd = branch = version = None
    n_user = n_tools = 0
    tools = Counter()
    models = set()
    first_prompt = None
    texts = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            cwd = cwd or rec.get("cwd")
            branch = branch or rec.get("gitBranch")
            version = version or rec.get("version")
            msg = rec.get("message") or {}
            if rec.get("type") == "user" and not rec.get("isMeta"):
                content = msg.get("content")
                if isinstance(content, str):
                    txt = content.strip()
                elif isinstance(content, list):
                    txt = " ".join(c.get("text", "") for c in content
                                   if isinstance(c, dict) and c.get("type") == "text").strip()
                else:
                    txt = ""
                if txt and not txt.startswith(SKIP_PROMPT_PREFIXES):
                    n_user += 1
                    texts.append(txt[:2000])
                    if first_prompt is None:
                        first_prompt = txt[:1500]
            elif rec.get("type") == "assistant":
                if msg.get("model"):
                    models.add(msg["model"])
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        n_tools += 1
                        tools[c.get("name", "?")] += 1
    tickets = sorted(set(TICKET_RE.findall(" ".join(texts[:50]))))
    return dict(
        session_id=sid, sources=["local_jsonl"], project_path=cwd,
        started_at=first_ts, ended_at=last_ts, n_user_turns=n_user, n_tool_calls=n_tools,
        models=sorted(models) or None, tools_top=Jsonb(dict(tools.most_common(8))),
        git_branch=branch, first_user_prompt=first_prompt, tickets=tickets or None,
        meta=Jsonb({"machine": "adam-mac", "claude_version": version}))


def ingest_local(conn):
    t0 = time.perf_counter()
    # teraflop work only — personal/non-teraflop projects are deliberately excluded
    files = [p for p in glob.glob(f"{PROJECTS}/-Users-vanities-git-work-teraflop*/*.jsonl")
             if not Path(p).name.startswith("agent-")]
    payload, errors = [], 0
    for p in files:
        try:
            payload.append(parse_jsonl(p))
        except Exception as e:
            errors += 1
            log.warning("[local] failed %s: %s", p, str(e)[:120])
    upsert_many(conn, payload)
    log.info("[local] upserted %d of %d files (%d errors) in %.1fs",
             len(payload), len(files), errors, time.perf_counter() - t0)


def main():
    with psycopg.connect(DSN, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        log.info("schema ready")
        if WHAT in ("archive", "all"):
            ingest_archive(conn)
        if WHAT in ("rollouts", "all"):
            ingest_rollouts(conn)
        if WHAT in ("local", "all"):
            ingest_local(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT unnest(sources) src, count(*) FROM dal_catalog.sessions
                           GROUP BY 1 ORDER BY 2 DESC""")
            print("\n=== catalog by source ===")
            for src, n in cur.fetchall():
                print(f"  {src}: {n}")
            cur.execute("SELECT count(*), min(started_at), max(started_at) FROM dal_catalog.sessions")
            print("total / range:", cur.fetchone())


main()
