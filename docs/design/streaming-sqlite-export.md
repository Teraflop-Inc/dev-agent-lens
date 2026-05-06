# Streaming SQLite Export for Large Queries

## Problem

When fetching large numbers of spans (25k+) via `docker exec python3`, the Python process inside the container OOMs because:
1. SQLite cursor fetches all rows into memory (`cursor.fetchall()`)
2. Converts all rows to Python dicts
3. JSON-serializes the entire list as one string
4. Prints to stdout

Each step accumulates memory, and with 25k spans (each with large `attributes` JSON blobs), this can easily exceed available memory.

## Current Approach (OOMs at ~75k spans)

```python
# Inside docker exec
cursor.execute(query)
columns = [desc[0] for desc in cursor.description]
rows = [dict(zip(columns, row)) for row in cursor.fetchall()]  # OOM here
print(json.dumps(rows))
```

## Proposed: Streaming NDJSON Export

Instead of building one giant JSON array, stream rows as newline-delimited JSON (NDJSON):

```python
# Inside docker exec - streams one row at a time
cursor.execute(query)
columns = [desc[0] for desc in cursor.description]

for row in cursor:  # Iterates without loading all into memory
    record = dict(zip(columns, row))
    print(json.dumps(record))  # One line per record
```

### Benefits
- **Constant memory**: Only one row in memory at a time
- **No size limit**: Can export millions of rows
- **Simpler parsing**: NDJSON is easy to parse line-by-line

### Implementation Changes

#### 1. Update `PhoenixSQLiteClient._execute_query_docker()`

```python
def _execute_query_docker_streaming(
    self,
    query: str,
    params: tuple[Any, ...] = (),
) -> Iterator[dict[str, Any]]:
    """Execute query via Docker container with streaming output."""

    python_code = f'''
import sqlite3
import json

conn = sqlite3.connect({json.dumps(self._container_db_path)})
cursor = conn.cursor()
cursor.execute({json.dumps(query)}, {json.dumps(params)})

columns = [desc[0] for desc in cursor.description]

# Stream one row at a time as NDJSON
for row in cursor:
    record = dict(zip(columns, row))
    print(json.dumps(record, default=str))

conn.close()
'''

    # Use subprocess with streaming stdout
    process = subprocess.Popen(
        ["docker", "exec", self._container_name, "python3", "-c", python_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # Line buffered
    )

    # Yield rows as they come
    for line in process.stdout:
        line = line.strip()
        if line:
            yield json.loads(line)

    # Check for errors
    process.wait()
    if process.returncode != 0:
        stderr = process.stderr.read()
        raise PhoenixSQLiteQueryError(f"Docker exec failed: {stderr}")
```

#### 2. Update `get_spans_dataframe()` to use streaming

```python
def get_spans_dataframe_streaming(
    self,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
    chunk_size: int = 10000,
) -> Iterator[pd.DataFrame]:
    """Yield DataFrames in chunks for memory-efficient processing."""

    # Build query (same as before)
    query = self._build_spans_query(start_time, end_time, limit, offset)

    # Collect rows in chunks
    chunk = []
    for row in self._execute_query_docker_streaming(query, params):
        chunk.append(row)

        if len(chunk) >= chunk_size:
            yield self._rows_to_dataframe(chunk)
            chunk = []

    # Yield remaining rows
    if chunk:
        yield self._rows_to_dataframe(chunk)
```

#### 3. Update CLI to consume streaming DataFrames

```python
# In pagination section of fetch_with_subdivision
for chunk_df in client.get_spans_dataframe_streaming(
    start_time=batch_start,
    end_time=batch_end,
    limit=None,  # No limit - stream everything
    chunk_size=10000,
):
    # Save each chunk immediately
    if normalizer:
        normalized = normalizer(chunk_df)
        store.append_spans(normalized, backend=backend_name)
    else:
        store.append_spans(chunk_df, backend=backend_name)

    total_fetched += len(chunk_df)
    click.echo(f"  +{len(chunk_df):,} spans (total: {total_fetched:,})")
```

## Alternative: Copy SQLite DB Out

Another approach is to copy the entire SQLite database out of the container:

```bash
docker cp dev-agent-lens-phoenix-1:/root/.phoenix/phoenix.db /tmp/phoenix.db
```

Then query locally with full Python/pandas capabilities. This is simpler but:
- Requires copying a potentially large file (could be 10GB+)
- File could be in-use/locked
- Doesn't work for ongoing syncs

## Recommended Approach

1. **Short term**: Use `--limit 10000` for pagination (current workaround)
2. **Medium term**: Implement streaming NDJSON export
3. **Long term**: Consider direct SQLite file access via docker cp for initial bulk sync

## Complexity Estimate

- Streaming NDJSON: ~2-3 hours
  - Update `_execute_query_docker` to use Popen with streaming
  - Add NDJSON parsing
  - Update `get_spans_dataframe` to yield chunks
  - Update CLI pagination to consume iterator
  - Test with large datasets

- Docker cp approach: ~1 hour
  - Add method to copy DB file
  - Handle file locking concerns
  - Switch to local SQLite connection
