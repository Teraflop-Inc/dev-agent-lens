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
[ -f .env ] || cp .env.example .env  # one-time: then uncomment + fill PHOENIX_SQL_DATABASE_URL (pooler password is out-of-band)
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

Keep that `con` — the recipes below run in the **same `uv run python` session**: recipes #1
and #2 each rebuild `con` so they stand alone, but #3–#4 reuse the `con` above, and the
bare-```sql``` recipes (#5–#7) run by wrapping the SQL: `con.execute("""<sql>""").df()`.

**There are two surfaces, not one.** `phoenix.spans` is what the *model* saw and said.
`<workspace_*>.sandbox_agent_events` is what the *agent* did — tool calls, statuses,
prompts — across 17 per-workspace schemas. Recipes 1–7 are span recipes; **if your question
is about tools or agent actions, start at [Recipe 8](#8-what-tools-did-the-agent-actually-run),
not here.** The two join on `session_id`. See [querying.md](querying.md#the-two-surfaces).

Two conventions used throughout:

- **`ACCT_UUID`** is the per-person identity on a span. **Guard the cast** — as of
  2026-08-12, 7,421 spans carry `user_api_key_end_user_id` as a raw string rather than a
  JSON object, and an unguarded `::jsonb` aborts the whole query with
  `invalid input syntax for type json`:

  ```sql
  CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%'
       THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END
  ```

  Shape breakdown: 67,016 castable JSON objects · 54,958 absent · 7,421 raw strings.
- **Push work down.** Anything that scans or groups goes inside `postgres_query('pg', $$ … $$)` so Postgres does it.

> **Corpus size (measured 2026-08-12): 129,347 spans, 2026-05-06 → present.** An earlier
> version of this page said 1.2M and warned that plain DuckDB scans time out. That was
> accurate when written on 2026-07-15 and stopped being true on **2026-08-03**, when
> `scripts/debloat_spans.py` (ENG2-1461) deliberately deleted ~1.24M duplicate rows —
> `raw_gen_ai_request` and pre-May `Claude_Code%` L² children — after archiving them to
> `~/dal-archive/phoenix-2026-08-03` + S3 (the script refuses to run without a verified
> archive). Nothing was lost. Measured 2026-08-12 after reclaim: **whole DB 1,761 MB, of
> which `phoenix.spans` is 1,345 MB** — down from ~31 GB pre-debloat. Scans are fast now.
> Don't size capacity or cost off the old number.

> **⚠ Two content gaps every content-based recipe must exclude.** Both leave the span row
> in place with its metadata intact, so counts look healthy while the *text* is gone:
>
> | Gap | Window | Filter |
> |---|---|---|
> | **litellm redaction** (ENG2-1510) | 2026-08-03 → 2026-08-12 | `attributes::text NOT LIKE '%redacted-by-litellm%'` |
> | **`dal_trim` dedupe** (ENG2-1469) | historical fat spans, trimmed 2026-08-06 | `... NOT LIKE '%dal_trim%'` |
>
> Redaction hit **96–98% of `litellm_request` spans** on 08-04 → 08-11. It was fixed on
> 2026-08-12 by commit `59c7e14` — same-day spans were 47% redacted as the fix rolled out,
> and capture after that is healthy. So this is a **bounded historical hole, not an ongoing
> outage**: `start_time < '2026-08-03'` is clean, and so is anything after 08-12. The
> `dal_trim` rows point at `llm.input_messages` or the parquet archive for their content.
>
> `sandbox_agent_events` is unaffected by both — it's a separate capture path. That's the
> fallback for any question about the August window ([Recipe 8](#8-what-tools-did-the-agent-actually-run)).

The `account_uuid → person` map lives in `dev_agent_lens/core/identity.yaml` — **gitignored
(it's PII); copy `identity.example.yaml` and fill it in locally, or get it out-of-band.**
Look up a person's UUIDs there, or resolve in Python:

```python
from dev_agent_lens.core.identity import label_account, resolve_account, load_identity_map
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
    SELECT CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%'
                THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid'
           END AS account_uuid,
           count(*) AS spans, max(start_time)::date AS last_seen
    FROM phoenix.spans
    WHERE start_time > now() - INTERVAL '14 days'
    GROUP BY 1 ORDER BY 2 DESC
  $q$)
""").df()
# NOTE: account_uuid IS NULL does NOT mean "sandbox VM" — that is backwards, and this
# page said so until 2026-08-12. Measured: sandbox spans are 13,217 attributed vs 907
# unattributed (93.6% carry an identity); of the 62,379 unattributed spans, 61,472
# (98.5%) are laptop traffic. Filtering NULL to isolate sandboxes excludes almost
# exactly the opposite of what you intend. Detect sandboxes by provenance (Recipe 7).
roster["who"] = roster["account_uuid"].map(lambda a: label_account(a) if a else "unattributed")
print(roster.to_string(index=False))
```

`label_account` resolves each `account_uuid` → email via the local `identity.yaml` (copy
`identity.example.yaml` and fill it, or get it out-of-band — it's gitignored PII). Without
that file it degrades gracefully (a benign `[identity] no identity file` note on stderr, no
crash); unresolved uuids render as `(unclaimed:<prefix>)`, so the roster still prints.

<details><summary>Raw <code>duckdb</code> CLI fallback</summary>

```bash
duckdb -c "INSTALL postgres; LOAD postgres;
ATTACH '$PHOENIX_SQL_DATABASE_URL' AS pg (TYPE postgres, READ_ONLY);
SELECT * FROM postgres_query('pg', \$\$
  SELECT CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END AS account_uuid,
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
      AND CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END
          = '<account-uuid>'   -- a person's uuid, from identity.yaml
  $q$)
""").df()

for s in summarize(spans):          # ~46 real sessions, not 1 thread
    print(s.start.date(), f"{s.minutes:>4.0f}min", f"{s.spans:>4} spans", f"{s.tokens:>10,} tok")
```

## 3. What did they actually type? (human turns, no agent noise)

> **Content-based recipe — exclude the two content gaps** (see [the note above](#query-cookbook)):
> add `AND attributes::text NOT LIKE '%redacted-by-litellm%'` and
> `AND attributes->'input'->>'value' NOT LIKE '%dal_trim%'`, or window to
> `start_time < '2026-08-03'`. Without them, 96–98% of 08-04 → 08-11 rows are empty text and
> your counts silently undercount.

The prompt text is at the tail of the message array. A large share of what looks like a user
turn is the agent talking to itself — `extract_human_turns()` drops it.

**The drop rate depends on which corpus you're filtering — don't carry one number across:**

| Corpus | Candidate user turns | Genuine human turns | Drop rate |
|---|---|---|---|
| `phoenix.spans` text turns (this recipe, pre-08-01, content-bearing) | 9,261 | 5,473 | **40.9%** |
| Local session JSONLs (`~/.claude/projects/`, 65-day window) | 14,209 | 3,928 | **72%** |

Both measured 2026-08-12. The local JSONLs drop far more because they also carry
machine-injected "user" events — tool results, system reminders, hook output — that never
reach the model as a text turn. **Budget sample sizes off the row that matches your source.**

```python
from dev_agent_lens.core.prompts import extract_human_turns

rows = con.execute("""
  SELECT * FROM postgres_query('pg', $q$
    SELECT right(attributes->'input'->>'value', 600) AS tail
    FROM phoenix.spans
    WHERE start_time > now() - INTERVAL '30 days'
      AND CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END
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

> **Content-based recipe** — it inherits Recipe 3's output, so it inherits Recipe 3's
> redaction / `dal_trim` exclusions too. A learning-vs-building split computed over the
> 2026-08-03 → 08-12 window is measuring the surviving 2–4% of turns, not the month.

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

> **Content-based recipe** — the `context` column below reads `attributes->'input'->>'value'`,
> so redacted and `dal_trim` rows return blank context next to a real `status_message`. Add
> the exclusions from Recipe 3, or the friction in the 08-03 → 08-12 window looks contextless.

Errors and their surrounding context are the cheap version of "find the friction":

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT start_time::date AS day,
         left(attributes->'input'->>'value', 120) AS context,
         status_message
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '30 days'
    AND CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END
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
  SELECT CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END AS account_uuid,
         start_time::date AS day,
         sum(coalesce(llm_token_count_prompt,0)+coalesce(llm_token_count_completion,0)) AS tokens
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '7 days'
    AND CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END IS NOT NULL
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

> **This splits spans by origin — it does not give you sandbox *detail*.** `phoenix.spans`
> only ever holds what the model saw and said. What the sandboxed agent actually *did* —
> every tool call, its arguments, its status — lives in
> `<workspace_*>.sandbox_agent_events`, a different set of schemas entirely. If this recipe
> is where you landed while looking for agent behaviour, you want
> [Recipe 8](#8-what-tools-did-the-agent-actually-run).
>
> Also note **there are three Phoenix projects**, not one, and a query that ignores them
> silently blends a dead project into your results:
>
> | Project | Spans | Range |
> |---|---|---|
> | `sf-workspaces` | 93,078 | 2026-05-15 → present |
> | `dev-agent-lens` | 36,250 | 2026-05-06 → **2026-06-06 (dead)** |
> | `default` | 19 | 2026-05-21 |
>
> Join through `phoenix.traces` → `phoenix.projects` and filter on `p.name` to scope a
> query to live traffic.

---

## 8. What tools did the agent actually run?

> **⚠ Start here, and do not reach for `span_kind = 'TOOL'` in `phoenix.spans`.** This is the
> single most common wrong turn against this corpus, and it has burned an analysis into
> concluding "we capture zero tool spans" — which is false and was used to argue the agent
> autonomy ratio wasn't computable.
>
> The history matters, because the naive check flips depending on *when* you run it. Until
> **2026-08-12** `span_kind` held only `LLM` and `UNKNOWN`; a `span_kind='TOOL'` filter
> returned exactly nothing and read as proof the data was absent. Since the ENG2-1510 capture
> fix landed, `TOOL` spans *do* exist — but there were only **1,080, all dated 2026-08-12**,
> against **129,347 spans total**. So the filter now returns a thin, brand-new sliver and
> still is not the tool record. **The authoritative tool surface has always been
> `sandbox_agent_events`.**

**Step 1 — find the schemas.** One per workspace project, 17 of them, 11 with data:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT table_schema FROM information_schema.tables
  WHERE table_name = 'sandbox_agent_events' AND table_schema LIKE 'workspace%'
  ORDER BY 1
$$);
```

> **Quote the schema name.** `workspace_eng2-1376` contains a hyphen, so a bare
> `workspace_eng2-1376.sandbox_agent_events` is a syntax error (Postgres parses the `-` as
> subtraction). Always `"workspace_eng2-1376".sandbox_agent_events`. This bites when you
> build a UNION across all 17 programmatically.

**Step 2 — count what ran.** `session_update_type` separates initiation from status
transitions, and conflating them roughly 4×s your numbers:

- **`tool_call`** — a tool was invoked. **This is "how many tools ran."** (1,218 total)
- `tool_call_update` — a status transition on an already-counted call. (4,277 total)

`tool_name`, `tool_call_id` and `status` are **first-class columns** — no JSON digging:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT tool_name, count(*) AS n
  FROM workspace_uuh_replay.sandbox_agent_events
  WHERE session_update_type = 'tool_call'
  GROUP BY 1 ORDER BY 2 DESC
$$);
```

Answering the ticket's motivating question — *what tools ran in the UUH replay eval?* —
that returns `Bash 26, Write 2, Monitor 1`: **29 tool calls** (plus 180 `tool_call_update`
rows, which is where a "212 tool events" figure comes from).

Fleet-wide, top tools across all 11 populated schemas: `Bash 735 · Read 237 · Edit 85 ·
mcp__linear-server__get_issue 43 · Write 41`.

The canonical name is also at
`payload->'params'->'update'->'_meta'->'claudeCode'->>'toolName'`, but **you don't need it** —
that path and the `tool_name` column agreed on 1,218 of 1,218 `tool_call` rows. Use the
column; keep the JSON path as a fallback if you hit a row where the column is null.

**Phoenix-side complement.** If you must find tool activity in spans, grep the *content*,
not `span_kind`:

```sql
-- the estate is Anthropic-native: /v1/messages ≫ /v1/chat/completions
attributes->'input'->>'value' LIKE '%tool_use%'      -- the model asked for a tool
attributes->'input'->>'value' LIKE '%tool_result%'   -- a tool came back
```

**Do NOT grep `tool_calls`** — that's OpenAI's vocabulary. It matches ~0.25% of this corpus
and reads as confirmation the data is missing.

---

## 9. What's our autonomy ratio? (inferring the stop signal without `stop_reason`)

The share of turns the agent takes *without* a human typing something is the centerpiece of
the off-call-human-time program. It is computable today, from spans alone.

**Don't reach for `stop_reason` — it's near-absent.** Only 703 of 129,355 spans carry it
(0.54%); `sandbox_agent_events` is similar at ~1%. Any recipe gated on it silently returns
almost nothing.

**You don't need it.** `attributes->'input'->>'value'` holds only the **latest** turn, and
its shape tells you who initiated:

| Shape of the latest message | Meaning |
|---|---|
| `[{'tool_use_id': …, 'type': 'tool_result', …}]` | the agent fed itself a tool result → **continued autonomously** |
| `[{'type': 'text', 'text': …}]` | a text-initiated turn |

The ratio of the two **is** the autonomy signal:

```sql
SELECT * FROM postgres_query('pg', $$
  WITH b AS (
    SELECT to_char(start_time,'YYYY-MM') AS mon, attributes->'input'->>'value' AS v
    FROM phoenix.spans
    WHERE name = 'litellm_request'
      AND attributes::text NOT LIKE '%redacted-by-litellm%'   -- see the content-gap note
  )
  SELECT mon,
    count(*) FILTER (WHERE v NOT LIKE '%dal_trim%' AND v LIKE '%tool_result%') AS agent_continued,
    count(*) FILTER (WHERE v NOT LIKE '%dal_trim%' AND v NOT LIKE '%tool_result%'
                       AND v LIKE '%type%text%')                               AS text_turns
  FROM b GROUP BY 1 ORDER BY 1
$$);
```

**Correct the denominator.** The text bucket is not all human — it includes the agent
talking to itself. Pass it through `extract_human_turns()` (Recipe 3) before treating it as
"human turns"; on the span corpus that drops ~41%, which raises the ratio by ~1.7×.

**Measured baseline (2026-08-12), so you have a reference to diff against:**

| Month | agent-continued | text turns | raw ratio | corrected (÷0.59) |
|---|---|---|---|---|
| 2026-05 | 3,530 | 2,862 | 1.23 : 1 | ~2.1 : 1 |
| 2026-06 | 8,938 | 2,375 | 3.76 : 1 | ~6.4 : 1 |
| 2026-07 | 9,902 | 3,127 | 3.17 : 1 | ~5.4 : 1 |
| 2026-08 | 516 | 314 | 1.64 : 1 | **unusable** — 97% redacted, tiny surviving sample |

**Cross-check against `sandbox_agent_events`**, which gives an exact per-tool count with no
redaction exposure and no inference at all (Recipe 8). If the two disagree sharply for a
window, trust the events table and suspect a content gap in the spans.

---

## When a query times out

Supabase enforces a `statement_timeout`, and `attributes` (JSONB) has no index.

**This is much less of a problem than it used to be.** Post-debloat (see the corpus-size
note at the top), full-corpus scans over all 129k spans — including `attributes::text LIKE`
predicates — completed in **6–18s** when this page was verified on 2026-08-12. Reach for the
mitigations below only when you actually hit a timeout, not pre-emptively.

If a query does die with `canceling statement due to statement timeout`:

1. **Narrow the window** — the old guidance was "30 days completes, 90 doesn't"; that was
   written against a 1.2M-row table and is no longer the binding constraint.
2. **Narrow the account first** — filter to one `account_uuid` before aggregating.
3. **Drop `count(DISTINCT …)` over JSON** — it's the most expensive shape and the first to
   time out.

If you get `FATAL: (EMAXCONNSESSION) max clients reached`, the 45-slot connection pool is
full — close idle DuckDB sessions and retry. Both limits are why the derived-schema idea in
ENG2-1398 exists; for now, work within them.
