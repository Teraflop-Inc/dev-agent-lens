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
bare-```sql``` recipes (#5–#11) run by wrapping the SQL: `con.execute("""<sql>""").df()`.

**There are two surfaces, not one.** `phoenix.spans` is what the *model* saw and said.
`<workspace_*>.sandbox_agent_events` is what the *agent* did — tool calls, statuses,
prompts — across 17 per-workspace schemas. Recipes 1–7 are span recipes; **if your question
is about tools or agent actions, start at [Recipe 8](#8-what-tools-did-the-agent-actually-run),
not here.** See [querying.md](querying.md#the-two-surfaces).

**To join the two surfaces:** `session_id` is **not** at `attributes->'metadata'->>'session_id'` — that path returns **0 rows**. It is nested inside the same field the account id lives in:

```sql
CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%'
     THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'session_id' END
```

That resolves on 67,128 spans; 102 of the 117 `sandbox_agent_events` sessions (87%) match.

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
  **All 7,421 raw-string rows fall on 2026-05-21 → 05-22**, so a query windowed to recent
  data won't trip the error and the guard will look pointless — it isn't, it just only
  fires on windows covering those two days. A `start_time > '2026-05-23'` floor is the
  cheaper alternative when your question allows it.
- **Push work down.** Anything that scans or groups goes inside `postgres_query('pg', $$ … $$)` so Postgres does it.

> **Corpus size (measured 2026-08-14): 147,208 spans, 2026-05-06 → present** (~9k/day growth;
> 129,347 when first measured 2026-08-12). An earlier
> version of this page said 1.2M and warned that plain DuckDB scans time out. That was
> accurate when written on 2026-07-15 and stopped being true on **2026-08-03**, when
> `scripts/debloat_spans.py` (ENG2-1461) deliberately deleted ~1.24M duplicate rows —
> `raw_gen_ai_request` and pre-May `Claude_Code%` L² children — after archiving them to
> `~/dal-archive/phoenix-2026-08-03` + S3 (the script refuses to run without a verified
> archive). Nothing was lost. Measured 2026-08-12 after reclaim: **whole DB 1,761 MB, of
> which `phoenix.spans` is 1,345 MB** — down from ~31 GB pre-debloat. Scans are fast now.
> Don't size capacity or cost off the old number.

> **Retention floor: 2026-05-06. There is nothing before it — on either surface.** The
> earliest span is 2026-05-06 (a partial day, 94 rows); `sandbox_agent_events` doesn't start
> until 2026-05-15; and the parquet archive itself only runs 2026-05-06 → 08-03. So the
> **2026-04-01 → 2026-05-06 window is a hard capture gap with no data anywhere**, not a
> query you haven't written yet. This bounds every longitudinal claim: any "since Q2" or
> "over the last N months" analysis silently starts on 2026-05-06. Say so when you report one.

> **⚠ Two content gaps every content-based recipe must exclude.** Both leave the span row
> in place with its metadata intact, so counts look healthy while the *text* is gone:
>
> | Gap | Window | Filter |
> |---|---|---|
> | **litellm redaction** (ENG2-1510) | 2026-08-03 → 2026-08-12 | `attributes::text NOT LIKE '%redacted-by-litellm%'` |
> | **`dal_trim` dedupe** (ENG2-1469) | historical fat spans, trimmed 2026-08-06 | `attributes->'input'->>'value' NOT LIKE '%dal_trim%'` |
>
> Redaction hit **90–98% of `litellm_request` spans** on 08-04 → 08-11 (most days 96–98%; 08-09 was 90.2% on a low-volume day). It was fixed on
> 2026-08-12 by commit `59c7e14` — same-day spans were 47% redacted as the fix rolled out,
> and capture after that is healthy (re-verified 2026-08-14: 0.3% on 08-13, 0.0% on 08-14).
> So this is a **bounded historical hole, not an ongoing
> outage**: `start_time < '2026-08-03'` is clean, and so is anything after 08-12. The
> `dal_trim` rows point at `llm.input_messages` or the parquet archive for their content.
>
> **Apply the `dal_trim` filter to `attributes->'input'->>'value'`, not `attributes::text`** —
> the marker also appears elsewhere in the blob, so the broad form excludes **47,926** spans
> (37% of the corpus) where the narrow one excludes **14,164** (11%). Recipes here use narrow.
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
      -- '<account-uuid>': a person's uuid, from identity.yaml
      AND CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END
          = '<account-uuid>'
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
| `phoenix.spans`, scoped to `sf-workspaces` (what [Recipe 9](#9-whats-our-autonomy-ratio-inferring-the-stop-signal-without-stop_reason) uses) | 6,511 | 4,538 | **30.3%** |
| Local JSONLs — **all** `type:"user"` events (396 files) | 38,343 | 3,994 | **89.6%** |
| Local JSONLs — only events carrying a text block | 5,616 | 3,994 | **28.9%** |

All measured 2026-08-12. **The definition of "candidate" matters more than the corpus does** —
the two local-JSONL rows share a numerator and differ 3× in rate purely on what counts as a
candidate. Most `type:"user"` events are machine-injected (tool results, system reminders,
hook output) and carry no text block at all, so counting them inflates the denominator ~7×.
**Budget off the row matching your source *and* your candidate definition, and say which.**

> An earlier draft cited **72%** (14,209 → 3,928) for the local JSONLs, taken from ENG2-1509.
> It does not reproduce: sweeping all 396 files under every reasonable candidate definition
> brackets it (28.9% … 89.6%) but never lands on it, and no subset reaches 14,209 candidates.
> The numerator was close (3,928 vs 3,994); the denominator was not. The rows above are a
> re-measurement, not the ticket's figure.

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
> so redacted and `dal_trim` rows return blank context. Add the exclusions from Recipe 3, or
> the friction in the 08-03 → 08-12 window looks contextless.
>
> **`status_message` is empty for every span in this corpus** (verified 2026-08-12: 0 non-empty
> of ~130k, across all of OK/UNSET/ERROR) — so select `status_code`, not `status_message`, or
> you get a column that is silently always `''`. The signal is in `context`: of ~1,524 ERROR
> rows in a 30-day window, ~1,028 are the string `quota` and the rest are real turns.

Errors and their surrounding context are the cheap version of "find the friction":

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT start_time::date AS day,
         left(attributes->'input'->>'value', 120) AS context,
         status_code
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '30 days'
    -- '<account-uuid>': a person's uuid, from identity.yaml
    AND CASE WHEN attributes->'metadata'->>'user_api_key_end_user_id' LIKE '{%' THEN ((attributes->'metadata'->>'user_api_key_end_user_id')::jsonb)->>'account_uuid' END
        = '<account-uuid>'
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

> **⚠ Corrected 2026-09-03. The previous version of this recipe substantially over-counted
> sandboxes.** It tested `attributes::text LIKE '%sandbox_id%'`, which scans the *whole* span
> blob — including the attribute that holds the conversation, which dominates a span's bytes.
> Any span whose conversation merely *mentions* `sandbox_id` matched: a rollout transcript, a
> ticket under discussion, this page. The great majority of matches were false positives.
>
> **The general rule: never pattern-match `attributes::text` for provenance.** This corpus
> stores prose, so a substring test answers "was this discussed?" and not "where did this run?"
> Match on the structured field that carries the fact.

Sandbox provenance lives in the litellm metadata header, not anywhere in the payload:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT CASE WHEN CASE WHEN (attributes->'metadata'->'requester_custom_headers'->>'x-litellm-metadata') LIKE '{%'
                        THEN ((attributes->'metadata'->'requester_custom_headers'->>'x-litellm-metadata')::jsonb)->>'sandbox_id' END
              IS NOT NULL THEN 'sandbox' ELSE 'laptop' END AS kind,
         count(*) AS spans
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '7 days'
  GROUP BY 1
$$);
```

Guard the `::jsonb` cast as shown — the header is a JSON *string*, and an unguarded cast
aborts the query on any row where it is absent or malformed. To get the sandbox ids
themselves, select that same `CASE` expression instead of collapsing it to a label; it
yields the sandbox identifier, which joins to `sandbox_agent_events`.

**Scope, so the number is not over-read:** this detects sandboxes whose traffic routes
through litellm *and* carries the header. A rollout that bypasses the proxy is invisible
here, so treat the sandbox count as a floor. Note also that sandbox spans commonly carry
`"session_id": null` in that same header.


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
> fix landed, `TOOL` spans *do* exist.
>
> **⚠ Updated 2026-09-03 — the "thin sliver" caveat below is no longer true, and following it
> now steers you away from a good surface.** When this was written, TOOL spans existed only
> for a single day. Re-measured since: they are produced continuously, across dozens of
> distinct tool kinds — Bash, Edit, Read, Write, WebFetch, MCP calls, subagent spawns — and
> they carry the full call *and* its result:
>
> ```
> input:  {"type":"tool_use","id":"toolu_…","name":"Bash","input":{"command":"…"}}
> output: "=== CORPUS BOUNDS ===\n… Traceback (most recent call last): …"
> claude_code_tool_name: Bash
> ```
>
> Arguments and results including stderr — replayable, not merely countable.
>
> **So use whichever fits the question.** `phoenix.spans` TOOL spans cover *all* Claude Code
> traffic that routes through the proxy, laptop included. `sandbox_agent_events` remains
> authoritative for **sandbox rollouts specifically**, and is the only surface unaffected by
> the litellm redaction window. For host-side tool behaviour, the span surface is now the
> better starting point; for rollout behaviour, start here.

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
that returns `Bash 26, Write 2, Monitor 1`: **29 tool calls**. If you have seen "212 tool
events" quoted for this eval, that is `count(*) WHERE tool_call_id IS NOT NULL` — 212 of the
schema's 586 rows carry a tool id (the 29 calls, 180 status updates, and 3 `assistant` rows
tagged `tool_name='Agent'`). It counts
*rows about tools*, not tools run. **29 is the number of tools that ran.**

Fleet-wide, top tools across all 11 populated schemas: `Bash 735 · Read 237 · Edit 85 ·
mcp__linear-server__get_issue 43 · Write 41`.

The canonical name is also at
`payload->'params'->'update'->'_meta'->'claudeCode'->>'toolName'`, but **you don't need it** —
that path and the `tool_name` column agreed on 1,218 of 1,218 `tool_call` rows. Use the
column; keep the JSON path as a fallback if you hit a row where the column is null.

> **Substring searches over `agent_message_chunk` events miss split tokens.** Streaming
> chunks split words across events — a final reply of `DONE` arrives as `'D'` + `'ONE'`
> in two rows (observed live, session `ce6da45d`, 2026-08-14). Concatenate a session's
> chunks in `created_at` order before grepping, or search the harvested session JSONL
> (which holds the assembled message) instead.

**Phoenix-side complement.** If you must find tool activity in spans, grep the *content*,
not `span_kind` — and **carry the redaction filter**, or you will reproduce the exact wrong
conclusion this recipe exists to prevent:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT to_char(start_time,'YYYY-MM') AS mon,
         count(*)                                                      AS spans,
         count(*) FILTER (WHERE attributes->'input'->>'value' LIKE '%tool_use%')    AS asked_for_tool,
         count(*) FILTER (WHERE attributes->'input'->>'value' LIKE '%tool_result%') AS tool_returned
  FROM phoenix.spans
  WHERE name = 'litellm_request'
    AND attributes::text NOT LIKE '%redacted-by-litellm%'   -- ← omit this and August reads ~0
  GROUP BY 1 ORDER BY 1
$$);
```

> **Why the filter is not optional here.** Without it, the `tool_use` hit rate reads ~16% in
> May, ~45% in June and July, and **3.5% in August** — a 13× cliff that looks exactly like
> "we stopped capturing tool activity." It isn't; it's the redaction window. That inference
> is the one that produced the "DAL has zero tool spans" claim. With the filter, August is
> a small but *consistent* sample.

**Do NOT grep `tool_calls`** — that's OpenAI's vocabulary (the estate is Anthropic-native:
`/v1/messages` ≫ `/v1/chat/completions`). Measured, it matches **0.30%** of `input.value` —
small enough to read as confirmation the data is missing.

> **This advice ages — re-measure before relying on it.** Post-ENG2-1510, Phoenix emits
> properly-named tool spans (`Claude_Code_Tool_Bash`, `Claude_Code_Tool_Read`, …) under
> `span_kind='TOOL'`. Re-measured 2026-08-14: **4,721 TOOL spans spanning 2026-08-12 → 08-14**
> and growing — so for windows entirely after 2026-08-12, `span_kind='TOOL'` is now a usable
> index. For anything touching earlier data it is still absent by construction (0 spans before
> 08-12 against a 147k corpus), so use `sandbox_agent_events` or content-grepping there. Also
> note tool-span **synthesis is proxy-version/tool-dependent** (observed: Bash synthesized,
> Read not, on some proxies) — `sandbox_agent_events` remains the authoritative count.

---

## 9. What's our autonomy ratio? (inferring the stop signal without `stop_reason`)

The share of turns the agent takes *without* a human typing something is the centerpiece of
the off-call-human-time program. It is computable today, from spans alone.

**Don't reach for `stop_reason` — it's near-absent.** Only 703 of ~129.3k spans carry it
(0.54%); `sandbox_agent_events` is similar at ~1%. Any recipe gated on it silently returns
almost nothing.

**You don't need it.** `attributes->'input'->>'value'` holds only the **latest** turn, and
its shape tells you who initiated:

| Shape of the latest message | Meaning |
|---|---|
| `[{'tool_use_id': …, 'type': 'tool_result', …}]` | the agent fed itself a tool result → **continued autonomously** |
| `[{'type': 'text', 'text': …}]` | a text-initiated turn |

The ratio of the two **is** the autonomy signal:

> **Filter to one project or this number is wrong** — this is Recipe 7's warning, and it
> bites hardest right here. `dev-agent-lens` died on 2026-06-06 but still holds 36,250
> spans, and it was a *far* less autonomous workload. Left unfiltered it supplies **93% of
> May's rows** and drags the May ratio from 6.15:1 down to 1.23:1 — which flips the headline
> from "autonomy is declining" to "autonomy tripled." Join to `phoenix.projects` and pick
> one.

```sql
SELECT * FROM postgres_query('pg', $$
  WITH b AS (
    SELECT to_char(s.start_time,'YYYY-MM') AS mon, s.attributes->'input'->>'value' AS v
    FROM phoenix.spans s
    JOIN phoenix.traces   t ON t.id = s.trace_rowid
    JOIN phoenix.projects p ON p.id = t.project_rowid
    WHERE s.name = 'litellm_request'
      AND p.name = 'sf-workspaces'                             -- live project; NOT the dead dev-agent-lens
      AND s.attributes::text NOT LIKE '%redacted-by-litellm%'  -- see the content-gap note
  )
  SELECT mon,
    count(*) FILTER (WHERE v NOT LIKE '%dal_trim%' AND v LIKE '%tool_result%') AS agent_continued,
    count(*) FILTER (WHERE v NOT LIKE '%dal_trim%' AND v NOT LIKE '%tool_result%'
                       AND v LIKE '%type%text%')                               AS text_turns
  FROM b GROUP BY 1 ORDER BY 1
$$);
```

**Correct the denominator.** The text bucket is not all human — it includes the agent
talking to itself. The query above returns *counts*, so to apply the correction you re-run it
selecting the text bucket's `v` instead of counting it, and pass that through
`extract_human_turns()` exactly as Recipe 3 does.

**Use the divisor for the corpus you filtered to.** This recipe pins `sf-workspaces`, where
the drop rate is **30.3%** → divide by **0.697**. The 40.9% figure in Recipe 3 is the
*all-projects* rate and gives a divisor of 0.591 — applying it here would overstate every
corrected ratio by ~18%. Same trap as the project filter itself: change the corpus, change
the constant.

**Measured baseline (`sf-workspaces`, 2026-08-12), so you have a reference to diff against:**

| Month | agent-continued | text turns | raw ratio | corrected (÷0.697) |
|---|---|---|---|---|
| 2026-05 | 240 | 39 | 6.15 : 1 | ~8.8 : 1 |
| 2026-06 | 8,862 | 2,361 | 3.75 : 1 | ~5.4 : 1 |
| 2026-07 | 9,902 | 3,127 | 3.17 : 1 | ~4.5 : 1 |
| 2026-08 | 540 | 328 | 1.65 : 1 | **unusable at measurement time** (97% redacted through 08-12); post-08-12 August data is clean — re-measure on a `start_time > '2026-08-12'` window |

**Read the trend with care, and don't headline it.** On live-project traffic the ratio
*declines* May → July. But May is only 279 classifiable spans (the project had just started;
the retention floor is 2026-05-06), against ~11k in June and ~13k in July — so the "6.15"
is a thin, early-adopter sample, not a fleet baseline. June → July, where the volume is
comparable, is the only month-over-month comparison here that carries weight: **3.75 → 3.17,
a mild decline.** Anyone quoting a May-to-July trend is quoting sampling noise.

> For contrast, the **unfiltered** numbers — which include the dead `dev-agent-lens`
> project — are May 3,530/2,862 = 1.23:1, June 8,938/2,375 = 3.76:1. The May figure is 93%
> dead-project traffic and inverts the trend. This is what the project filter is protecting
> you from.

**Cross-check against `sandbox_agent_events`**, which gives an exact per-tool count with no
redaction exposure and no inference at all (Recipe 8). If the two disagree sharply for a
window, trust the events table and suspect a content gap in the spans.

---

## 10. Is thinking on, and where's the reasoning text?

> **⚠ Do not detect thinking by grepping `budget_tokens`.** That parameter was **removed from
> the API on Opus 4.7+** (sending it returns a 400) — only pre-4.6 models (in our fleet:
> Haiku 4.5) can still legally send it. Grepping it "proves" only Haiku thinks, and
> `{"type": "adaptive"}` counted as "disabled" completes the inversion. Both produced the
> false "0.9% thinking-enabled" figure in ENG2-1487. Classify on `thinking.type`:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT coalesce(attributes->'llm'->>'model_name','(null)') AS model,
         CASE WHEN attributes->'llm'->>'invocation_parameters' ~ '"thinking":\s*\{"type":\s*"adaptive"' THEN 'ON (adaptive)'
              WHEN attributes->'llm'->>'invocation_parameters' ~ '"thinking":\s*\{"type":\s*"disabled"' THEN 'off (disabled)'
              WHEN attributes->'llm'->>'invocation_parameters' LIKE '%budget_tokens%' THEN 'ON (budget_tokens, pre-4.6)'
              ELSE 'absent (model default)' END AS thinking,
         count(*) AS n
  FROM phoenix.spans
  WHERE start_time > now() - INTERVAL '7 days' AND name = 'litellm_request'
  GROUP BY 1, 2 ORDER BY 3 DESC
$$);
```

Verified 2026-08-14: `claude-opus-5` ON (adaptive) 7,027 · off 477 · absent 282 — thinking is
ON for ~95% of Opus 5 traffic. `absent` means the model's own default applies (Opus 5 /
Sonnet 5 / Fable default to adaptive).

**Where the reasoning text lives — and the two traps that hid it:**

- Anthropic never returns raw chain of thought. `thinking.display` decides what you get:
  `"omitted"` (the old fleet default) returns thinking blocks with **empty text but real
  signatures** — 5,253 such signed-empty blocks accumulated in this store and read as
  "nothing captured." Since **2026-08-14** the proxy injects `display: "summarized"` on
  adaptive requests (ENG2-1487, dev-agent-lens PR #64), so summaries flow: **576 spans with
  reasoning text in the first 90 minutes.**
- The text lands on the callback-synthesized `Claude_Code_*` spans (`span_kind` **UNKNOWN**,
  packed `llm` attributes) — **not** on `litellm_request.output_messages`. Scan the whole
  frame, not LLM-kind rows.
- Attribute JSON arrives with **escaped quotes** (and dict-repr in some client dataframes) —
  quote-exact patterns like `'"signature"'` silently match nothing. Use quote-agnostic
  regexes:

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT count(*) FILTER (WHERE attributes::text ~ 'display.{0,4}:\s*.{0,3}summarized') AS asked_for_summary,
         count(*) FILTER (WHERE attributes::text ~ 'thinking.{0,4}:\s*.{0,3}[A-Za-z][^"\\]{10}') AS has_reasoning_text
  FROM phoenix.spans
  WHERE start_time > '2026-08-14'
$$);
```

Reasoning-token counts are **not separable**: Anthropic folds thinking into `output_tokens`,
and `usage_object.reasoning_tokens` is ~always 0 (65 non-zero of 28,578 field-carrying spans).
"How many tokens went to reasoning?" is not answerable on this path — don't build a report on
it. Cache accounting IS real: `usage_object.cache_read/creation_input_tokens` non-zero on ~90%
of carrying spans.

## 11. Main loop vs. subagents (who actually spent the tokens)

Claude Code stamps two headers on every request it makes, and LiteLLM preserves them under
`attributes->'metadata'->'requester_custom_headers'`:

| header | meaning |
|--------|---------|
| `x-claude-code-session-id` | the conversation thread |
| `x-claude-code-agent-id`   | **present only on subagent (Task tool) calls** |

So the main loop is the *absence* of the agent header, and each subagent is one distinct id.
No extra instrumentation is needed — this is stock Claude Code behaviour.

```sql
SELECT * FROM postgres_query('pg', $$
  SELECT coalesce(attributes->'metadata'->'requester_custom_headers'->>'x-claude-code-agent-id',
                  '(main loop)') AS agent_id,
         count(*) AS calls,
         sum(coalesce(llm_token_count_prompt,0))     AS prompt_tokens,
         sum(coalesce(llm_token_count_completion,0)) AS completion_tokens
  FROM phoenix.spans
  WHERE name = 'litellm_request'
    AND start_time > now() - INTERVAL '2 days'
  GROUP BY 1 ORDER BY 2 DESC
$$);
```

Swap the time filter for
`… ->>'x-claude-code-session-id' = '<session-uuid>'` to break one session into its main loop
plus each subagent it spawned. Subagent calls carry the parent session id too, so the join
back to the parent is free.

**⚠️ Only `litellm_request` carries these headers.** Measured over a 2-day window: 8,001
`litellm_request` spans, 100% with a session id and ~51% with an agent id — and **zero** on
all seven other span names (`raw_gen_ai_request`, `Claude_Code_Internal_Prompt_*`,
`Claude_Code_Tool_*`, `Claude_Code_Final_Output_*`, `claude_code.UserPromptSubmit`). This is
the same span-type artifact that bites `account_uuid` in [Recipe 1](#1-who-is-active-the-team-roster):
group without `WHERE name = 'litellm_request'` and the bulk of your corpus lands under
`(main loop)` by construction, making subagent work look like it never happened.

Two more traps, both of which produced a wrong "DAL isn't capturing this" conclusion before
this recipe was written:

- **The `agent-` prefix is not on the wire.** Local sidecars are
  `~/.claude/projects/<enc-cwd>/<session>/subagents/agent-<id>.jsonl`, but the header value is
  the bare `<id>`. Grepping the filename against `attributes::text` returns 0 rows and reads
  as absence.
- **Don't bound by the session's first day.** A session resumed a week later spawns subagents
  with timestamps far from its start. Filter on the session id, not a `BETWEEN` window around
  it, or you will silently drop the later ones.

**Agent *type* is not in the headers** — only the opaque id. `agentType` (`Explore`, `Plan`,
`general-purpose`, …) exists solely in the local sidecar
`subagents/agent-<id>.meta.json`, so "cost by agent kind" needs a sidecar join at sync time.
Tracked in ENG2-1590.

Finally, the "`session_id` is NOT a working session" caveat in [querying.md](querying.md)
applies to `x-claude-code-session-id` as well: it is a conversation thread that survives
`--continue`/`--resume`, **not** a working session. Don't `GROUP BY` it to count sessions.

---

## When a query times out

Supabase enforces a `statement_timeout`, and `attributes` (JSONB) has no index.

**This is much less of a problem than it used to be.** Post-debloat (see the corpus-size
note at the top), full-corpus scans over all 129k spans — including `attributes::text LIKE`
predicates — completed in **6–35s** when this page was verified on 2026-08-12. Reach for the
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
