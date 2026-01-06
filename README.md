# Dev-Agent-Lens

A transparent proxy and analysis toolkit for AI coding agents. Capture traces from Claude Code (or any LLM), store them efficiently, and query your agent's behavior.

## What's Here

**1. LiteLLM Proxy** - Routes Claude Code through a local proxy to capture all API calls with OpenTelemetry, sending traces to Phoenix (local) or Arize (cloud).

**2. DAL Toolkit** - A Python package (`dev_agent_lens`) and CLI (`dal`) for syncing, storing, and querying trace data locally as Parquet files.

## Quick Start: Proxy Setup

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

## Quick Start: Syncing Data

Pull historical traces from Phoenix or Arize into local Parquet files:

```bash
# Sync from Phoenix
dal sync-historical --source phoenix --project my-project --start 2024-01-01

# Sync from Arize
dal sync-historical --source arize --start 2024-01-01

# Export to optimized Parquet format
dal export-parquet --source my-project
```

See [docs/sync-historical.md](docs/sync-historical.md) for checkpointing, incremental sync, and configuration.

## Quick Start: Querying Data

```bash
# List available data sources
dal sources

# Query a source
dal query my-project --limit 10

# Search for patterns
dal query my-project --pattern "TODO|FIXME"

# Filter by status
dal query my-project --status ERROR
```

Or use the Python API:

```python
from dev_agent_lens.query import query_source

result = query_source(source="my-project", pattern=r"ENG-\d+")
print(f"Found {result.total_spans} spans in {result.total_sessions} sessions")
```

See [docs/querying.md](docs/querying.md) for the full API, exports, and performance tips.

## Architecture

```
Claude Code ──► LiteLLM Proxy ──► Phoenix/Arize
                                       │
                    dal sync-historical │
                                       ▼
                               ~/.dal/data/
                              (Parquet files)
                                       │
                         dal query / Python API
                                       ▼
                            Analysis & Reports
```

## Documentation

- [Proxy Setup](docs/proxy-setup.md) - LiteLLM, Phoenix, Arize configuration
- [Syncing Data](docs/sync-historical.md) - Historical sync from observability backends
- [Querying Data](docs/querying.md) - CLI and Python query API
- [SDK Examples](examples/README.md) - TypeScript and Python SDK integration
