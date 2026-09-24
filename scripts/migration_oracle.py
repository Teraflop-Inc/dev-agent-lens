#!/usr/bin/env python3
# ruff: noqa: E501 -- SQL fixtures retain one complete statement per line.
"""Run the same question against Phoenix Postgres and the span store, and diff the answers.

Decision 0003 §2 and §9: the migration oracle is a *cross-schema* diff. The same question
runs through two different shapes and must return the same rows. This is that check, as
one command, so it can run before cutover, after every import batch, and again after the
typed rebuild. It is the regression suite 0003 §9 says the query cookbook should be.

    uv run python scripts/migration_oracle.py --project dev-agent-lens --source phoenix-dal-dead
    uv run python scripts/migration_oracle.py --project dev-agent-lens --source phoenix-dal-dead \
        --layout raw --json /tmp/oracle.json
    uv run python scripts/migration_oracle.py --all-sources --until 2026-09-11T15:00:00Z   # the daily check

Postgres side: DuckDB ATTACH (READ_ONLY) + postgres_query pushdown, scoped to one Phoenix
project via traces -> projects, exactly as the cookbook does. Store side: the configured
span store, through the named layout's `spans` view, scoped to one DAL source.

Two things the pairs deliberately normalise, because they are the known cross-schema gaps:
  * identity: the typed layout propagates account/session across a trace at ingest;
    Phoenix has it per span. Every identity recipe here is written per-trace on BOTH sides.
  * trace ids: the store carries the hex trace_id; Postgres spans carry trace_rowid. Every
    Postgres recipe joins traces to get the hex id.

Exit 1 on any mismatch. Never prints credentials; the DSN comes from the environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb  # noqa: E402

from dev_agent_lens.storage.layouts import get_layout  # noqa: E402
from dev_agent_lens.storage.spanstore import open_store  # noqa: E402
from dev_agent_lens.storage.spanstore.base import quote_literal  # noqa: E402

log = logging.getLogger("oracle")

# ---------------------------------------------------------------------------------------
# Postgres side. `{scope}` is the project join; every recipe is a full statement pushed down.
# ---------------------------------------------------------------------------------------
_PG_BASE = """
  FROM phoenix.spans s
  JOIN phoenix.traces   t ON t.id = s.trace_rowid
  JOIN phoenix.projects p ON p.id = t.project_rowid
  WHERE p.name = {project}{until}
"""
_PG_EUID = "s.attributes->'metadata'->>'user_api_key_end_user_id'"
_PG_ACCOUNT = (
    f"COALESCE(CASE WHEN {_PG_EUID} LIKE '{{%' THEN (({_PG_EUID})::jsonb)->>'account_uuid' END, "
    f"s.attributes->'user'->>'id')"
)
_PG_SESSION = (
    f"COALESCE(CASE WHEN {_PG_EUID} LIKE '{{%' THEN (({_PG_EUID})::jsonb)->>'session_id' END, "
    f"s.attributes->'claude'->>'session_id', s.attributes->'session'->>'id')"
)
_PG_IDENT = f"""
  WITH ident AS (
    SELECT t.trace_id, max({_PG_ACCOUNT}) AS account_uuid, max({_PG_SESSION}) AS session_id,
           count(*) AS n
    {_PG_BASE}
    GROUP BY 1
  )
