# Querying Data

Two ways to query the team's Claude Code trace data:

1. **Live, against Supabase directly** (this section) — no sync, query the shared
   backend in place with DuckDB. This is the fastest way to answer "who did what."
2. **Local Parquet** via `dal sync` + `dal query-spans` (further down) — for repeated
   heavy queries over a source you've pulled down.

Start with (1). It needs nothing but the connection string. For copy-paste recipes that
answer real questions (who's active, one person's sessions, learning-vs-building, blockers),
see the [query cookbook](query-cookbook.md).

---

## 1. Query Supabase directly with DuckDB (no sync)

The backend is a Supabase Postgres. You do **not** need `psql`, and you do **not** need to
`dal sync` first. DuckDB's `postgres` extension attaches to it and queries in place.

### Setup (once)

**Run everything through the project's `uv` environment — `uv run python`, NOT a bare
`python`/`python3` or the standalone `duckdb` CLI.** The connection string lives in
`.env` (unexported), and the pinned `duckdb` (tested: **1.4.3**) comes from `uv.lock` — a
bare `python` on a machine whose default is miniconda `base` dumps `numpy._ARRAY_API`
tracebacks (numpy 2.x vs pandas-1.x optional imports) that look fatal but aren't (the
query still returns correct data), and a brew `duckdb` CLI is a different, un-pinned
version. So:

```bash
cd dev-agent-lens
set -a; source .env; set +a          # exports PHOENIX_SQL_DATABASE_URL (it is NOT exported by default)
uv run python                        # the project venv — pinned duckdb, clean numpy
```

Then attach in Python — DuckDB's `ATTACH` needs a **string-literal** path, so pass
`os.environ[...]` (a bare `ATTACH getenv(...)` is a `Parser Error` — `getenv()` is a
`SELECT` scalar, not a valid ATTACH target):

```python
import duckdb, os
con = duckdb.connect()
con.execute("INSTALL postgres; LOAD postgres;")
con.execute(f"ATTACH '{os.environ['PHOENIX_SQL_DATABASE_URL']}' AS pg (TYPE postgres, READ_ONLY)")
```

