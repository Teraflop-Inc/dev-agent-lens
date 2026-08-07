# Questions Library — Example Queries

Built on `dal_catalog.sessions` (1,333+ sessions, 2026-05-06 → 08-04) with person attribution, category assignments, and call/tool counts. Use these as starting points—adapt them to your needs.

## Quick Start: Daily Report

Every query in this library is available via one command:

```bash
dal report                  # Yesterday's activity
dal report --since today    # Just today
dal report --since 7        # Past 7 days
```

Or use Python directly for custom analysis:

```python
from dev_agent_lens.analysis.questions import (
    get_team_activity,
    get_active_roster,
    get_repeated_patterns,
    get_stalled_sessions,
)

# What did my team do this week?
df = get_team_activity(days=7)
print(df)

# Who's active and what changed?
df = get_active_roster()
print(df)

# What patterns repeat often enough to be a skill?
df = get_repeated_patterns(min_sessions=3)
print(df)

# Which sessions ran long?
df = get_stalled_sessions(hours=3)
print(df)
```

---

## What Did My Team Do?

### By Person (This Week)

```python
from dev_agent_lens.analysis.questions import get_team_activity

df = get_team_activity(days=7)
df  # person, sessions, llm_calls, tool_calls, categories
```

**Output:**
```
           person  sessions  llm_calls  tool_calls                                                                         categories
0            alex         6        175           0  business-ops, customer-work, dal-observability, infra-ops, noise, planning-triage
1            None         6          0         571              general-qa, meeting-qa, noise, planning-triage, ticket-implementation
2            adam         5         82           0                                     ticket-implementation, workspace-rollout-infra
3  yashwanthsai.v         3         40           0                                                                eval-tooling, noise
```

### By Category & Person

```python
from dev_agent_lens.analysis.questions import get_weekly_summary

df = get_weekly_summary()
df  # person, category, sessions, llm_calls, tool_calls
```

---

## Where Is Time Going?

### By Category (60-Day Patterns)

What patterns repeat often enough to become a skill? This shows the distribution of work and how much time each type consumes on average.

```python
from dev_agent_lens.analysis.questions import get_repeated_patterns

df = get_repeated_patterns(min_sessions=5)
df  # category, sessions, pct_of_total, avg_llm_calls, avg_tool_calls, avg_duration_minutes
```

**Output:**
```
                   category  sessions  pct_of_total  avg_llm_calls  avg_tool_calls  avg_duration_minutes
0                   ir-eval       381          36.8              7               0                     4
1                     noise       200          19.3              6               0                    16
2   workspace-rollout-infra        79           7.6             44              32                   520
3             customer-work        68           6.6            116               2                  3441
4     ticket-implementation        61           5.9            109              10                   917
5            research-spike        31           3.0             62               0                   920
```

**Coverage Note:** The Phoenix project has been dark since 2026-06-06 (ENG2-1375), and thinking-token capture is unexplained (ENG2-1487). This report reflects only actively captured sessions. Gaps don't imply zero activity.

### By Project

```python
from dev_agent_lens.analysis.questions import get_project_distribution

df = get_project_distribution()
df  # project, sessions, llm_calls, tool_calls
```

---

## Who Is Active and What Changed?

### Week-Over-Week Comparison

```python
from dev_agent_lens.analysis.questions import get_active_roster

df = get_active_roster()
df  # person, this_week_sessions, last_week_sessions, change, this_week_calls, this_week_tools
```

**Output:**
```
           person  this_week_sessions  last_week_sessions change  this_week_calls  this_week_tools
0            alex                   6                  36  ↓ -83              175                0
1            None                   6                   0     →                 0              571
2            adam                   5                  28  ↓ -82               82                0
3  yashwanthsai.v                   3                   1  ↑ 200               40                0
```

---

## Which Sessions Stalled?

Sessions longer than N hours. Useful for finding long-running debugging sessions, writing sprints, or blocked work.

```python
from dev_agent_lens.analysis.questions import get_stalled_sessions

# Sessions >3 hours in the past 14 days
df = get_stalled_sessions(hours=3)
df  # person, session_id, started_at, ended_at, duration_hours, category, summary
```

**Output (first 3 rows):**
```
            person                            session_id                       started_at                         ended_at  duration_hours                 category                                                                              summary
0      (unclaimed)  9a4c1caa-fb94-4fd7-8d74-f2faae4cf3cf 2026-08-03 15:33:54.999000+00:00 2026-08-04 15:04:34.174000+00:00            23.5          planning-triage                Daily planning: check latest standup + Linear tickets, create tickets
1      (unclaimed)  96493c15-3069-4ffd-95ad-81d9694b2f2f 2026-07-30 15:23:54.766000+00:00 2026-08-03 15:04:45.763000+00:00            95.7          planning-triage  Daily planning: review Adam's Linear todos + today's standup, create tasks for t...
2   yashwanthsai.v  4ab2215b-35d2-41c3-a579-1d3d14012297 2026-07-24 15:06:19.807533+00:00 2026-07-31 21:12:28.940519+00:00           174.1              dev-tooling  End-to-end codebase/system explanation with ASCII diagrams and narrative for Yas...
```

---

## Custom Queries

All functions are importable. Here's how to build on them:

```python
from dev_agent_lens.analysis.questions import get_session_history

# One person's recent sessions
df = get_session_history(account_uuid="your-uuid-here", limit=10)

# Last 5 sessions across the team
df = get_session_history(limit=5)
```

For complex custom queries against the raw database:

```python
import os
import psycopg
import pandas as pd

url = os.getenv("PHOENIX_SQL_DATABASE_URL")
con = psycopg.connect(url, autocommit=True)

# Your SQL here; tables: dal_catalog.sessions, dal_catalog.accounts
sql = """
SELECT count(*) FROM dal_catalog.sessions
WHERE category = 'ticket-implementation'
"""

df = pd.read_sql(sql, con)
con.close()
```

---

## Coverage Limits

Know what you're measuring:

1. **Phoenix archive (phoenix_archive):** LiteLLM request spans from 2026-08-03 parquet, grouped by session_id. Cross-machine, 2026-05-06 → 08-03. ~14.5k early calls have no session tag and are excluded.

2. **Sandbox rollout (sandbox_agent_sessions):** Spans across all `workspace_*` schemas in Supabase. Captures agent runs during rollout testing.

3. **Local Claude sessions (local_jsonl):** `~/.claude/projects/` on Adam's machine (teraflop paths only). Claude Code prunes local JSONLs after ~30 days, so the archive is the ground truth for historical data.

4. **Phoenix dark since 2026-06-06:** The `dev-agent-lens` Phoenix project stopped capturing traces (ENG2-1375). Sessions before that date are archived; new sessions won't appear until Phoenix is re-enabled.

5. **Thinking tokens unexplained (ENG2-1487):** Thinking token counts are not currently captured. Reports show zero.

---

## See Also

- `dal report` — Quick daily digest from the CLI
- `dal sync` — Sync new sessions from backends
- `docs/query-cookbook.md` — Lower-level DuckDB/Postgres recipes
