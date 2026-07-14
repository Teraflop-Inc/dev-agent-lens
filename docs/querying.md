# Querying Data

Query your synced trace data using the `dal` CLI or Python API.

## CLI Usage

### List Available Sources

```bash
dal sources
```

Shows all data sources in `~/.dal/data/` with row counts.

### Basic Queries

```bash
# Query a source (auto-detects Parquet or JSONL)
dal query my-project

# Limit results
dal query my-project --limit 100

# Get specific session
dal query my-project --session abc123
```

### Filtering

```bash
# By status
dal query my-project --status ERROR
dal query my-project --status OK

# By model
dal query my-project --model claude-sonnet

# By time range
dal query my-project --start 2024-01-01 --end 2024-01-31

# Regex pattern search
dal query my-project --pattern "TODO|FIXME"
dal query my-project --pattern "ENG-\d+" --case-insensitive
```

### Export Formats

```bash
# JSON output
dal query my-project --format json > results.json

# CSV output
dal query my-project --format csv > results.csv

# Markdown table
dal query my-project --format markdown
```

## Python API

### Basic Query

```python
from dev_agent_lens.query import query_source

result = query_source(source="my-project")
print(f"Found {result.total_spans} spans in {result.total_sessions} sessions")

# Access data
for session in result.sessions:
    print(f"Session: {session['session_id']}")
    for span in session.get('spans', []):
        print(f"  - {span.get('name')}: {span.get('status_code')}")
```

### Filtering

```python
from dev_agent_lens.query import query_source

# Filter by multiple criteria
result = query_source(
    source="my-project",
    session_id="abc123",           # Specific session
    status_code="ERROR",           # Filter by status
    model_name="claude",           # Partial match, case-insensitive
    pattern=r"TICKET-\d+",         # Regex search
    case_insensitive=True,
    start_time="2024-01-01",
    end_time="2024-01-31",
    limit=500,
)
```

### Direct Parquet Queries

For maximum performance on large datasets:

```python
from dev_agent_lens.query import query_parquet, find_parquet_files, get_parquet_stats

# Discover available sources
sources = find_parquet_files()
# → {'my-project': {'spans': Path(...), 'sessions': Path(...)}, ...}

# Get file stats without loading
stats = get_parquet_stats("~/.dal/data/parquet/my-project_spans.parquet")
# → {'row_count': 1925899, 'session_count': 21487, 'file_size_bytes': 1879535936}

# Direct Parquet query
result = query_parquet(
    spans_path="~/.dal/data/parquet/my-project_spans.parquet",
    status_code="ERROR",
    limit=500,
)
```

### Export Functions

```python
from dev_agent_lens.query import query_source, export_json, export_csv, export_markdown

result = query_source(source="my-project", limit=100)

# Export to different formats
json_str = export_json(result)
csv_str = export_csv(result)
markdown_str = export_markdown(result)
```

## QueryResult Object

All queries return a `QueryResult` object:

```python
result.total_spans      # Total span count
result.total_sessions   # Total session count
result.sessions         # List of session dicts with nested spans
result.spans            # Flat list of all spans
result.metadata         # Query metadata (source, filters, timing)
```

## Performance

The Parquet backend provides significant improvements over JSONL:

| Dataset Size | Rows | Query Time |
|--------------|------|------------|
| ~2 MB | 2,500 | 0.03-0.12s |
| ~30 MB | 22,000 | 0.15-0.22s |
| ~1.8 GB | 1.9M | 2.5-4.5s |

Storage is also ~97% smaller (52 GB JSONL → 1.8 GB Parquet with ZSTD).

### Tips

1. **Use Parquet** - Always export to Parquet for repeated queries
2. **Filter early** - Apply filters in the query rather than post-processing
3. **Limit results** - Use `limit` to cap result size for exploratory queries
4. **Use `find_parquet_files()`** - Discover sources without hardcoding paths

## API Reference

| Function | Description |
|----------|-------------|
| `query_source()` | Auto-select backend, query by source name |
| `query_parquet()` | Direct Parquet query with DuckDB |
| `search_parquet()` | Regex search on Parquet data |
| `find_parquet_files()` | Discover available Parquet sources |
| `get_parquet_stats()` | Get file statistics without loading |
| `export_json()` | Export results to JSON |
| `export_csv()` | Export results to CSV |
| `export_markdown()` | Export results to Markdown table |

## Conversation reconstruction & mining (fabric)

The `fabric` layer turns raw spans into readable, per-session conversations — one command
to "give me the conversations matching this signal," so eval mining (IR, M12, …) stops
re-hand-rolling DuckDB + per-session stitching. It wraps the query → chain → markdown
pipeline (`dev_agent_lens.fabric`).

### CLI

```bash
# Reconstruct one session's conversation to markdown (start_time order)
dal reconstruct-session <session_id> --source phoenix-local-alex -o session.md
dal reconstruct-session <session_id>                 # print to stdout

# List sessions by content pattern, tool usage, date, and size (newest first)
dal list-sessions --source phoenix-local-alex \
  --pattern transcript \
  --tools 'mcp__claude_ai_Linear,mcp__claude_ai_Notion' \
  --min-spans 50 --since 2026-05-01 \
  --output json

# Bulk-export N matching sessions to one .md per session (written atomically)
dal export-conversations --source phoenix-lambda2-dal \
  --filter transcript --limit 20 -o ./mining-batch/
```

Omit `--source` to fan out across every source under `~/.dal/data`.

### Python API

```python
from dev_agent_lens.fabric import (
    list_sessions,         # filters → session dicts (newest first)
    reconstruct_session,   # session_id → markdown export, in time order
    export_conversations,  # bulk write one .md per session, atomically
)

# Fan out a batch of gold examples for a miner
for path in export_conversations(source="phoenix-local-alex",
                                 pattern="transcript", limit=20,
                                 output_dir="./batch/"):
    print(path)

# Or one at a time
export = reconstruct_session("abc123", source="phoenix-local-alex")
Path("session.md").write_text(export.main_content)
```

`export_conversations` writes each file temp-then-rename, so a crash never leaves a
partial session on disk — safe to point a miner at the output dir while it runs.

### Business-entity lookups

`dal meeting-sessions <id>`, `dal ticket-sessions ENG2-123`, and
`dal session-context <session_id>` surface which sessions reference a meeting / ticket,
and what entities (meetings, tickets), tokens, and duration a session touched.

### Notes

- **Reconstruction** links a session's spans into a `ConversationChain` (grouping by
  Claude session-UUID, or temporal proximity). A session with no recognizable UUID and no
  neighbours is rendered as a standalone single-session chain rather than dropped.
- **Filtering**: `--pattern`/`--filter` and `--since`/`--until` push down into the parquet
  query; `--tools` and `--min-spans` are applied on top.

| Function | Description |
|----------|-------------|
| `list_sessions()` | Sessions matching pattern/tool/date/size filters, newest first |
| `reconstruct_session()` | One session → markdown export in start_time order |
| `export_conversations()` | Bulk per-session `.md` export, written atomically |
| `get_session_context()` | Meetings/tickets/tokens/duration referenced by a session |
