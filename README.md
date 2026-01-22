# Dev-Agent-Lens

A transparent proxy and analysis toolkit for AI coding agents. Capture traces from Claude Code (or any LLM), store them efficiently, and query your agent's behavior.

## Installation

```bash
git clone <repo-url>
cd dev-agent-lens
uv sync
```

## Quickstart: Export Sessions to Markdown

Export your Claude Code sessions to readable markdown for analysis. No proxy setup required - works with existing `~/.claude` sessions.

**Using Claude Code?** Just run `/analyze-session` - see [.claude/commands/analyze-session.md](.claude/commands/analyze-session.md)

### 1. Find Your Session

Sessions live in `~/.claude/projects/`. You can search manually or ask Claude Code to find them:

```bash
# List all projects
ls ~/.claude/projects/

# List recent sessions (exclude agent files)
ls -lt ~/.claude/projects/-Users-*/*.jsonl | grep -v agent- | head -10

# Search for sessions mentioning a keyword
grep -l "my search term" ~/.claude/projects/*/*.jsonl | grep -v agent-
```

Or just tell Claude Code: *"Find my session where I was working on authentication"*

### 2. Export to Markdown

```bash
dal claude-session-logs-to-markdown ~/.claude/projects/-Users-me-project/abc123.jsonl -o ./exports/
```

This creates:
- `{session-id}.md` - Main conversation
- `subagent_{type}_{n}.md` - Subagent conversations (if any)

### 3. Analyze

Give the markdown to any AI agent, or have Claude Code read it directly:

```
Analyze this session export. Summarize what was accomplished,
identify any errors, and suggest improvements.
```

See [docs/quickstart_session_export.md](docs/quickstart_session_export.md) for CLI options and LiteLLM chain exports.

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

# Sync historical data
dal sync-historical --source my-phoenix

# Export to Parquet
dal export-parquet --source my-phoenix
```

Data is stored in `~/.dal/data/parquet/`. See [docs/sync-historical.md](docs/sync-historical.md) for details.

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
                   dal sync-historical
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
- [Syncing Data](docs/sync-historical.md) - Historical sync from observability backends
- [Querying Data](docs/querying.md) - CLI and Python query API
- [SDK Examples](examples/README.md) - TypeScript and Python SDK integration