"""

PG = {
    "count": f"SELECT count(*)::bigint AS spans, count(DISTINCT t.trace_id)::bigint AS traces {_PG_BASE}",
    "daily": f"SELECT (s.start_time AT TIME ZONE 'UTC')::date::text AS day, count(*)::bigint AS n {_PG_BASE} GROUP BY 1 ORDER BY 1",
    "status": f'SELECT s.status_code, count(*)::bigint AS n {_PG_BASE} GROUP BY 1 ORDER BY s.status_code COLLATE "C" NULLS LAST',
    "names": f'SELECT s.name, count(*)::bigint AS n {_PG_BASE} GROUP BY 1 ORDER BY 2 DESC, s.name COLLATE "C" NULLS LAST LIMIT 25',
    "tokens_daily": (
        f"SELECT (s.start_time AT TIME ZONE 'UTC')::date::text AS day, "
        f"sum(coalesce(s.llm_token_count_prompt,0))::bigint AS p, "
        f"sum(coalesce(s.llm_token_count_completion,0))::bigint AS c {_PG_BASE} GROUP BY 1 ORDER BY 1"
    ),
    "cache_daily": (
        f"SELECT (s.start_time AT TIME ZONE 'UTC')::date::text AS day, "
        f"sum(coalesce((s.attributes->'metadata'->'usage_object'->>'cache_read_input_tokens')::bigint,0))::bigint AS r, "
        f"sum(coalesce((s.attributes->'metadata'->'usage_object'->>'cache_creation_input_tokens')::bigint,0))::bigint AS w "
        f"{_PG_BASE} GROUP BY 1 ORDER BY 1"
    ),
    "by_model": f"SELECT s.attributes->'llm'->>'model_name' AS model, count(*)::bigint AS n {_PG_BASE} GROUP BY 1 ORDER BY 2 DESC, (s.attributes->'llm'->>'model_name') COLLATE \"C\" NULLS LAST LIMIT 20",
    # invocation_parameters is double-encoded and sometimes trimmed to a non-JSON marker
    # ("[dal_trim..."); parse only what starts as an object, on both sides identically.
    "thinking": (
        f"SELECT CASE WHEN s.attributes->'llm'->>'invocation_parameters' LIKE '{{%' "
        f"THEN (s.attributes->'llm'->>'invocation_parameters')::jsonb->'thinking'->>'type' END AS think, "
        f"count(*)::bigint AS n {_PG_BASE} GROUP BY 1 ORDER BY 2 DESC, (CASE WHEN s.attributes->'llm'->>'invocation_parameters' LIKE '{{%' THEN (s.attributes->'llm'->>'invocation_parameters')::jsonb->'thinking'->>'type' END) COLLATE \"C\" NULLS LAST"
    ),
    "roster": f'{_PG_IDENT} SELECT account_uuid, count(*)::bigint AS traces, sum(n)::bigint AS spans FROM ident GROUP BY 1 ORDER BY 2 DESC, account_uuid COLLATE "C" NULLS LAST',
    "sessions": f"{_PG_IDENT} SELECT count(DISTINCT session_id)::bigint AS sessions, count(*) FILTER (WHERE session_id IS NULL)::bigint AS traces_without FROM ident",
    "errors": f'SELECT t.trace_id, s.span_id {_PG_BASE} AND s.status_code = \'ERROR\' ORDER BY t.trace_id COLLATE "C", s.span_id COLLATE "C" LIMIT 50',
    "trace_shape": (
        f'WITH top AS (SELECT t.trace_id {_PG_BASE} GROUP BY 1 ORDER BY count(*) DESC, t.trace_id COLLATE "C" LIMIT 25) '
        f"SELECT t.trace_id, s.span_id, coalesce(s.parent_id,'') AS parent_id, s.name {_PG_BASE} "
        f'AND t.trace_id IN (SELECT trace_id FROM top) ORDER BY t.trace_id COLLATE "C", s.span_id COLLATE "C"'
    ),
    # Mirrors typed.py: LiteLLM writes claude_code_tool_name, the OTLP hook path writes tool.name.
    "tools": (
        f"WITH b AS (SELECT coalesce(s.attributes->>'claude_code_tool_name', s.attributes->'tool'->>'name') AS tool {_PG_BASE}) "
        f'SELECT tool, count(*)::bigint AS n FROM b WHERE tool IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, tool COLLATE "C" LIMIT 30'
    ),
    "sample_content": (
        f"SELECT s.span_id, t.trace_id, s.name, s.status_code, "
        f"to_char(s.start_time AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US') AS start_time, "
        f"coalesce(s.llm_token_count_prompt,0)::bigint AS tp, coalesce(s.llm_token_count_completion,0)::bigint AS tc, "
        f"s.attributes->'llm'->>'model_name' AS model, "
        f"md5(coalesce(s.attributes->'input'->>'value','')) AS in_md5, md5(coalesce(s.attributes->'output'->>'value','')) AS out_md5, "
        f"coalesce(jsonb_array_length(s.events),0)::bigint AS n_events "
        f"{_PG_BASE} AND s.span_id LIKE '%00' ORDER BY s.span_id COLLATE \"C\" LIMIT 500"
    ),
    "autonomy": (
        f"WITH b AS (SELECT to_char(s.start_time AT TIME ZONE 'UTC','YYYY-MM') AS mon, s.attributes->'input'->>'value' AS v "
        f"{_PG_BASE} AND s.name = 'litellm_request') "
        f"SELECT mon, count(*) FILTER (WHERE v NOT LIKE '%redacted-by-litellm%' AND v NOT LIKE '%dal_trim%' AND v LIKE '%tool_result%')::bigint AS agent_continued, "
        f"count(*) FILTER (WHERE v NOT LIKE '%redacted-by-litellm%' AND v NOT LIKE '%dal_trim%' AND v NOT LIKE '%tool_result%' AND v LIKE '%type%text%')::bigint AS text_turns "
        f"FROM b GROUP BY 1 ORDER BY 1"
    ),
}

# ---------------------------------------------------------------------------------------
# Store side. `{scope}` is the source filter. Column expressions differ per layout, the
# answer must not. Identity is per-trace on both layouts: typed already propagated it at
# ingest; raw derives it with the same max()-over-trace the typed build uses.
# ---------------------------------------------------------------------------------------
_RAW_EUID = "json_extract_string(attributes,'$.metadata.user_api_key_end_user_id')"
BIND = {
    "raw": {
        "model": "json_extract_string(attributes,'$.llm.model_name')",
        "tp": "llm_token_count_prompt",
        "tc": "llm_token_count_completion",
        "cr": "TRY_CAST(json_extract_string(attributes,'$.metadata.usage_object.cache_read_input_tokens') AS BIGINT)",
        "cw": "TRY_CAST(json_extract_string(attributes,'$.metadata.usage_object.cache_creation_input_tokens') AS BIGINT)",
        # CASE does not stop DuckDB parsing the other branch; feed NULL to the parser instead.
        "think": "json_extract_string(CASE WHEN json_extract_string(attributes,'$.llm.invocation_parameters') LIKE '{%' THEN json_extract_string(attributes,'$.llm.invocation_parameters') END,'$.thinking.type')",
        "tool": "json_extract_string(attributes,'$.claude_code_tool_name')",
        "input": "json_extract_string(attributes,'$.input.value')",
        "output": "json_extract_string(attributes,'$.output.value')",
        "parent": "parent_id",
        "account": (
            f"COALESCE(max(CASE WHEN {_RAW_EUID} LIKE '{{%' THEN json_extract_string({_RAW_EUID},'$.account_uuid') END) OVER (PARTITION BY trace_id), "
            "max(json_extract_string(attributes,'$.user.id')) OVER (PARTITION BY trace_id))"
        ),
        "session": (
            f"COALESCE(max(CASE WHEN {_RAW_EUID} LIKE '{{%' THEN json_extract_string({_RAW_EUID},'$.session_id') END) OVER (PARTITION BY trace_id), "
            "max(COALESCE(json_extract_string(attributes,'$.claude.session_id'), json_extract_string(attributes,'$.session.id'))) OVER (PARTITION BY trace_id))"
        ),
        "scope_col": "source",
    },
    "typed": {
        "model": "model_name",
        "tp": "tokens_prompt",
        "tc": "tokens_completion",
        "cr": "tokens_cache_read",
        "cw": "tokens_cache_write",
        "think": "thinking_type",
        "tool": "tool_name",
        "input": "input_value",
        "output": "output_value",
        "parent": "parent_span_id",
        "account": "account_uuid",
        "session": "session_id",
        "scope_col": "source",
    },
}

STORE = {
    "count": "SELECT count(*)::BIGINT AS spans, count(DISTINCT trace_id)::BIGINT AS traces FROM sp",
    "daily": "SELECT CAST(day AS VARCHAR) AS day, count(*)::BIGINT AS n FROM sp GROUP BY 1 ORDER BY 1",
    "status": "SELECT status_code, count(*)::BIGINT AS n FROM sp GROUP BY 1 ORDER BY 1 NULLS LAST",
    "names": "SELECT name, count(*)::BIGINT AS n FROM sp GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST LIMIT 25",
    "tokens_daily": "SELECT CAST(day AS VARCHAR) AS day, sum(coalesce({tp},0))::BIGINT AS p, sum(coalesce({tc},0))::BIGINT AS c FROM sp GROUP BY 1 ORDER BY 1",
    "cache_daily": "SELECT CAST(day AS VARCHAR) AS day, sum(coalesce({cr},0))::BIGINT AS r, sum(coalesce({cw},0))::BIGINT AS w FROM sp GROUP BY 1 ORDER BY 1",
    "by_model": "SELECT {model} AS model, count(*)::BIGINT AS n FROM sp GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST LIMIT 20",
    "thinking": "SELECT {think} AS think, count(*)::BIGINT AS n FROM sp GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST",
    "roster": (
        "WITH ident AS (SELECT trace_id, any_value(account_uuid) AS account_uuid, count(*) AS n "
        "FROM (SELECT trace_id, {account} AS account_uuid FROM sp) GROUP BY 1) "
        "SELECT account_uuid, count(*)::BIGINT AS traces, sum(n)::BIGINT AS spans FROM ident GROUP BY 1 ORDER BY 2 DESC, 1 NULLS LAST"
    ),
    "sessions": (
        "WITH ident AS (SELECT trace_id, any_value(session_id) AS session_id "
        "FROM (SELECT trace_id, {session} AS session_id FROM sp) GROUP BY 1) "
        "SELECT count(DISTINCT session_id)::BIGINT AS sessions, count(*) FILTER (WHERE session_id IS NULL)::BIGINT AS traces_without FROM ident"
    ),
    "errors": "SELECT trace_id, span_id FROM sp WHERE status_code = 'ERROR' ORDER BY 1, 2 LIMIT 50",
    "trace_shape": (
        "WITH top AS (SELECT trace_id FROM sp GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 25) "
        "SELECT trace_id, span_id, coalesce({parent},'') AS parent_id, name FROM sp "
        "WHERE trace_id IN (SELECT trace_id FROM top) ORDER BY 1, 2"
    ),
    "tools": "SELECT {tool} AS tool, count(*)::BIGINT AS n FROM sp WHERE {tool} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 30",
    "sample_content": (
        "SELECT span_id, trace_id, name, status_code, "
        "strftime(start_time AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M:%S.%f') AS start_time, "
        "coalesce({tp},0)::BIGINT AS tp, coalesce({tc},0)::BIGINT AS tc, {model} AS model, "
        "md5(coalesce({input},'')) AS in_md5, md5(coalesce({output},'')) AS out_md5, "
        "coalesce(json_array_length(events),0)::BIGINT AS n_events "
        "FROM sp WHERE span_id LIKE '%00' ORDER BY 1 LIMIT 500"
    ),
    "autonomy": (
        "WITH b AS (SELECT strftime(start_time AT TIME ZONE 'UTC', '%Y-%m') AS mon, {input} AS v FROM sp WHERE name = 'litellm_request') "
        "SELECT mon, count(*) FILTER (WHERE v NOT LIKE '%redacted-by-litellm%' AND v NOT LIKE '%dal_trim%' AND v LIKE '%tool_result%')::BIGINT AS agent_continued, "
        "count(*) FILTER (WHERE v NOT LIKE '%redacted-by-litellm%' AND v NOT LIKE '%dal_trim%' AND v NOT LIKE '%tool_result%' AND v LIKE '%type%text%')::BIGINT AS text_turns "
        "FROM b GROUP BY 1 ORDER BY 1"
    ),
}


def digest(rows) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(("\x1f".join("" if v is None else str(v) for v in r) + "\x1e").encode())
    return h.hexdigest()[:16]


def first_diff(a, b, limit=3):
    out = []
    for i, (x, y) in enumerate(zip(a, b)):
        if [str(v) for v in x] != [str(v) for v in y]:
            out.append((i, x, y))
            if len(out) >= limit:
                break
    if not out and len(a) != len(b):
        out.append(
            (
                min(len(a), len(b)),
                a[len(b) : len(b) + 1] if len(a) > len(b) else None,
                b[len(a) : len(a) + 1] if len(b) > len(a) else None,
            )
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--project", help="Phoenix project name (postgres side scope)")
    ap.add_argument("--source", help="DAL source name (store side scope)")
    ap.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="SOURCE=PROJECT",
        help="run several source/project pairs in one process (repeatable)",
    )
    ap.add_argument(
        "--all-sources",
        action="store_true",
        help="every configured phoenix-postgres source that names a project becomes a pair; "
        "the scheduled accuracy check uses this",
    )
    ap.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="only spans with start_time at or after this instant, on both sides. A rolling "
        "deployment holds a window, not history; compare the window",
    )
    ap.add_argument(
        "--until",
        default=None,
        metavar="ISO8601",
        help="only spans with start_time before this instant, on both sides. A live "
        "project keeps growing while the import runs; without a shared cutoff the "
        "producer is always ahead and every count mismatches for a true reason",
    )
    ap.add_argument("--layout", default=None, help="raw|typed (default: configured)")
    ap.add_argument("--store", default=None, help="store URI (default: configured)")
    ap.add_argument("--only", default=None, help="comma list of recipe names")
    ap.add_argument("--json", default=None, help="write results here")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s", stream=sys.stderr
    )

    dsn = os.environ.get("PHOENIX_SQL_DATABASE_URL")
    if not dsn:
        print("PHOENIX_SQL_DATABASE_URL is not set", file=sys.stderr)
        return 2

    from dev_agent_lens.config import get_span_layout

    layout_name = a.layout or get_span_layout()
    bind = BIND[layout_name]

    t0 = time.perf_counter()
    pg = duckdb.connect()
    pg.execute("INSTALL postgres; LOAD postgres;")
    pg.execute(f"ATTACH {quote_literal(dsn)} AS pg (TYPE postgres, READ_ONLY)")
    # The pooler's default statement_timeout cancels the full-corpus questions at two
    # minutes (sf-workspaces, 414k spans: roster, sessions, cache_daily all died there on
    # 2026-09-11 while the store answered in under a second). Raise it for this session so
    # the oracle measures Postgres rather than the pooler's patience.
    try:
        pg.execute("CALL postgres_execute('pg', 'SET statement_timeout = ''30min''')")
    except Exception as e:  # noqa: BLE001 - a role that cannot SET still gets the default
        log.info("[oracle] could not raise statement_timeout: %s", str(e).splitlines()[0][:100])
    log.info("[oracle] postgres attached in %.1fs", time.perf_counter() - t0)

    t0 = time.perf_counter()
    store = open_store(a.store)
    st = duckdb.connect()
    layout = get_layout(layout_name)
    layout.attach(st, store)
    log.info(
        "[oracle] store %s layout=%s attached in %.1fs",
        store.uri,
        layout_name,
        time.perf_counter() - t0,
    )

    pairs = [tuple(p.split("=", 1)) for p in a.pair] or (
        [(a.source, a.project)] if a.source and a.project else []
    )
    if a.all_sources:
        from dev_agent_lens.core.sources import SourceManager

        for src in SourceManager().list_sources():
            if (
                str(getattr(src.source_type, "value", src.source_type)) == "phoenix-postgres"
                and src.project
            ):
                pairs.append((src.name, src.project))
    if not pairs:
        ap.error("give --source and --project, one or more --pair SOURCE=PROJECT, or --all-sources")
    import re as _re

    for flag, val in (("--until", a.until), ("--since", a.since)):
        if val and not _re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z?)?", val):
            ap.error(f"{flag} must look like 2026-09-11T15:00:00Z")
    until_pg = (f" AND s.start_time < '{a.until}'" if a.until else "") + (
        f" AND s.start_time >= '{a.since}'" if a.since else ""
    )
    until_st = (f" AND start_time < TIMESTAMPTZ '{a.until}'" if a.until else "") + (
        f" AND start_time >= TIMESTAMPTZ '{a.since}'" if a.since else ""
    )
    if a.until or a.since:
        log.info(
            "[oracle] both sides cut to %s <= start_time < %s", a.since or "-inf", a.until or "+inf"
        )
    names = [n for n in PG if not a.only or n in a.only.split(",")]
    all_results = []
    fails = 0
    for source, project in pairs:
        scope = (
            f"WHERE {bind['scope_col']} = {quote_literal(source)}"
            if bind["scope_col"]
            else "WHERE TRUE"
        ) + until_st
        st.execute(f"CREATE OR REPLACE VIEW sp AS SELECT * FROM spans {scope}")
        results = []
        print(f"\n== {source} (store)  vs  {project} (phoenix)  layout={layout_name}")
        print(
            f"{'recipe':<14} {'result':<9} {'rows':>7} {'pg ms':>9} {'store ms':>9}  first difference"
        )
        for name in names:
            pg_sql = (
                PG[name].replace("{project}", quote_literal(project)).replace("{until}", until_pg)
            )
            t = time.perf_counter()
            try:
                pg_rows = pg.execute(
                    f"SELECT * FROM postgres_query('pg', $oracle${pg_sql}$oracle$)"
                ).fetchall()
            except Exception as e:
                pg_rows = None
                pg_err = f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
            pg_ms = (time.perf_counter() - t) * 1000
            st_sql = STORE[name].format(**bind)
            t = time.perf_counter()
            try:
                st_rows = st.execute(st_sql).fetchall()
            except Exception as e:  # a recipe that cannot be expressed is a finding, not a crash
                st_rows, err = None, f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
            else:
                err = None
            st_ms = (time.perf_counter() - t) * 1000
            if pg_rows is None:
                ok, note, pg_rows = False, f"postgres error: {pg_err}", []
            elif st_rows is None:
                ok, note = False, f"store error: {err}"
            else:
                ok = digest(pg_rows) == digest(st_rows)
                note = (
                    ""
                    if ok
                    else "; ".join(
                        f"row {i}: pg={x} store={y}" for i, x, y in first_diff(pg_rows, st_rows)
                    )
                )
            fails += 0 if ok else 1
            results.append(
                {
                    "recipe": name,
                    "match": ok,
                    "rows_pg": len(pg_rows),
                    "rows_store": None if st_rows is None else len(st_rows),
                    "pg_ms": round(pg_ms),
                    "store_ms": round(st_ms),
                    "note": note,
                }
            )
            print(
                f"{name:<14} {'MATCH' if ok else 'MISMATCH':<9} {len(pg_rows):>7} {pg_ms:>9.0f} {st_ms:>9.0f}  {note[:150]}"
            )

        total_pg = sum(r["pg_ms"] for r in results)
        total_st = sum(r["store_ms"] for r in results)
        agree = sum(1 for r in results if r["match"])
        print(
            f"{agree}/{len(names)} recipes agree. postgres {total_pg / 1000:.1f}s, store {total_st / 1000:.1f}s "
            f"({total_pg / max(total_st, 1):.1f}x). project={project} source={source} layout={layout_name}"
        )
        all_results.append(
            {
                "project": project,
                "source": source,
                "results": results,
                "agree": agree,
                "total": len(names),
            }
        )
    if len(pairs) > 1:
        print(
            f"\nOVERALL: {'PASS' if not fails else 'FAIL'} across {len(pairs)} pairs, {fails} mismatching recipe(s)"
        )
    if a.json:
        with open(a.json, "w") as f:
            json.dump(
                {
                    "layout": layout_name,
                    "store": store.uri,
                    "pairs": all_results,
                    "fails": fails,
                    "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                f,
                indent=2,
                default=str,
            )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
