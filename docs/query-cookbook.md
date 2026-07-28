# Query Cookbook

Copy-paste recipes for the questions we actually ask of the trace data. Each one is
verified against the live Supabase backend. Start from the closest recipe and adapt.

New to this? Read [querying.md](querying.md) first — it covers the one-time setup, the
`postgres_query()` pushdown rule, and the known limits (connection cap, JSON timeout).

**Preflight (do this once per shell):** run everything through the project's `uv`
environment — `uv run python`, never a bare `python`/`python3` or the standalone `duckdb`
CLI (see querying.md for *why* — miniconda numpy tracebacks + un-pinned CLI):

```bash
cd dev-agent-lens
set -a; source .env; set +a          # exports PHOENIX_SQL_DATABASE_URL (NOT exported by default)
uv run python                        # pinned duckdb (tested 1.4.3), clean numpy
```

Everything below assumes you've attached in that `uv run python` — `ATTACH` needs a
string-literal path, so pass `os.environ[...]` (a bare `ATTACH getenv(...)` is a
`Parser Error`):

```python
import duckdb, os
con = duckdb.connect()
con.execute("INSTALL postgres; LOAD postgres;")
con.execute(f"ATTACH '{os.environ['PHOENIX_SQL_DATABASE_URL']}' AS pg (TYPE postgres, READ_ONLY)")
```

Two conventions used throughout:

- **`ACCT_UUID`** is the JSON path `((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid'` — the per-person identity on a span. It's verbose, so recipes alias it early.
- **Push work down.** Anything that scans or groups goes inside `postgres_query('pg', $$ … $$)` so Postgres does it. A plain DuckDB scan over 1.2M spans times out.

