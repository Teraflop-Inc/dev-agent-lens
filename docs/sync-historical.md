# Syncing Historical Data

Pull trace data from Phoenix or Arize into local storage for offline analysis.

## Basic Usage

### From Phoenix

```bash
# Sync a specific project
dal sync-historical --source phoenix --project my-project --start 2024-01-01

# With end date
dal sync-historical --source phoenix --project my-project \
    --start 2024-01-01 --end 2024-01-31
```

### From Arize

```bash
# Sync from Arize (uses ARIZE_* env vars)
dal sync-historical --source arize --start 2024-01-01
```

Required environment variables for Arize:
```bash
export ARIZE_API_KEY=your-api-key
export ARIZE_SPACE_KEY=your-space-key
export ARIZE_MODEL_ID=your-model-id  # Optional, defaults to project name
```

## Configuration

### Environment Variables

```bash
# Phoenix
export DAL_PHOENIX_URL=http://localhost:6006
export DAL_PHOENIX_PROJECT=my-project

# Arize
export ARIZE_API_KEY=your-api-key
export ARIZE_SPACE_KEY=your-space-key
```

### CLI Options

| Option | Description |
|--------|-------------|
| `--source` | Backend: `phoenix` or `arize` |
| `--project` | Project/model name |
| `--start` | Start date (YYYY-MM-DD) |
| `--end` | End date (YYYY-MM-DD), default: today |
| `--output` | Output directory, default: `~/.dal/data/` |
| `--batch-size` | Records per batch, default: 1000 |

## Incremental Sync

DAL tracks sync progress and can resume interrupted syncs:

```bash
# First run - syncs everything
dal sync-historical --source phoenix --project my-project --start 2024-01-01

# Second run - only syncs new data since last checkpoint
dal sync-historical --source phoenix --project my-project --start 2024-01-01
```

Checkpoints are stored in `~/.dal/data/state/`.

### Force Full Resync

```bash
dal sync-historical --source phoenix --project my-project \
    --start 2024-01-01 --force
```

## Export to Parquet

After syncing, export to optimized Parquet format:

```bash
# Export a source
dal export-parquet --source my-project

# Update existing Parquet (only new data)
dal export-parquet --source my-project --update
```

Parquet provides:
- ~97% smaller files (ZSTD compression)
- 10-100x faster queries (DuckDB backend)
- Columnar storage for analytics

## Data Location

```
~/.dal/data/
├── raw/                    # Raw JSONL from sync
│   └── my-project/
│       ├── sessions.jsonl
│       └── spans.jsonl
├── parquet/               # Optimized Parquet files
│   ├── my-project_sessions.parquet
│   └── my-project_spans.parquet
├── state/                 # Sync checkpoints
│   └── my-project.json
└── unified/               # Unified format (legacy)
```

## Workflow

Typical workflow for a new project:

```bash
# 1. Sync historical data
dal sync-historical --source phoenix --project my-project --start 2024-01-01

# 2. Export to Parquet
dal export-parquet --source my-project

# 3. Query locally
dal query my-project --limit 10

# 4. Keep updated (run periodically)
dal sync-historical --source phoenix --project my-project --start 2024-01-01
dal export-parquet --source my-project --update
```

## Rate Limiting

Both Phoenix and Arize have rate limits. DAL handles this automatically:
- Failed batches are queued for retry
- Exponential backoff between retries
- Progress is checkpointed so you can stop/resume

## Troubleshooting

**No data synced:**
- Check project name matches exactly
- Verify date range contains data
- Check Phoenix/Arize credentials

**Sync interrupted:**
- Just run the same command again - it resumes from checkpoint
- Use `--force` to start fresh if needed

**Slow sync:**
- Reduce `--batch-size` for more frequent checkpoints
- Run overnight for large historical syncs

**Parquet export fails:**
- Ensure raw data exists in `~/.dal/data/raw/`
- Check disk space for output
