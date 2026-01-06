# Syncing Historical Data

Pull trace data from Phoenix or Arize into local storage for offline analysis.

## Setup

Before syncing, configure a named source:

```bash
# For Phoenix (local)
dal config add-source my-phoenix --type phoenix \
    --url http://localhost:6006 --project default

# For Arize (cloud) - requires env vars
export ARIZE_API_KEY=your-api-key
export ARIZE_SPACE_KEY=your-space-key
dal config add-source my-arize --type arize --model-id my-model

# List configured sources
dal config list-sources
```

## Basic Usage

```bash
# Sync all available data from a source
dal sync-historical --source my-phoenix

# Sync from a specific date
dal sync-historical --source my-arize --start-date 2024-01-01

# Sync a date range
dal sync-historical --source my-phoenix \
    --start-date 2024-01-01 --end-date 2024-01-31

# Sync last N days
dal sync-historical --source my-phoenix --days 30
```

## CLI Options

| Option | Description |
|--------|-------------|
| `--source` | Named source (configured via `dal config add-source`) |
| `--start-date` | Start date (YYYY-MM-DD) |
| `--end-date` | End date (YYYY-MM-DD), default: today |
| `--days` | Number of days to sync (alternative to start-date) |
| `--batch-size` | Days per batch, default: 1 |
| `--limit` | Max spans per batch, default: 50000 |
| `--status` | Show sync progress without syncing |
| `--reset` | Clear checkpoint and start fresh |

## Incremental Sync

DAL tracks sync progress and can resume interrupted syncs:

```bash
# First run - syncs everything
dal sync-historical --source my-phoenix

# Second run - resumes from checkpoint
dal sync-historical --source my-phoenix

# Check progress
dal sync-historical --status
```

Checkpoints are stored in `~/.dal/data/state/`.

### Force Full Resync

```bash
dal sync-historical --source my-phoenix --reset
```

## Export to Parquet

After syncing, export to optimized Parquet format:

```bash
# Export a source
dal export-parquet --source my-phoenix

# Update existing Parquet (only new data)
dal export-parquet --source my-phoenix --update
```

Parquet provides:
- ~97% smaller files (ZSTD compression)
- 10-100x faster queries (DuckDB backend)
- Columnar storage for analytics

## Data Location

```
~/.dal/data/
├── raw/                    # Raw JSONL from sync
│   └── my-phoenix/
│       ├── sessions.jsonl
│       └── spans.jsonl
├── parquet/               # Optimized Parquet files
│   ├── my-phoenix_sessions.parquet
│   └── my-phoenix_spans.parquet
├── state/                 # Sync checkpoints
│   └── my-phoenix.json
└── unified/               # Unified format (legacy)
```

## Workflow

Typical workflow for a new project:

```bash
# 1. Configure source
dal config add-source my-phoenix --type phoenix \
    --url http://localhost:6006 --project default

# 2. Sync historical data
dal sync-historical --source my-phoenix

# 3. Export to Parquet
dal export-parquet --source my-phoenix

# 4. Query locally
dal query my-phoenix --limit 10

# 5. Keep updated (run periodically)
dal sync-historical --source my-phoenix
dal export-parquet --source my-phoenix --update
```

## Rate Limiting

Both Phoenix and Arize have rate limits. DAL handles this automatically:
- Failed batches are queued for retry
- Exponential backoff between retries
- Progress is checkpointed so you can stop/resume

## Troubleshooting

**No data synced:**
- Check source is configured: `dal config list-sources`
- Verify date range contains data
- Check Phoenix/Arize credentials

**Sync interrupted:**
- Just run the same command again - it resumes from checkpoint
- Use `--reset` to start fresh if needed

**Slow sync:**
- Reduce `--batch-size` for more frequent checkpoints
- Run overnight for large historical syncs

**Parquet export fails:**
- Ensure raw data exists in `~/.dal/data/raw/`
- Check disk space for output