The `account_uuid → person` map lives in `dev_agent_lens/core/identity.yaml` — **gitignored
(it's PII); copy `identity.example.yaml` and fill it in locally, or get it out-of-band.**
Look up a person's UUIDs there, or resolve in Python:

```python
from dev_agent_lens.core.identity import label_account, resolve_account
label_account("<account-uuid>")                 # -> the person's email
[a for p in load_identity_map().people if p.email == "someone@example.com" for a in p.accounts]
```

The recipes below use `<account-uuid>` as a placeholder — substitute a real one from your
local map.

---

## 1. Who is active? (the team roster)

Self-contained — paste the whole block into `uv run python` (after the preflight above):

```python
import duckdb, os
from dev_agent_lens.core.identity import label_account

con = duckdb.connect()
con.execute("INSTALL postgres; LOAD postgres;")
con.execute(f"ATTACH '{os.environ['PHOENIX_SQL_DATABASE_URL']}' AS pg (TYPE postgres, READ_ONLY)")

roster = con.execute("""
  SELECT * FROM postgres_query('pg', $q$
    SELECT ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' AS account_uuid,
           count(*) AS spans, max(start_time)::date AS last_seen
    FROM phoenix.spans
    WHERE start_time > now() - INTERVAL '14 days'
    GROUP BY 1 ORDER BY 2 DESC
  $q$)
""").df()
# account_uuid = NULL is sandbox VMs (no identity on the span), not a person.
roster["who"] = roster["account_uuid"].map(lambda a: label_account(a) if a else "sandbox VM")
print(roster.to_string(index=False))
```

`label_account` resolves each `account_uuid` → email via the local `identity.yaml` (copy
`identity.example.yaml` and fill it, or get it out-of-band — it's gitignored PII). Rows it
can't resolve print the raw uuid.

<details><summary>Raw <code>duckdb</code> CLI fallback</summary>

```bash
duckdb -c "INSTALL postgres; LOAD postgres;
ATTACH '$PHOENIX_SQL_DATABASE_URL' AS pg (TYPE postgres, READ_ONLY);
SELECT * FROM postgres_query('pg', \$\$
  SELECT ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' AS account_uuid,
         count(*) AS spans, max(start_time)::date AS last_seen
  FROM phoenix.spans WHERE start_time > now() - INTERVAL '14 days'
  GROUP BY 1 ORDER BY 2 DESC
\$\$);"
```
(un-pinned DuckDB; resolve names by hand via `identity.yaml`.)
</details>

## 2. One person's working sessions

`session_id` is a resumed conversation *thread*, not a session — do **not** group by it (see
querying.md). Pull the person's spans, then segment on idle gaps in Python:

```python
import duckdb, os
from dev_agent_lens.core.sessionize import summarize

con = duckdb.connect(); con.execute("INSTALL postgres; LOAD postgres;")
con.execute(f"ATTACH '{os.environ['PHOENIX_SQL_DATABASE_URL']}' AS pg (TYPE postgres, READ_ONLY)")

spans = con.execute("""
  SELECT * FROM postgres_query('pg', $q$
    SELECT start_time,
           coalesce(llm_token_count_prompt,0)+coalesce(llm_token_count_completion,0) AS tokens
    FROM phoenix.spans
    WHERE start_time > now() - INTERVAL '30 days'
      AND ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid'
          = '<account-uuid>'   -- a person's uuid, from identity.yaml
  $q$)
""").df()

for s in summarize(spans):          # ~46 real sessions, not 1 thread
    print(s.start.date(), f"{s.minutes:>4.0f}min", f"{s.spans:>4} spans", f"{s.tokens:>10,} tok")
```

## 3. What did they actually type? (human turns, no agent noise)

The prompt text is at the tail of the message array. ~46% of what looks like a user turn is
the agent talking to itself — `extract_human_turns()` drops it.

```python
from dev_agent_lens.core.prompts import extract_human_turns

rows = con.execute("""
  SELECT * FROM postgres_query('pg', $q$
    SELECT right(attributes->'input'->>'value', 600) AS tail
    FROM phoenix.spans
    WHERE start_time > now() - INTERVAL '30 days'
      AND ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid'
          = '<account-uuid>'
      AND attributes->'input'->>'value' IS NOT NULL
  $q$)
""").df()

import re
candidates = [m.group(1) for t in rows["tail"].dropna()
              if (m := re.search(r"'text': '([^']{5,400})", t))]
for turn in extract_human_turns(candidates):
    print("•", turn)
```

## 4. Learning vs. building (user story 1)

Recipes 2 + 3 give you the person's real sessions and real prompts. The learning-vs-building
split is a *judgment* over those prompts — there's no column for it. Two ways:

- **Eyeball it** on the `extract_human_turns()` output — patterns are obvious (questions =
  learning, commands = building).
- **Classify at scale** — feed the turns to an LLM with a rubric (learning = seeking to
  understand; building = directing work). The ENG2-1393 audit did this for one engineer:
  32% learning / 68% building, and the *weekly trend* (learning share peaking in week 2 as
  he hit unfamiliar substrate, then falling) was the actionable finding. See the ticket for
  the full method.

The signal to trend over time:

```python
# group human-turn counts by ISO week to see the ramp
rows["week"] = ...   # bucket by the span's start_time week, then classify + ratio per week
```

## 5. Where did they get stuck? (blockers — user story 2)

Errors and their surrounding context are the cheap version of "find the friction":

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT start_time::date AS day,
         left(attributes->'input'->>'value', 120) AS context,
         status_message
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '30 days'
    AND ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid'
        = '<account-uuid>'   -- a person's uuid, from identity.yaml
    AND status_code = 'ERROR'
  ORDER BY start_time DESC LIMIT 50
$$);
```

Richer blockers (repo-access walls, credential thrash, re-asking settled questions) live in
the *human turns*, not the status codes — grep the recipe-3 output for frustration markers
(`again`, `still`, `not found`, `forgot`, `why did`). The ENG2-1393 audit surfaced an engineer's
`remote: Repository not found` hard-stop and a multi-day OAuth-token thrash this way.

## 6. Token / cost per person over time

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' AS account_uuid,
         start_time::date AS day,
         sum(coalesce(llm_token_count_prompt,0)+coalesce(llm_token_count_completion,0)) AS tokens
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '7 days'
    AND ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' IS NOT NULL
  GROUP BY 1, 2 ORDER BY 2 DESC, 3 DESC
$$);
```

## 7. Laptop vs. sandbox (until the project label is fixed)

The `sf-workspaces` project label currently covers *both* laptop work and sandbox rollouts
(ENG2-1375 — a static proxy config). Until that's fixed, tell them apart by sandbox
provenance on the span rather than the project name:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT CASE WHEN attributes::text LIKE '%sandbox_id%' THEN 'sandbox' ELSE 'laptop' END AS kind,
         count(*) AS spans
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '7 days'
  GROUP BY 1
$$);
```

---

## When a query times out

Supabase enforces a `statement_timeout`, and `attributes` (JSONB) has no index. If a query
dies with `canceling statement due to statement timeout`:

1. **Narrow the window** — 30 days usually completes; 90 usually doesn't.
2. **Narrow the account first** — filter to one `account_uuid` before aggregating.
3. **Drop `count(DISTINCT …)` over JSON** — it's the most expensive shape and the first to
   time out.

If you get `FATAL: (EMAXCONNSESSION) max clients reached`, the 45-slot connection pool is
full — close idle DuckDB sessions and retry. Both limits are why the derived-schema idea in
ENG2-1398 exists; for now, work within them.
