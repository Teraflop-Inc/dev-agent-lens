# Dev Agent Lens: database learnings & the OLTP vs OLAP decision

*2026-08-04 · Adam Mischke · for the DAL 2.0 architecture decision (ENG2-1471)*
*Companion data: `dal_catalog.sessions` usage catalog (ENG2-1470) · Related: roll-our-own telemetry spec (ENG2-1462)*

**TL;DR — the "row-oriented Postgres ceiling" we diagnosed in July was real as an
experience but wrong as a diagnosis.** 97% of the database was one capture bug writing
the same conversation content hundreds of times. With the data shaped correctly (dedup +
one trigram index), the same Postgres searches the entire history in ~7 seconds on a
434 MB table. At today's scale there is no forcing case for a dedicated OLAP store; the
hybrid we already run (hot Postgres + parquet/DuckDB cold archive) is the right
architecture, and DAL 2.0 should keep a Postgres OLTP core with measurable triggers for
when that stops being true.

---

## 1. Current state — what we store

| Store | What | Scale (2026-08-04) |
|---|---|---|
| `phoenix.spans` (shared Supabase) | LLM traces: litellm → Phoenix callback, one `litellm_request` span per model call, JSONB `attributes` | 81k spans, **584 MB** incl. 150 MB trgm index (was 31 GB on 07-28) |
| `workspace_*.sandbox_agent_events` / `_sessions` | ACP rollout capture: what the sandboxed agent *did* (tool calls, status, prompts), joinable to spans by `session_id` | 13.6k events, 115 sessions, 16 schemas |
| Local session JSONLs (`~/.claude/projects/`) | Claude Code's own transcripts — richest per-session record, but pruned ~30 days and per-machine | 132 sessions (Jul 7 →) |
| Parquet archive (`~/dal-archive/phoenix-2026-08-03` + S3) | Full pre-cleanup span history, twice-verified (exact id-set equality) | 27 GB, 1.31M spans, May 6 → Aug 3 |
| `dal_catalog.sessions` (new, ENG2-1470) | Precomputed per-session lookup: person, category, summary, ticket mentions | 1,333 sessions, 19 categories |

Whole shared DB: **952 MB** (was ~31 GB a week ago). Identity rides on the spans as a
`{device_id, account_uuid, session_id}` tag; `dal_catalog.accounts` maps account → person.

Growth after the capture fixes (ENG2-1461 message redaction, ENG2-1476 tools-schema
dedup + size sentinel): **single-digit MB/week.** Before them: 2–3 GB/week.

## 2. Query patterns we actually run

From the query cookbook + the usage catalog (1,333 real sessions, ENG2-1470):

1. **Aggregations over spans** — who's active, spans/tokens per person per week,
   model mix. Pure OLAP-shaped, but small: seconds on today's table.
2. **Content substring search** (`attributes::text ILIKE`) — "find the session where we
   discussed X." The workhorse of `workflow-postmortem` and analyst mining, and the
   pattern that broke (§3). Now index-assisted: **full-history in ~7 s**.
3. **Session reconstruction** — ordered span fetch for one `session_id` (`dal
   reconstruct-session`, replay). Needle-shaped OLTP read; always fine.
