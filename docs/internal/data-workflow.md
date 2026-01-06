# DAL Data Pulling and Processing Workflow

This document describes how to pull, access, and analyze Claude Code trace data using the DAL (Dev Agent Lens) CLI.

## Overview

DAL syncs trace data from two backend types:
- **Phoenix** - Local/self-hosted OpenTelemetry collector
- **Arize** - Cloud-hosted observability platform

Data flows through this pipeline:
```
Backend (Phoenix/Arize) → sync → raw JSONL → normalize → sessions JSONL → analyze
```

## Directory Structure

All data is stored in `~/.dal/`:

```
~/.dal/
├── config/
│   └── sources.json          # Named source configurations
├── data/
│   ├── raw/{source}/         # Raw span data per source
│   │   └── sync_*.jsonl      # Timestamped raw sync files
│   ├── sessions/{source}/    # Normalized session data per source
│   │   ├── sessions_*.jsonl  # Date-stamped session files
│   │   └── sessions_current.jsonl → (symlink to latest)
│   └── parquet/              # Parquet exports (optional)
└── state/
    └── historical-sync-{source}.json  # Checkpoint state for resume
```

## Step 1: Configure Sources

Before syncing, configure your data sources:

```bash
# List configured sources
dal config list-sources

# Add a Phoenix source
dal config add-source my-phoenix \
    --type phoenix \
    --url http://localhost:6006 \
    --project dev-agent-lens

# Add an Arize source (requires API key in env)
dal config add-source my-arize \
    --type arize \
    --space-key "your-space-key" \
    --model-id "your-model"

# View full configuration
dal config show
```

## Step 2: Sync Historical Data

For initial data population, use `sync-historical`:

```bash
# Sync all available data from a source
dal sync-historical --source phoenix-local-alex

# Sync last 30 days only
dal sync-historical --source arize-ax-alex --days 30

# Sync specific date range
dal sync-historical --source arize-ax-alex \
    --start-date 2025-11-01 \
    --end-date 2025-12-31

# Check sync progress
dal sync-historical --source arize-ax-alex --status
```

**Key Options:**
- `--batch-size N` - Days per batch (default: 1)
- `--timeout N` - Seconds per request (default: 120)
- `--delay N` - Seconds between requests (default: 2.0)
- `--reset` - Clear checkpoint and start fresh
- `--skip-normalize` - Save raw data only (faster for large backfills)

**Resume Capability:**
Historical syncs automatically save checkpoints. If interrupted, simply re-run the same command to resume from where it stopped.

## Step 3: Incremental Sync

After initial historical sync, use regular `sync` for incremental updates:

```bash
# Sync from a specific source (incremental)
dal sync --source phoenix-local-alex

# Sync from all configured sources
dal sync --all-sources

# Full sync (ignore state, refetch everything)
dal sync --source phoenix-local-alex --full
```

## Step 4: Query and Analyze Data

### View Statistics

```bash
# Stats for a source
dal stats --source phoenix-local-alex

# Stats grouped by session
dal stats --source phoenix-local-alex --by-session

# JSON output for programmatic use
dal stats --source phoenix-local-alex --output json
```

### View Sessions

```bash
# View a specific session
dal session abc123

# JSON output
dal session abc123 --output json
```

### Daily Usage Report

```bash
# Last 7 days
dal daily-usage

# Last 30 days
dal daily-usage --days 30

# JSON output
dal daily-usage --output json
```

### LLM-Powered Analysis

These commands require OpenAI API key:

```bash
# Summarize a session
dal summarize abc123

# Preview what would be sent to LLM
dal summarize abc123 --preview

# Cluster sessions by behavior
dal cluster --sample 20

# Get improvement suggestions
dal suggest abc123
```

### Cost Analysis

```bash
# Cost for sessions related to a meeting
dal cost meeting 712a463f-4417-4765-8ce6-7f01ecd33ba0

# Cost for a Linear ticket
dal cost ticket ENG2-123
```

## Data Format

### Raw Spans (JSONL)

Each line in raw files is a JSON object:

```json
{
    "span_id": "ae2549ee2fcfb489",
    "trace_id": "0a2cca0aacaca1b82e745c0cacb5dff4",
    "parent_id": "22406fcbbfbc24f5",
    "name": "Claude_Code_Internal_Prompt_0",
    "span_kind": "LLM",
    "start_time": "2025-12-30T22:57:04.026458+00:00",
    "end_time": "2025-12-30T22:57:04.026476+00:00",
    "input_value": "[{\"type\": \"text\", ...}]",
    "output_value": null,
    "llm_model_name": "claude-haiku-4-5-20251001",
    "llm_token_count_prompt": 1234,
    "llm_token_count_completion": 567,
    "backend": "phoenix",
    "_backend": "phoenix-local-alex",
    "_sync_time": "2025-12-30T14:57:35.652807"
}
```

### Session Files

Session files contain normalized spans grouped by trace_id (session):
- `sessions_YYYYMMDD.jsonl` - Date-stamped files
- `sessions_current.jsonl` - Symlink to latest

## Programmatic Access

### Python

```python
import json
from pathlib import Path

# Read sessions for a source
sessions_file = Path.home() / ".dal/data/sessions/phoenix-local-alex/sessions_current.jsonl"

with open(sessions_file) as f:
    for line in f:
        span = json.loads(line)
        print(f"Session: {span['trace_id']}, Span: {span['name']}")
```

### Using Pandas

```python
import pandas as pd

# Load all sessions
df = pd.read_json(
    "~/.dal/data/sessions/phoenix-local-alex/sessions_current.jsonl",
    lines=True
)

# Group by session
sessions = df.groupby('trace_id')
print(f"Found {len(sessions)} sessions with {len(df)} total spans")
```

## Troubleshooting

### Sync Hanging/Slow

- Increase `--timeout` for large batches
- Increase `--delay` to avoid rate limiting
- Use `--batch-hours 6` for high-volume days

### Resume Not Working

Check if checkpoint exists:
```bash
ls -la ~/.dal/state/historical-sync-*.json
```

Reset and start fresh:
```bash
dal sync-historical --source my-source --reset
```

### Missing Data

Check raw files were synced:
```bash
ls -la ~/.dal/data/raw/my-source/
```

Check if normalization ran:
```bash
ls -la ~/.dal/data/sessions/my-source/
```
