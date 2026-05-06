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