4. **Cross-store joins** — spans ↔ `sandbox_agent_events` by session_id ("what the
   model said" ↔ "what the agent did"); spans ↔ identity; now catalog ↔ everything.
5. **Point lookups by conversation id** — exactly what `dal_catalog` precomputes
   (Alex's "lookup, not search" ask — O(1) now).

What the catalog says about *who generates the load*: eval traffic dominates (ir-eval +
eval-tooling = 34% of sessions), quota/health noise is 24%, and the human-work core
(tickets, customer work, infra, meetings) is ~23%. Two design inputs for DAL 2.0:
**(a)** the store must cheaply separate machine noise from human signal; **(b)** eval
runs are the biggest producer and consumer — batch-shaped, replayable, archival-friendly.

## 3. Problems encountered (and what actually fixed them)

The full arc, because the *shape* of this failure is the main learning:

- **May 6 (ENG2-1036):** first L² blowup — litellm's Arize integration emitted the full
  conversation history in every turn's span. Patched (latest-turn-only). ~$5k of Arize
  storage burned before the fix.
- **June:** a rebuild of the litellm image silently dropped the patch → the bleed
  returned via a *different* attribute family (`raw_gen_ai_request` children, up to
  1.3 MB/span). Nobody noticed for ~7 weeks because nothing watched span sizes.
- **July 28 (ENG2-1452):** symptom surfaces — historical content search times out. The
  searchable horizon was *shrinking* as volume grew (~38 s of detoast per GB against a
  120 s pooler ceiling). We (reasonably, wrongly) framed this as the row-store ceiling.
- **Aug 3 (ENG2-1461):** row-level fix — capture patched again, full table archived to
  parquet (verified), 1.24M duplicate span rows deleted: 31 GB → 5.5 GB.
- **Aug 4 (ENG2-1469):** content-level fix — the surviving spans still each carried the
  whole conversation prefix + a 160–520 KB tools schema + a duplicate payload. Deduped
  47,755 spans in place (13.6 GB raw → 357 MB), VACUUM FULL → **434 MB**, added a
  `pg_trgm` GIN on `(attributes::text)`. Repro: 79 s → 6.6 s; 6-week window: timeout →
  9.4 s; full history: 7.7 s. Existing queries unchanged.
- **Aug 4 (ENG2-1476):** the deployed patch was still writing ~500 KB of MCP tool
  schemas per call; fixed, and a **span-size regression sentinel** now exists so a
  rebuild can't silently un-fix capture a *third* time.

**Learnings:**
1. **It was never a scale problem.** 97% of the DB was one bug's output. Measure where
   bytes actually live before concluding "wrong database."
2. **TOAST detoast is the enemy of JSONB content search** — cost scales with stored
   bytes scanned, so duplicated payloads poison queries quadratically.
3. **Capture regressions recur; only sentinels stop them.** Same bug shipped twice
   through image rebuilds.
4. **Archive-before-destroy works.** Verified parquet made both destructive cleanups
   low-drama and reversible.
5. Index the *right* thing: a trigram index was pointless over duplicated 30 GB, cheap
   and decisive over deduped 400 MB.

## 4. OLTP vs OLAP

**Transactional (OLTP-shaped) in DAL today:** live span/event ingest (small rows, high
frequency), session upserts, identity/enrichment tables (accounts, UID→name), the
catalog, per-session reconstruction reads, grant/annotation-style product features.
This is bread-and-butter Postgres and would be awkward anywhere else.

**Analytical (OLAP-shaped):** content search over history, fleet-wide aggregations,
clustering/mining, eval-corpus builds. Two honest observations from this week:

- *Shaped correctly, hot Postgres handles our analytical load too.* 7-second
  full-history scans, index-assisted search, sub-second aggregations — at 81k spans /
  434 MB there is no query we run that Postgres can't serve inside the pooler timeout.
  Phoenix's own choice of Postgres for this workload looks validated *at this scale*.
- *The heavy analytics already moved to columnar without us deciding it.* The 27 GB
  archive is parquet; the usage catalog was built by DuckDB scanning it (65k-span
  aggregations in ~4 s, content extraction for 1,266 sessions in ~9 s) — laptop-local,
  zero load on prod. That's the OLAP store, and it costs nothing to run.

**Would ClickHouse/OLAP-first help?** Not at this scale. Our corpus is ~1.3k sessions
per quarter and post-fix ingest is MB/week. A dedicated OLAP deployment adds an ops
surface (the PCD teardown this morning is a reminder of what always-on analytical
infra costs) to solve a problem we no longer measurably have. The DuckDB-over-parquet
lane covers the heavy/cold end today.

**When would this flip?** Concrete triggers worth writing down: hot table > ~10 GB of
*legitimate* (post-sentinel) data; content-search p95 > ~30 s; multi-tenant/customer
DAL where per-customer analytical isolation matters; or streaming analytics (live
dashboards over ingest) becoming a product feature. Hitting any of these reopens
ENG2-1462's build-vs-buy question with real evidence.

## 5. Recommendation & open questions

**Recommendation for DAL 2.0:**

1. **Postgres OLTP core** — sessions, events, identity, enrichments, catalog. Boring,
   transactional, joinable.
2. **Keep the two-tier analytics we already have**: hot Postgres (shaped + indexed +
   sentinel-guarded) for interactive search/aggregation; parquet + DuckDB for cold
   history and heavy mining, with a scheduled archive sync (today it's manual).
3. **Guardrails over migrations**: the size sentinel (ENG2-1476) is the load-bearing
   piece — the architecture only stays cheap while capture stays sane.
4. **Skip monthly partitioning** (ENG2-1469 item 1) — a 434 MB table doesn't justify
   the machinery; revisit at the same triggers as above. *(Alex's call.)*
5. **ENG2-1462 (roll-our-own telemetry) reads as no-build for now** — this week's
   tactical fixes bought the runway the spec was worried we didn't have. The spec's
   value is the trigger list + the Phoenix-as-optional-sink boundary, not near-term
   construction.

**Open questions:**
- Rollout identity: 550 non-noise sessions have no account tag (sandbox VMs mint no
  identity). Worth threading `account_uuid` through the rollout env so evals attribute?
- Archive cadence: parquet sync is manual today; post-08-03 spans exist only in hot PG.
  Monthly cron?
- Catalog lifecycle: refresh on demand vs schedule — currently one-shot re-runnable
  (scripts: dev-agent-lens `scripts/dal_catalog/`).
- The mystery 5th account (`6cfa3617…`, 9 sessions) — identify or ignore.