<details><summary>Raw <code>duckdb</code> CLI fallback (only if you can't use <code>uv run</code>)</summary>

The CLI can't read env vars into `ATTACH`, so interpolate the DSN in the shell — and know
you're on an **un-pinned** DuckDB, which may differ from the tested 1.4.3:

```bash
duckdb -c "INSTALL postgres; LOAD postgres;
ATTACH '$PHOENIX_SQL_DATABASE_URL' AS pg (TYPE postgres, READ_ONLY);
SELECT 1;"
```
</details>

### The one rule that matters: push work down with `postgres_query()`

A plain `SELECT … FROM pg.phoenix.spans` streams every row to your machine and then
filters — over 1.2M spans it crawls or times out. Wrap aggregates in `postgres_query()`
so Postgres does the work and only the summary crosses the wire:

```sql
-- who is active, last 14 days
SELECT * FROM postgres_query('pg', $$
  SELECT ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' AS account_uuid,
         count(*) AS spans, max(start_time)::date AS last_seen
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '14 days'
  GROUP BY 1 ORDER BY 2 DESC
$$);
```

That returns instantly. The same query written as a plain DuckDB scan over the attached
table will time out. Rule of thumb: **anything that scans or groups goes inside
`postgres_query()`.**

### Turning an account_uuid into a person

Traces carry an opaque `account_uuid`, not a name — the only email in the corpus is the
placeholder `oauth@claude-code.ai`. The `account_uuid → person` map lives in
`dev_agent_lens/core/identity.yaml` and resolves in Python:

```python
from dev_agent_lens.core.identity import label_account, resolve_account

label_account("00000000-0000-0000-0000-000000000001")   # -> 'person-a@example.com'
resolve_account("00000000-0000-0000-0000-000000000002").email   # -> 'person-b@example.com'
```

Unknown accounts resolve to `None` (never a guess). One person can own several accounts —
an interactive login plus a `CLAUDE_CODE_OAUTH_TOKEN` account used inside sandbox VMs — so
the map is many-to-one. To pull one person's spans, resolve their email to the set of
account_uuids first, then filter on those inside `postgres_query()`.

### ⚠️ `session_id` is NOT a working session

`session_id` is a Claude Code *conversation thread*. It survives `--continue`/`--resume`
indefinitely — one real thread in this corpus spans 28 days and 498M tokens. **Do not
`GROUP BY session_id` to get "working sessions"** — you'll get one month-long blob per
person. Segment on idle gaps instead:

```python
import duckdb, os
from dev_agent_lens.core.sessionize import summarize   # gap-based sessions
from dev_agent_lens.core.prompts import extract_human_turns  # drop agent-emitted noise

dsn = os.environ["PHOENIX_SQL_DATABASE_URL"]
con = duckdb.connect()
con.execute("INSTALL postgres; LOAD postgres;")
con.execute(f"ATTACH '{dsn}' AS pg (TYPE postgres, READ_ONLY)")

spans = con.execute("""
  SELECT * FROM postgres_query('pg', $q$
    SELECT start_time,
           coalesce(llm_token_count_prompt,0)+coalesce(llm_token_count_completion,0) AS tokens
    FROM phoenix.spans
    WHERE start_time > now() - INTERVAL '30 days'
      AND ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid'
          = '<account-uuid>'   -- resolve a person's uuid(s) via core.identity
  $q$)
""").df()

for s in summarize(spans):          # ~46 real sessions, not 1 thread
    print(s.start.date(), f"{s.minutes:.0f}min", s.spans, "spans")
```

`extract_human_turns()` matters just as much: ~46% of what parses as a user message is the
agent talking to itself (`Perform a web search for the query: …`, statusline prompts, tool
results). Any "what did they ask Claude?" analysis that skips it is roughly half wrong.

### Known limits (you will hit these)

- **Connection cap.** The DSN is the session-mode pooler (`:5432`), limited to **45
  concurrent clients**. Under load you'll see `FATAL: (EMAXCONNSESSION) max clients
  reached`. The transaction pooler (`:6543`) does *not* help — DuckDB needs the session
  pooler's `REPEATABLE READ`. Close DuckDB when idle.
- **No index on `attributes`.** Aggregates over the JSON identity fields beyond ~30 days
  hit Supabase's `statement_timeout`. Narrow the window, or narrow the account first.

---

## 2. Local Parquet: `dal sync` + `dal query-spans`

For repeated heavy queries, pull a source down to Hive-partitioned Parquet and query it
locally with DuckDB.

```bash
# one-time: register the shared backend as a source
dal config add-source team --type phoenix-postgres --project dev-agent-lens --shared

dal sync --source team              # the expensive pull (checkpointed; resumable)
dal export-parquet --source team    # unified JSONL -> partitioned parquet

dal query-spans --source team --stats
dal query-spans --source team --status-code ERROR --limit 20
dal query-spans --source team --model claude --name Tool
```

`dal query-spans` filters: `--source` (required), `--session-id`, `--status-code`,
`--model`, `--name`, `--limit`, `--stats`, `--format table|json`. For Claude-session events
(role-tagged: user/assistant/tool/subagent), use `dal query-events` over
`dal export-events` output.

### Python API

```python
from dev_agent_lens.query import query_source, find_parquet_files, get_parquet_stats

find_parquet_files()          # -> {'team': {'spans': Path(...), 'sessions': Path(...)}, ...}
result = query_source(source="team", status_code="ERROR", limit=500)
result.total_spans, result.total_sessions
```

Parquet lands under `~/.dal/data/parquet/spans/source=<name>/week=<YYYY-WNN>/part-*.parquet`
(Hive-partitioned; use `find_parquet_files()` rather than hardcoding paths).

---

## 3. Conversation reconstruction & mining (fabric)

The `fabric` layer turns raw spans into readable, per-session conversations — one command
for "give me the conversations matching this signal," so eval mining stops re-hand-rolling
DuckDB + per-session stitching.

```bash
# Reconstruct one session's conversation to markdown (start_time order)
dal reconstruct-session <session_id> --source team -o session.md

# List sessions by content pattern, tool usage, date, size (newest first)
dal list-sessions --source team \
  --pattern transcript --tools 'mcp__claude_ai_Linear' \
  --min-spans 50 --since 2026-05-01 --output json

# Bulk-export matching sessions to one .md per session (written atomically)
dal export-conversations --source team --filter transcript --limit 20 -o ./mining-batch/
```

```python
from dev_agent_lens.fabric import list_sessions, reconstruct_session, export_conversations
```

Business-entity lookups: `dal meeting-sessions <id>`, `dal ticket-sessions ENG2-123`,
`dal session-context <session_id>`.

> ⚠️ **Reconstruction groups by `session_id`**, so a resumed thread renders as one giant
> conversation (see the §1 warning). For "working sessions," segment with
> `dev_agent_lens.core.sessionize` first, then reconstruct per block.

| Function | Description |
|----------|-------------|
| `query_source()` | Query a synced source's parquet by filters |
| `find_parquet_files()` | Discover synced sources without hardcoding paths |
| `list_sessions()` | Sessions matching pattern/tool/date/size filters, newest first |
| `reconstruct_session()` | One session_id → markdown, in start_time order |
| `export_conversations()` | Bulk per-session `.md` export, written atomically |
| `label_account()` / `resolve_account()` | account_uuid → person (`core.identity`) |
| `summarize()` | Gap-segmented working sessions (`core.sessionize`) |
| `extract_human_turns()` | Drop agent-emitted noise from candidate prompts (`core.prompts`) |
