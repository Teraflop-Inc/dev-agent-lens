# DAL session-usage catalog (ENG2-1470)

One-shot (re-runnable) pipeline that catalogs every historical Dev Agent Lens /
Claude session into `dal_catalog.sessions` in the shared Supabase — the ground
truth for "how is DAL actually used" ahead of the DAL 2.0 architecture decision
(ENG2-1471 Notion doc, OLTP vs OLAP).

## Tables

- `dal_catalog.sessions` — one row per session_id: sources, person, project,
  time range, call/turn/tool counts, first prompt, LLM-assigned `category` +
  `summary`, ticket mentions, rollout metadata. Droppable if it doesn't earn
  its keep.
- `dal_catalog.accounts` — account_uuid → email/name (Adam, Alex, Will, Yashwanth).

## Sources (merged by session_id)

1. **phoenix_archive** — `litellm_request` spans in the 2026-08-03 parquet
   archive (`~/dal-archive/phoenix-2026-08-03`), grouped by the
   `user_api_key_end_user_id.session_id` tag. Cross-machine, 2026-05-06 → 08-03.
   ~14.5k early calls have no session tag and are deliberately excluded.
2. **sandbox_rollout** — `sandbox_agent_sessions` across all `workspace_*` schemas.
3. **local_jsonl** — `~/.claude/projects/-Users-vanities-git-work-teraflop*/` on
   Adam's machine (teraflop paths only; personal projects deliberately excluded).
   Note: Claude Code prunes local JSONLs (~30 days), hence the archive source.

## Run order

```bash
DSN=$PHOENIX_SQL_DATABASE_URL   # dev-agent-lens/.env
uv run --with 'psycopg[binary]' --with duckdb --with pytz python catalog_ingest.py "$DSN" all
uv run --with 'psycopg[binary]' --with duckdb --with pytz python extract_content.py "$DSN"
# categorize: 14 parallel Claude subagents over session_samples/ (see ENG2-1470
# session 2026-08-04) writing categorized/batch_*.jsonl, then:
uv run --with 'psycopg[binary]' python apply_categories.py "$DSN"
```

Categories are a seeded taxonomy (ir-eval, ticket-implementation, meeting-qa,
customer-work, workspace-rollout-infra, infra-ops, …) plus emergent labels
normalized after the fact (hivemind, planning-triage, business-ops, …).

First full run (2026-08-04): 1,333 sessions, 0 uncategorized. Distribution and
findings: see ENG2-1470.
