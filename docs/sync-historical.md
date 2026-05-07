# Syncing Historical Data

Pull trace data from Phoenix or Arize into local storage for offline analysis.

## Setup

Before syncing, configure a named source:

```bash
# For Phoenix (local, REST)
dal config add-source my-phoenix --type phoenix \
    --url http://localhost:6006 --project default

# For Phoenix on external Postgres (recommended when Phoenix is configured
# with PHOENIX_SQL_DATABASE_URL — bypasses the REST API and reads straight
# from the DB. --connection-url and --schema fall back to env vars
# PHOENIX_SQL_DATABASE_URL / PHOENIX_SQL_DATABASE_SCHEMA, so credentials
# don't need to live on the command line.)
dal config add-source my-phoenix-pg --type phoenix-postgres \
    --project dev-agent-lens --shared

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
| `--batch-hours` | Hours per batch (overrides --batch-size, for high-volume days) |
| `--limit` | Max spans per batch (default: 50000 for HTTP, 500000 for SQLite) |
| `--delay` | Delay between API requests in seconds, default: 2.0 |
| `--timeout` | Timeout per API request in seconds, default: 120 |
| `--retries` | Retries per batch on failure, default: 3 |
| `--no-auto-subdivide` | Disable automatic time subdivision (see below) |
| `--skip-normalize` | Skip normalization, save raw data only (faster for large backfills) |
| `--status` | Show sync progress without syncing |
| `--history` | Include completed syncs in `--status` output |
| `--reset` | Clear checkpoint and start fresh |
| `--force-resume` | Resume existing checkpoint regardless of date range |
| `--clean` | Delete completed sync state files |
| `--with-annotations` | Also fetch annotations (Phoenix only, slower) |
| `--sqlite` | Use direct SQLite access instead of HTTP API (Phoenix only) |
| `--sqlite-container` | Docker container name for SQLite access |

## Auto-Subdivision

When a batch exceeds `--limit` spans, DAL automatically subdivides the time window into smaller chunks and retries. This ensures complete data capture even for high-volume periods.

**How it works:**
1. Request spans for time window (e.g., one day)
2. If result hits the limit, split window in half
3. Recursively subdivide until each chunk is under the limit
4. Progress is tracked per sub-batch

**Example output:**
```
[0%] Batch 1/180: 2026-01-05 to 2026-01-06 hit limit (10,000), subdividing...
  → 13:10-01:10: hit limit, subdividing...
      → 13:10-19:10: hit limit, subdividing...
          → 13:10-16:10: 510 spans
          → 16:10-19:10: hit limit, subdividing...
              → 16:10-17:40: 5,717 spans
              → 17:40-19:10: 10,000 spans (at limit)
```

**Tips for high-volume sources:**
- Use `--limit 10000` (lower limit) for more frequent checkpoints
- Use `--delay 5` to avoid overwhelming the server
- Auto-subdivide handles the rest automatically

To disable: `--no-auto-subdivide` (not recommended)

## Annotations

Phoenix and Arize handle annotations differently:

- **Phoenix**: Annotations (human feedback, labels, scores) are stored separately from spans. Use `--with-annotations` to fetch them. This makes an additional API call per batch, which is slower.
- **Arize**: Annotations are embedded directly in span attributes during export, so they're automatically included - no extra flag needed.

```bash
# Sync Phoenix with annotations
dal sync-historical --source my-phoenix --with-annotations

# Arize already includes annotations in span data
dal sync-historical --source my-arize
```

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

Checkpoints are stored in `~/.dal/state/`.

## Checking Sync Status

View the status of all syncs:

```bash
# Show in-progress syncs
dal sync-historical --status

# Show in-progress AND completed syncs
dal sync-historical --status --history

# Or just use --history (implies --status)
dal sync-historical --history
```

**Example output with `--history`:**
```
Historical Sync Status

  arize-litellm: 17.2% stale (process died) (ETA: 13:02:40)
    Range: 2025-07-09 to 2026-01-05
    Spans: 1,145,903
    Batches: 31 completed, 100 failed
    Remaining gaps: 4

Completed Historical Syncs

  phoenix-local-alex: completed
    Last sync: 2026-01-07 12:25:41
    Type: phoenix
    Backend: Phoenix @ http://localhost:6006
```

The `--history` flag is useful for agents or scripts that need to understand what data has already been synced.

### Force Full Resync

```bash
dal sync-historical --source my-phoenix --reset
```

### Force Resume Existing Checkpoint

If you have an existing checkpoint and want to resume it regardless of the date range you specify:

```bash
# Resume existing checkpoint, ignoring --days or --start-date
dal sync-historical --source my-phoenix --force-resume
```

### Clean Up Completed Syncs

After syncs complete, state files remain in `~/.dal/state/`. Clean them up with:

```bash
# Delete all completed sync state files
dal sync-historical --clean
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

## SQLite Direct Access (Phoenix Only)

For high-volume Phoenix instances, the HTTP API can timeout or crash under load. SQLite direct access bypasses the API entirely by querying Phoenix's SQLite database directly.

**Requirements:**
- Phoenix must be running in Docker on the same machine
- You need the Docker container name (e.g., `dev-agent-lens-phoenix-1`)

### Setup

**Option 1: Specify container at sync time**
```bash
dal sync-historical --source my-phoenix \
    --sqlite --sqlite-container dev-agent-lens-phoenix-1
```

**Option 2: Save container in source config (recommended)**
```bash
# Add sqlite-container to source config
dal config add-source my-phoenix --type phoenix \
    --url http://localhost:6006 \
    --project default \
    --sqlite-container dev-agent-lens-phoenix-1

# Then just use --sqlite flag
dal sync-historical --source my-phoenix --sqlite
```

### When to Use SQLite Mode

Use `--sqlite` when:
- HTTP API times out on large queries
- You see "peer closed connection" errors
- High-volume days fail repeatedly even with auto-subdivision
- You want faster sync speeds (SQLite is ~10x faster)

**SQLite mode benefits:**
- **10x higher default limit**: 500,000 spans per batch vs 50,000 for HTTP
- **Minimal subdivision**: Most days fit in a single batch, no recursive splitting
- **Complete data capture**: High-volume days are fetched without truncation
- **Memory-safe**: 500k limit uses ~2-3GB RAM, safe for most systems

**Performance comparison:**
| Method | Speed | Reliability | Default Limit |
|--------|-------|-------------|---------------|
| HTTP API | ~50 spans/sec | Can timeout on large queries | 50,000 |
| SQLite | ~500 spans/sec | Direct database access, no timeouts | 500,000 |

### Limitations

- **Local only**: SQLite mode only works when Phoenix runs in Docker on the same machine
- **Phoenix only**: Arize cloud doesn't expose SQLite access
- **Single source**: Can only sync one source at a time with `--sqlite`

### Finding Your Container Name

```bash
# List running Docker containers
docker ps --format "table {{.Names}}\t{{.Image}}" | grep phoenix

# Or check docker-compose
docker compose ps
```

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
