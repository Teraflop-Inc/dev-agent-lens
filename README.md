# Dev-Agent-Lens

A transparent proxy and analysis toolkit for AI coding agents. Capture traces from Claude Code (or any LLM), store them efficiently, and query your agent's behavior.

## Installation

```bash
git clone <repo-url>
cd dev-agent-lens
uv sync
```

## Quickstart: Analyze Your Claude Sessions

Export your Claude Code sessions to readable markdown for analysis. No proxy setup required—works with existing `~/.claude` sessions.

**Using Claude Code?** Just run `/analyze-session` to find and analyze sessions automatically.

### 1. Find Your Session

**If you already know which session:** Sessions live in `~/.claude/projects/`. Locate the file and skip to step 2.

```bash
# List recent sessions
ls -lt ~/.claude/projects/-Users-*/*.jsonl | grep -v agent- | head -10

# Search by keyword
grep -l "authentication" ~/.claude/projects/*/*.jsonl | grep -v agent-
```

**If you need to discover it:** Export to Parquet and query with DuckDB to find sessions by patterns, metrics, or outliers.

```bash
dal export-events --output ~/claude-sessions.parquet
```

```python
import duckdb

conn = duckdb.connect()
result = conn.execute("""
    SELECT session_id, COUNT(*) as subagent_calls
    FROM '~/claude-sessions.parquet'
    WHERE event_type = 'subagent'
    GROUP BY session_id
    ORDER BY subagent_calls DESC
    LIMIT 5
""").fetchall()

for session_id, count in result:
    print(f"{session_id}: {count} subagents")
```

Find sessions related to a Linear or Jira ticket:

```python
result = conn.execute("""
    SELECT DISTINCT session_id
    FROM '~/claude-sessions.parquet'
    WHERE text ILIKE '%ENG-123%' OR text ILIKE '%PROJ-456%'
""").fetchall()
```

Write other queries to fit your needs—find sessions by tool usage, error patterns, compaction events, or time range.

### 2. Export to Markdown

```bash
dal claude-session-logs-to-markdown <session-file> -o ./exports/
```

The export preserves the full conversation structure including subagents (as linked files) and compactions (inline with context summaries). See [docs/unified_markdown_format.md](docs/unified_markdown_format.md) for the format specification—it can help guide your agent in analyzing sessions that exceed context windows.

### 3. Analyze

Read the markdown into your Claude Code session, or attach to any AI chat:

```
Analyze this session export. Summarize what was accomplished,
identify any errors, and suggest improvements.
```

*More tooling to accelerate markdown analysis is coming soon.*

See [docs/quickstart_session_export.md](docs/quickstart_session_export.md) for CLI options and advanced usage.

---

## Team Collaboration

Share Claude session data with your team for aggregate analysis. This integrates with the existing [Oxen](https://oxen.ai) data version control already used by the DAL toolkit.

### Export with Your Name

Use a unique source name so team members' data doesn't conflict:

```bash
dal export-events --source claude-local-alex    # Use your name/handle
```

This creates `~/.dal/data/parquet/claude-local-alex_events.parquet`.

### Push to Your Team's Repo

```bash
dal push -s claude-local-alex --parquet-only -m "Weekly sync"
```

The `--parquet-only` flag skips large intermediate files, keeping pushes fast.

### Pull Team Data

```bash
dal pull
```

Now you have everyone's session data locally.

### Query Across the Team

```python
import duckdb

conn = duckdb.connect()

# Compare tool usage across teammates
conn.execute("""
    SELECT
        regexp_extract(source, 'claude-local-(\w+)', 1) as teammate,
        tool_name,
        COUNT(*) as uses
    FROM '~/.dal/data/parquet/claude-local-*_events.parquet'
    WHERE event_type = 'tool_use'
    GROUP BY 1, 2
    ORDER BY teammate, uses DESC
""").fetchdf()

# Find how teammates approached a specific ticket
conn.execute("""
    SELECT source, session_id, MIN(timestamp) as started
    FROM '~/.dal/data/parquet/claude-local-*_events.parquet'
    WHERE text ILIKE '%ENG-456%'
    GROUP BY 1, 2
""").fetchdf()
```

> **Note**: Raw session files in `~/.claude/projects/` stay local. Only processed Parquet exports are shared.

---

## What's Here

**1. DAL Toolkit** - A Python package (`dev_agent_lens`) and CLI (`dal`) for exporting, syncing, and querying Claude Code trace data.

**2. LiteLLM Proxy** - Routes Claude Code through a local proxy to capture all API calls with OpenTelemetry, sending traces to Phoenix (local) or Arize (cloud).

---

## Advanced: Full Tracing Pipeline

For comprehensive observability, set up the LiteLLM proxy to capture all API calls with full token counts, timing, and model metadata.

### Proxy Setup

```bash
# 1. Configure
cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY (and ARIZE keys if using cloud)

# 2. Start (pick one)
docker compose --profile phoenix up -d   # Local Phoenix UI at :6006
docker compose --profile arize up -d     # Cloud Arize

# 3. Use Claude Code through the proxy
./claude-lens
```

See [docs/proxy-setup.md](docs/proxy-setup.md) for OAuth, project switching, and configuration details.

### Syncing Data

Pull traces from Phoenix or Arize into local Parquet files for fast querying:

```bash
# Configure a source
dal config add-source my-phoenix --type phoenix \
    --url http://localhost:6006 --project default

# Sync data (incremental by default, or use --start-date for historical)
dal sync --source my-phoenix

# Export to Parquet
dal export-parquet --source my-phoenix
```

Data is stored in `~/.dal/data/parquet/`. Run `dal sync --help` for all options.

### Querying Data

```bash
# List available data sources
dal sources

# Query a source
dal query my-project --limit 10

# Search for patterns
dal query my-project --pattern "TODO|FIXME"
```

Or use the Python API:

```python
from dev_agent_lens.query import query_source

result = query_source(source="my-project", pattern=r"ENG-\d+")
print(f"Found {result.total_spans} spans in {result.total_sessions} sessions")
```

See [docs/querying.md](docs/querying.md) for the full API.

---

## Architecture

```
Claude Code ──► ~/.claude/projects/     (native sessions)
     │                │
     │                └──► dal claude-session-logs-to-markdown ──► Markdown
     │
     └──► LiteLLM Proxy ──► Phoenix/Arize
                                  │
                              dal sync
                                  ▼
                          ~/.dal/data/
                         (Parquet files)
                                  │
                    dal query / Python API
                                  ▼
                       Analysis & Reports
```

> **Note:** This codebase is under active development. Some features may be broken or incomplete, particularly AI-powered commands like `summarize`, `cluster`, `suggest`, and `quality`. The session export functionality documented above is stable.

## Documentation

- [Session Export Quickstart](docs/quickstart_session_export.md) - Full guide with CLI options and automation
- [Session Storage](docs/claude_code_session_storage.md) - How Claude Code stores session data
- [Markdown Format](docs/unified_markdown_format.md) - Unified markdown export specification
- [Proxy Setup](docs/proxy-setup.md) - LiteLLM, Phoenix, Arize configuration
- [Syncing Data](docs/sync.md) - Sync from observability backends
- [Querying Data](docs/querying.md) - CLI and Python query API
- [SDK Examples](examples/README.md) - TypeScript and Python SDK integration
