# Markdown Export Pipeline Documentation

## Overview

The markdown export pipeline in dev-agent-lens is an end-to-end system that transforms raw OpenTelemetry traces from Phoenix and Arize into human-readable markdown documentation. This document covers the complete pipeline from initial data collection through final markdown export.

**Pipeline Stages**:
1. Data Sources (Phoenix/Arize backends)
2. Historical Sync (fetching trace data)
3. Raw Storage (JSONL files)
4. Schema Normalization (unified format)
5. Session Unification (grouping spans)
6. Parquet Storage (optimized storage)
7. Chain Building (linking sessions)
8. Markdown Export (human-readable output)

---

## Part I: Upstream Pipeline (Data Collection)

This section documents how trace data flows from Phoenix/Arize into the system.

---

## 1. Data Sources

### 1.1 Phoenix Local Server

**Client**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/clients/phoenix.py`

Phoenix is an open-source observability platform that collects OpenTelemetry traces from local development environments.

**Connection Configuration** (lines 47-56):
```python
class PhoenixClient:
    def __init__(
        self,
        base_url: str | None = None,      # Default: http://localhost:6006
        project_name: str | None = None,  # Default: "default"
        timeout: float = 30.0,
    ):
        self.base_url = base_url or os.getenv("DAL_PHOENIX_URL", "http://localhost:6006")
        self.project_name = project_name or os.getenv("DAL_PHOENIX_PROJECT", "default")
```

**Key Methods**:

1. **`get_spans_dataframe()`** (lines 81-129):
   - Fetches spans from Phoenix server via HTTP API
   - Returns pandas DataFrame with Phoenix's native schema
   - Parameters: `project_name`, `start_time`, `end_time`, `limit`
   - Default limit: 100,000 spans per request

2. **`get_span_annotations_dataframe()`** (lines 131-191):
   - Fetches human/LLM annotations for spans
   - Used for quality evaluation data
   - Optional feature (disabled by default in sync)

**Phoenix Schema** (raw format before normalization):
- `context.span_id` - Unique span identifier
- `context.trace_id` - Trace group identifier
- `parent_id` - Parent span reference
- `name` - Span name (e.g., "Claude_Code_Tool_Read")
- `span_kind` - Type: LLM, TOOL, CHAIN, etc.
- `attributes` - Nested JSON with all metadata
- `start_time`, `end_time` - Timestamps

### 1.2 Arize Cloud Platform

**Client**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/clients/arize.py`

Arize is a cloud-based ML observability platform that stores production trace data.

**Connection Configuration** (lines 49-58):
```python
class ArizeClient:
    def __init__(
        self,
        api_key: str | None = None,      # From ARIZE_API_KEY env var
        space_key: str | None = None,    # From ARIZE_SPACE_KEY env var
        model_id: str | None = None,     # Default: "dev-agent-lens"
    ):
```

**Key Methods**:

1. **`get_spans_dataframe()`** (lines 89-174):
   - Fetches spans via Arize Export API
   - Required: `start_time` and `end_time` (no "all data" mode)
   - Returns DataFrame with Arize's native schema
   - Note: API fetches ALL spans in range, then truncates to limit

2. **`get_spans_parquet()`** (lines 176-263):
   - More efficient for large datasets
   - Streams directly to disk without loading into memory
   - Useful for initial historical backfills

**Performance Parameters**:
- `columns` - Specify subset of columns to reduce transfer size
- `stream_chunk_size` - Rows per chunk (larger = faster)
- `parallelize_exports` - Enable parallel fetching

**Arize Schema** (raw format, differs from Phoenix):
- Different column names and nesting structure
- Normalized to unified schema later in pipeline

### 1.3 Source Configuration

**File**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/core/sources.py`

Sources are named configurations for backends, stored in `~/.dal/config/sources.json`.

**Example Configuration** (lines 10-27):
```json
{
    "version": 1,
    "sources": {
        "phoenix-alex": {
            "type": "phoenix",
            "url": "localhost:6006",
            "project": "dev-agent-lens",
            "local_only": true
        },
        "arize-team": {
            "type": "arize",
            "space_key": "U3BhY2U6...",
            "model_id": "dev-agent-lens",
            "local_only": false
        }
    }
}
```

**SourceConfig Class** (lines 47-149):
- Manages backend connection parameters
- `local_only` flag controls whether data syncs to remote
- Validates required fields per source type

**Managing Sources**:
```bash
dal config add-source <name>     # Interactive setup
dal config list-sources           # Show all sources
dal config remove-source <name>   # Delete source
```

---

## 2. Historical Sync Pipeline

The historical sync command performs one-time backfills of large date ranges.

### 2.1 Command: `dal sync-historical`

**Location**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/cli/main.py` (lines 658-1847)

**Purpose**: Download all available trace data from a source in batched, resumable fashion.

**Usage Examples**:
```bash
# Sync everything from a source
dal sync-historical --source phoenix-local-alex

# Sync last 30 days
dal sync-historical --source arize-ax-alex --days 30

# Sync specific date range
dal sync-historical --source phoenix-local-alex --start-date 2025-11-01 --end-date 2025-12-31

# Check progress
dal sync-historical --source phoenix-local-alex --status

# Resume after interruption (automatic)
dal sync-historical --source phoenix-local-alex

# High-volume Phoenix with smaller batches
dal sync-historical --source phoenix-local-alex --limit 10000 --delay 5
```

**Key Features**:
1. **Resume capability** - Saves checkpoints, continues from interruption
2. **Auto-subdivision** - Splits high-volume days into smaller time windows
3. **Progress tracking** - Shows ETA and percentage complete
4. **State integration** - Updates sync state for incremental syncs

### 2.2 Historical Sync State Management

**File**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/core/historical_sync.py`

**State Storage**: `~/.dal/state/historical-sync-{source}.json`

**State Format (v4)** (lines 10-50):
```json
{
    "version": 4,
    "source": "phoenix-local-alex",
    "started_at": "2025-01-01T00:00:00",
    "updated_at": "2025-01-01T12:00:00",
    "target_range": {
        "start": "2025-11-01",
        "end": "2025-12-31"
    },
    "completed_ranges": [
        {"start": "2025-12-25", "end": "2025-12-31", "spans": 45000},
        {"start": "2025-12-18", "end": "2025-12-25", "spans": 52000}
    ],
    "partial_ranges": {
        "2025-12-11": [
            {"start": "2025-12-11 00:00:00", "end": "2025-12-11 06:00:00", "spans": 5000}
        ]
    },
    "current_batch": {
        "start": "2025-12-11",
        "end": "2025-12-18"
    },
    "stats": {
        "total_spans": 97000,
        "batches_completed": 2,
        "batches_failed": 0,
        "subdivisions": 1
    }
}
```

**Key Classes**:

1. **`HistoricalSyncState`** (lines 250-350):
   - Tracks which date ranges have been completed
   - Stores failed batches for retry
   - Detects stale state (process died mid-sync)

2. **`DateRange`** (lines 84-114):
   - Represents a time window with span count
   - Methods: `overlaps()`, `contains()`

3. **`SyncConfig`** (lines 146-181):
   - Batch size (days or hours)
   - Limit per request
   - Timeout and delay settings

**Staleness Detection** (lines 294-308):
- Checks if process is still alive via PID
- Threshold: 5 minutes without updates
- Allows safe resume if process crashed

### 2.3 Sync Execution Flow

**Implementation**: `dev_agent_lens/cli/main.py`, lines 850-1847

**Step 1: Initialize State** (lines 850-970):
```python
from dev_agent_lens.core.historical_sync import HistoricalSyncState

# Load or create state
state = HistoricalSyncState.load(source_name) or create_new_state()

# Check if resuming
if state.current_batch and not reset:
    click.echo(f"Resuming from {state.current_batch.start}")
```

**Step 2: Determine Date Range** (lines 971-1120):
- If `--days` specified: Last N days from now
- If `--start-date` specified: Explicit range
- Otherwise: Sync everything available
- Arize requires explicit dates (no auto-detection)

**Step 3: Batch Generation** (lines 1121-1250):
```python
# Generate batches (daily by default)
batches = []
current = start_date
while current < end_date:
    batch_end = min(current + timedelta(days=batch_size), end_date)
    if not state.is_completed(current, batch_end):
        batches.append((current, batch_end))
    current = batch_end

click.echo(f"Processing {len(batches)} batches...")
```

**Step 4: Fetch Loop** (lines 1251-1600):
```python
for batch_start, batch_end in batches:
    click.echo(f"Batch {i+1}/{len(batches)}: {batch_start.date()}")

    # Update state
    state.current_batch = DateRange(batch_start, batch_end)
    state.save()

    # Fetch spans
    df = client.get_spans_dataframe(
        start_time=batch_start,
        end_time=batch_end,
        limit=limit,
    )

    # Check if limit exceeded (auto-subdivide)
    if len(df) >= limit and not no_auto_subdivide:
        # Split batch in half
        mid = batch_start + (batch_end - batch_start) / 2
        sub_batches = [(batch_start, mid), (mid, batch_end)]
        # Recursively process sub-batches

    # Normalize and save
    normalized = normalizer(df)
    store.append_spans(normalized, backend=source_name)

    # Mark complete
    state.completed_ranges.append(DateRange(batch_start, batch_end, len(df)))
    state.current_batch = None
    state.save()
```

**Step 5: Error Handling & Retry** (lines 1601-1847):
- Failed batches tracked in `state.failed_ranges`
- Automatic retry pass with longer delays
- Final persistent retry for stubborn failures
- Interrupt handling: Ctrl+C saves state for resume

---

## 3. Raw Data Storage

### 3.1 Storage Layout

**Base Directory**: `~/.dal/data/`

```
~/.dal/data/
├── raw/                          # Raw JSONL from sync
│   ├── phoenix-local-alex/       # Per-source directories
│   │   ├── sync_20250107_140530.jsonl
│   │   ├── sync_20250107_153022.jsonl
│   │   └── ...
│   └── arize-ax-alex/
│       └── ...
├── sessions/                     # Unified session data
│   ├── phoenix-local-alex/
│   │   ├── sessions_current.jsonl  # Symlink to latest
│   │   ├── sessions_20250107.jsonl
│   │   └── sessions_20250108.jsonl
│   └── ...
├── parquet/                      # Optimized columnar storage
│   ├── phoenix-local-alex_spans.parquet
│   ├── phoenix-local-alex_sessions.parquet
│   └── ...
└── state/                        # Sync state and checkpoints
    ├── historical-sync-phoenix-local-alex.json
    └── match_report.json
```

### 3.2 Raw JSONL Files

**Format**: One JSON object per line (newline-delimited JSON)

**Location**: `~/.dal/data/raw/{source_name}/sync_{timestamp}.jsonl`

**Purpose**:
- Preserve exact data from backend before normalization
- Enable re-normalization if schema changes
- Debugging and auditing

**Content** (after normalization to unified schema):
```json
{"span_id": "abc123", "trace_id": "def456", "name": "Claude_Code_Tool_Read", "start_time": "2025-01-07T14:30:00Z", ...}
{"span_id": "abc124", "trace_id": "def456", "name": "litellm_request", "start_time": "2025-01-07T14:30:05Z", ...}
```

**OxenStore.append_spans()** implementation:
- Creates timestamped JSONL file
- Appends new spans to existing data
- Returns file path for logging

---

## 4. Schema Normalization

### 4.1 Unified Schema

**File**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/core/schema.py`

Different backends (Phoenix, Arize) have different schemas. The unified schema provides a canonical format.

**UnifiedSpan Type** (lines 20-79):
```python
class UnifiedSpan(TypedDict, total=False):
    # Core fields
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    span_kind: str | None        # LLM, TOOL, CHAIN
    start_time: str              # ISO-8601
    end_time: str | None         # ISO-8601
    status_code: str | None

    # Content fields
    input_value: str | None
    output_value: str | None
    input_messages: str | None   # JSON string
    output_messages: str | None  # JSON string

    # LLM fields
    llm_model_name: str | None
    llm_token_count_prompt: int | None
    llm_token_count_completion: int | None
    llm_token_count_total: int | None

    # Metadata
    backend: str                 # "phoenix" or "arize"
    raw_attributes: str | None   # Full original attributes as JSON
```

**Column Order** (lines 82-101):
Enforced order for consistency in parquet files:
```python
UNIFIED_COLUMNS = [
    "span_id", "trace_id", "parent_id", "name", "span_kind",
    "start_time", "end_time", "status_code",
    "input_value", "output_value", "input_messages", "output_messages",
    "llm_model_name", "llm_token_count_prompt", "llm_token_count_completion", "llm_token_count_total",
    "backend", "raw_attributes"
]
```

### 4.2 Normalization Process

**Location**: `dev_agent_lens/cli/main.py`, line 467

```python
# After fetching from backend
spans_df = client.get_spans_dataframe(...)

# Normalize to unified schema
normalized = normalizer(spans_df)  # normalize_phoenix_spans() or normalize_arize_spans()
```

**Phoenix Normalization**:
- Extract `span_id` from `context.span_id`
- Extract `trace_id` from `context.trace_id`
- Flatten nested `attributes` field
- Convert timestamps to ISO-8601 strings
- Preserve full attributes in `raw_attributes`

**Arize Normalization**:
- Map Arize column names to unified names
- Handle different nesting structure
- Convert timestamps to ISO-8601 strings

---

## 5. Session Unification

### 5.1 Session ID Extraction

**File**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/core/unify.py`

**Challenge**: Session metadata is only present on certain span types (LLM requests), not on tool spans.

**Solution**: Extract session IDs and propagate to all spans with the same trace_id.

**Function**: `_extract_session_ids()` (lines 66-130)

```python
def _extract_session_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Add session_id column by extracting from span metadata.

    Priority:
    1. Extract explicit session_id from metadata (session_xxx pattern)
    2. Propagate session_id to all spans sharing the same trace_id
    3. Fall back to trace_id for traces without session metadata
    """

    # First pass: extract session_id from each span
    df["_raw_session_id"] = df.apply(
        lambda row: extract_session_id_from_span(row.to_dict()), axis=1
    )

    # Build mapping: trace_id -> proper session_id
    trace_to_session = {}
    for _, row in df.iterrows():
        trace_id = row.get("trace_id")
        raw_session = row.get("_raw_session_id")
        # If this span has a proper session_id (not just trace_id fallback)
        if raw_session and trace_id and raw_session != trace_id:
            trace_to_session[trace_id] = raw_session

    # Second pass: propagate proper session_ids to all spans in same trace
    df["session_id"] = df.apply(lambda row:
        trace_to_session.get(row["trace_id"]) or row["_raw_session_id"],
        axis=1
    )
```

**Claude Code Pattern**:
- Single conversation generates many trace_ids (one per API call)
- All share same `session_id` in metadata: `user_{hash}_account_{uuid}_session_{uuid}`
- Metadata only on LLM request spans, not tool spans
- Propagation ensures all spans get the session_id

### 5.2 Session Unification Logic

**Function**: `unify_sessions()` (lines 258-362)

**Purpose**: Merge new spans with existing session data, detecting continuations and deduplicating.

```python
def unify_sessions(
    new_spans: pd.DataFrame,
    existing_file: Path | None = None,
    output_file: Path | None = None,
) -> tuple[pd.DataFrame, MatchReport]:
    """Unify new spans with existing session data.

    1. Extract session IDs from new spans
    2. Load existing data (if any)
    3. Classify sessions as new or continued
    4. Merge dataframes
    5. Deduplicate by span_id (keep latest)
    6. Sort by start_time
    7. Generate match report
    """

    # Extract session IDs
    new_df = _extract_session_ids(new_spans)
    existing_df = read_sessions_file(existing_file) if existing_file else pd.DataFrame()

    # Get session sets
    existing_session_ids = set(existing_df["session_id"].unique())
    new_session_ids = set(new_df["session_id"].unique())

    # Classify
    continued_sessions = list(existing_session_ids & new_session_ids)
    new_sessions = list(new_session_ids - existing_session_ids)

    # Merge
    unified_df = pd.concat([existing_df, new_df], ignore_index=True)

    # Deduplicate by span_id (keep latest version)
    unified_df, duplicates_removed = _deduplicate_spans(unified_df)

    # Sort by time
    unified_df = _sort_by_time(unified_df)

    # Create report
    report = MatchReport(
        timestamp=datetime.now().isoformat(),
        new_sessions=sorted(new_sessions),
        continued_sessions=sorted(continued_sessions),
        total_spans_after=len(unified_df),
        duplicates_removed=duplicates_removed,
        spans_added=len(new_df)
    )

    return unified_df, report
```

**MatchReport Output** (lines 37-63):
```json
{
    "timestamp": "2025-01-07T15:30:00",
    "new_sessions": ["session_abc123", "session_def456"],
    "continued_sessions": ["session_xyz789"],
    "total_spans_before": 15000,
    "total_spans_after": 14950,
    "duplicates_removed": 50,
    "spans_added": 150,
    "summary": {
        "new_session_count": 2,
        "continued_session_count": 1
    }
}
```

**Saved to**: `~/.dal/data/state/match_report.json`

### 5.3 Deduplication Strategy

**Function**: `_deduplicate_spans()` (lines 167-186)

```python
def _deduplicate_spans(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Deduplicate spans by span_id, keeping the latest version."""
    original_count = len(df)

    # Keep last occurrence (newest data wins)
    df = df.drop_duplicates(subset=["span_id"], keep="last")

    duplicates_removed = original_count - len(df)
    return df, duplicates_removed
```

**Why duplicates occur**:
- Overlapping sync windows
- Re-fetching data after failures
- Incremental syncs may re-fetch recent spans

**Strategy**: Keep latest version (assume newer data is more complete)

---

## 6. Incremental Sync

The regular `dal sync` command performs incremental updates.

### 6.1 Command: `dal sync`

**Location**: `/Users/alexowen/Company/dev3/private-dev-agent-lens/dev_agent_lens/cli/main.py` (lines 166-655)

**Usage Examples**:
```bash
# Incremental sync (from last sync time)
dal sync

# Sync specific source
dal sync --source phoenix-local-alex

# Full sync (ignore state, last 7 days)
dal sync --full

# Sync specific date range
dal sync --start-date 2025-01-01 --end-date 2025-01-05

# Sync and push to Oxen remote
dal sync --push
```

**Incremental Logic** (lines 332-540):
```python
def sync_single(source_id, client, normalizer, ...):
    # Calculate time range
    end_time = datetime.now()

    if full:
        # Full sync: last N days
        start_time = end_time - timedelta(days=days)
    else:
        # Incremental: from last sync
        last_sync = state.get_last_sync(source_id)
        if last_sync:
            start_time = last_sync
        else:
            # First sync: last N days
            start_time = end_time - timedelta(days=days)

    # Fetch spans
    df = client.get_spans_dataframe(start_time=start_time, end_time=end_time)

    # Normalize
    normalized = normalizer(df)

    # Store raw
    store.append_spans(normalized, backend=source_id)

    # Unify with existing sessions
    unified_df, report = unify_sessions(normalized, existing_file=current_sessions)

    # Update state
    state.set_last_sync(source_id, datetime.now())
```

**State File**: `~/.dal/data/state/sync_state.json`
```json
{
    "phoenix-local-alex": {
        "last_sync": "2025-01-07T15:30:00"
    }
}
```

---

## 7. Parquet Conversion

After unification, sessions are converted to parquet format for efficient querying.

### 7.1 Parquet Generation

**Location**: Source-specific storage in `~/.dal/data/parquet/`

**Files**:
- `{source_name}_spans.parquet` - All spans in unified schema
- `{source_name}_sessions.parquet` - Session metadata (optional)

**Conversion Process**:
```python
# Load unified sessions JSONL
df = pd.read_json("sessions_current.jsonl", lines=True)

# Write to parquet
df.to_parquet(f"{source_name}_spans.parquet", engine="pyarrow")
```

**Benefits**:
1. **Columnar storage** - Efficient for analytical queries
2. **Compression** - 10-20x smaller than JSONL
3. **Fast filtering** - Query by date, session_id without full scan
4. **Schema enforcement** - Validates column types

**Example Sizes**:
```
Raw JSONL:    3.2 GB
Parquet:      210 MB  (93% reduction)
```

---

## Part II: Downstream Pipeline (Analysis & Export)

This section documents how parquet data is loaded, analyzed, and exported to markdown.

---

## 8. Data Flow Overview

### Data Sources
Data can come from two primary sources:

1. **Phoenix Local** - OpenTelemetry traces from local Phoenix server
2. **Parquet Files** - Pre-synced trace data stored in `~/.dal/data/<source_name>/spans.parquet`

### Loading Process

The CLI command triggers the pipeline:
```bash
dal chain-export --source phoenix-local-alex --index 0
```

**Flow** (from `dev_agent_lens/cli/main.py`, lines 5611-5622):
1. Load sessions from parquet file using `_load_sessions_from_parquet()`
2. Each session contains a list of spans (trace events)
3. Sessions are dictionaries with structure:
   ```python
   {
       "session_id": str,
       "spans": [
           {
               "span_id": str,
               "parent_id": str,
               "name": str,
               "start_time": datetime,
               "end_time": datetime,
               "input_value": str,
               "output_value": str,
               "raw_attributes_json": str,  # JSON with metadata
               "llm_model_name": str,
               "llm_token_count_total": int,
               ...
           }
       ]
   }
   ```

### Key Data Structures

**ConversationChain** (lines 41-88):
```python
@dataclass
class ConversationChain:
    chain_id: str                    # ID of first session in chain
    session_ids: list[str]           # Ordered list of sessions
    start_time: datetime | None
    end_time: datetime | None
    compaction_count: int            # Number of context compactions
    total_spans: int
    total_tokens: int
    claude_session_id: str | None    # UUID from Claude Code metadata
    user_hash: str | None            # User identifier
```

**MarkdownExportResult** (lines 1170-1177):
```python
@dataclass
class MarkdownExportResult:
    main_content: str                # Main markdown content
    subagents: list[SubagentExport]  # Separate subagent conversations
    tool_calls: list[ToolCallExport] # Tool call detail data
    metrics: dict[str, Any]          # Aggregated statistics
```

---

## 9. Session Handling

### Session Identification

Sessions are extracted using `extract_session_id_from_span()` from `dev_agent_lens/core/session.py`.

**Extraction Priority**:
1. **Claude session UUID** from metadata (definitive): `"user_{hash}_account_{uuid}_session_{uuid}"`
2. **trace_id** field (fallback for spans without metadata)

**Session Extraction** (from `dev_agent_lens/core/unify.py`, lines 66-130):
- Session metadata is only present on LLM request spans (not tool spans)
- The `_extract_session_ids()` function propagates session IDs to all spans with the same `trace_id`
- This handles Claude Code's pattern where a single conversation generates many trace_ids but shares one session_id

### Chain Building

**Primary Method: UUID-based Linking** (lines 327-438):

```python
def build_conversation_chains(sessions, max_gap_seconds=60):
    # 1. Extract Claude session UUIDs from spans
    for session in sessions:
        claude_uuid, user_hash = extract_session_claude_id(session)
        if claude_uuid:
            uuid_to_sessions[claude_uuid].append(session_id)

    # 2. Group sessions by shared UUID
    # All sessions with same claude_uuid belong to same chain
    for claude_uuid, session_ids in uuid_to_sessions.items():
        # Sort sessions by start time
        session_times.sort(key=lambda x: x[1])

        # Create chain with ordered sessions
        chain = ConversationChain(
            chain_id=sorted_session_ids[0],
            session_ids=sorted_session_ids,
            ...
        )
```

**Fallback Method: Temporal Linking** (lines 440-516):

For sessions without UUIDs, links are built using:
- **Compaction markers** in input (line 215-218): `has_compaction_marker()`
- **Temporal proximity**: Sessions within 60 seconds (MAX_SESSION_GAP_SECONDS)

```python
def build_session_links(sessions, max_gap_seconds=60):
    # Only link if:
    # 1. Current session has compaction continuation marker
    # 2. Starts within 60 seconds of previous session ending

    if curr_id in compaction_sessions and gap <= max_gap:
        links[curr_id] = prev_id
```

### Session Ordering

Within each chain, sessions are ordered chronologically by start time (lines 373-382):
```python
session_times.sort(key=lambda x: x[1] or datetime.min)
sorted_session_ids = [sid for sid, _ in session_times]
```

---

## 10. Span Processing

### Main Thread Span Detection

**Function**: `_is_main_thread_span()` (lines 645-710)

**Purpose**: Filter spans to include only user-visible conversation content, excluding internal routing/classification.

**Inclusion Criteria** (lines 665-673):
Must match one of these name patterns:
- `"Claude_Code_Internal_Prompt_"` - User input spans
- `"Claude_Code_Final_Output_"` - Assistant output spans
- `"litellm_request"` - LLM API calls
- `"raw_gen_ai_request"` - Direct API calls

**Exclusion Patterns**:

1. **Ancillary Input Patterns** (lines 679-687):
   - `<policy_spec>` - Command prefix extraction
   - `"Claude Code Code Bash command prefix detection"` - Safety checks

2. **Ancillary Output Patterns** (lines 694-702):
   - `"isNewTopic"` - Topic detection responses
   - `"is_displaying_contents"` - Content display checks
   - Very short outputs like `"#"` (quota check) or `"{"` (delegation signal)

**Example**:
```python
# EXCLUDED - topic detection output
output = '{"isNewTopic": false}'  # Returns False

# INCLUDED - actual conversation
output = 'I analyzed the code and found...'  # Returns True
```

### Thread Classification

From `dev_agent_lens/analysis/threads.py`, spans are classified into three thread types:

1. **MAIN_THREAD** (lines 683-717):
   - Tool spans: `Claude_Code_Tool_*`
   - Internal prompts: `Claude_Code_Internal_Prompt_*`
   - LLM requests: `litellm_request`, `raw_gen_ai_request`

2. **ANCILLARY** (lines 638-658):
   - Pattern-based detection (input/output patterns)
   - Examples: quota checks, status lines, topic detection

3. **SUB_AGENT** (lines 628-636):
   - Task tool invocations: `Claude_Code_Tool_Task`

**Note**: Classification is **pattern-first, not model-first**. Haiku may be the main conversation model, so we don't assume Haiku = ancillary.

---

## 11. Message Extraction

### Input/Output Value Extraction

**Input Extraction**: `_extract_input_value()` (lines 193-212)
```python
def _extract_input_value(span):
    # 1. Try direct field
    input_val = span.get("input_value")
    if input_val:
        return input_val

    # 2. Try raw_attributes_json nested structure
    attrs = json.loads(span.get("raw_attributes_json"))
    return attrs.get("attributes", {}).get("input", {}).get("value", "")
```

**Output Extraction**: `_extract_output_value()` (lines 599-618) - Same pattern as input

### Message Content Parsing

**Function**: `_parse_message_content()` (lines 818-869)

**Purpose**: Parse JSON/Python literal message arrays into structured format

**Input Formats Handled**:
1. JSON with double quotes: `[{"type": "text", "text": "Hello"}]`
2. Python literals with single quotes: `[{'type': 'text', 'text': 'Hello'}]`

**Content Types**:

1. **Text Content** (lines 849-850):
   ```python
   {"type": "text", "text": "User message or assistant response"}
   ```

2. **Tool Use** (lines 851-860):
   ```python
   {
       "type": "tool_use",
       "tool": "Read",
       "input": {"file_path": "/path/to/file"},
       "id": "toolu_abc123"
   }
   ```

3. **Tool Result** (lines 861-865):
   ```python
   {
       "type": "tool_result",
       "content": "Result of tool execution"
   }
   ```

**Parsing Strategy** (lines 828-841):
```python
# Try JSON first
try:
    parsed = json.loads(content)
except json.JSONDecodeError:
    # Fallback to Python literal_eval (handles single quotes)
    parsed = ast.literal_eval(content)

# If parsing fails, return as plain text
if not parsed:
    return [{"type": "text", "text": content}]
```

---

## 12. Compaction Handling

### Compaction Markers

**Task Marker** (line 194 in threads.py):
```python
COMPACTION_TASK_MARKER = "Your task is to create a detailed summary"
```
- Appears when Claude generates a conversation summary before context exhaustion

**Continuation Marker** (lines 197-198 in threads.py):
```python
COMPACTION_CONTINUATION_MARKER = "This session is being continued from a previous conversation"
COMPACTION_SUMMARY_MARKER = "The conversation is summarized below:"
```
- Appears in the new session's first input with embedded summary

### Detection Logic

**Function**: `has_compaction_marker()` (lines 215-218)
```python
def has_compaction_marker(span):
    input_val = _extract_input_value(span)
    return COMPACTION_CONTINUATION_MARKER in input_val
```

**Function**: `is_compaction_task()` (lines 221-224)
```python
def is_compaction_task(span):
    input_val = _extract_input_value(span)
    return COMPACTION_TASK_MARKER in input_val
```

### Compaction Marker Display

**When to Show** (lines 1465-1477):
Compaction markers are added **lazily** - only when actual content appears in a continuation session:

```python
def _maybe_add_compaction_marker():
    """Add compaction marker before first content in a continuation session."""
    if session_idx > 0 and not compaction_marker_added:
        compaction_count += 1
        lines.append("---")
        lines.append(f"### 🔄 Compaction #{compaction_count}")
        lines.append("*Session continued after context window limit*")
        lines.append("---")
        compaction_marker_added = True
```

**Marker Format**:
```markdown
---
### 🔄 Compaction #1
*Session continued after context window limit*
---

> **Previous Context Summary**
>
> The conversation is summarized below:
> [Full summary text preserved - no truncation]
```

### Continuation Session Processing

**Special Handling** (lines 1494-1573):

When a span has `COMPACTION_CONTINUATION_MARKER`:

1. **Extract and Display Summary** (lines 1497-1508):
   ```python
   if "The conversation is summarized below:" in input_val:
       summary_text = input_val[summary_start:]
       lines.append("> **Previous Context Summary**")
       for line in summary_text.split("\n"):
           lines.append(f"> {line}")
   ```

2. **Process Assistant Output** (lines 1513-1572):
   - Clear input messages (don't show compaction marker as user input)
   - Parse and display assistant's continuation message
   - Show any tool calls from the continuation

3. **Skip Normal Processing** (line 1573):
   ```python
   continue  # Skip normal input/output processing
   ```

---

## 13. Filtering and Deduplication

### Message Deduplication

**Mechanism** (lines 1426-1432):
```python
seen_messages: set[str] = set()

def _message_key(text: str) -> str:
    """Generate a key for deduplication (first 500 chars, normalized)."""
    return text[:500].strip().lower()
```

**Applied During Export** (lines 1603-1607, 1621-1625):
```python
msg_key = _message_key(text)
if msg_key in seen_messages:
    continue  # Skip duplicate
seen_messages.add(msg_key)
```

**Why**: Messages can appear multiple times across compaction boundaries

### User Message Skip Conditions

**Function**: `_has_meaningful_user_input()` (lines 713-748)

**Skipped User Messages** (lines 1586-1601):

1. **Empty or too short** (lines 1588-1589):
   ```python
   if not text or len(text) <= 10:
       continue
   ```

2. **System messages** (lines 1591-1592):
   ```python
   if text.startswith("<system"):
       continue
   ```

3. **Tool result echoes** (lines 1594-1595):
   ```python
   if text.startswith("Command:") and "\nOutput:" in text:
       continue
   ```

4. **JSON tool results** (lines 1597-1598):
   ```python
   if text.startswith("{") and text.endswith("}"):
       continue
   ```

5. **Cache warmup** (lines 1600-1601):
   ```python
   if text == "Warmup" or text.startswith('"Warmup"'):
       continue
   ```

6. **Continuation markers** (lines 726-727):
   ```python
   if COMPACTION_CONTINUATION_MARKER in input_val:
       return False
   ```

### Mid-Stream Detection

**Function**: `_detect_mid_stream_start()` (lines 751-815)

**Purpose**: Detect if chain starts with assistant output before user input (data collection started mid-conversation)

**Detection Logic**:
1. Find first main thread span with output (assistant message)
2. Find first main thread span with meaningful user input
3. If output appears before user input (by timestamp), chain started mid-stream

**Warning Added** (lines 1420-1424):
```markdown
> **Note:** This conversation continues from earlier context not captured in this export.
> The original user request that prompted this conversation is not available.
```

---

## 14. Export Functions

### `export_chain_to_markdown()`

**Location**: Lines 1278-1742

**Signature**:
```python
def export_chain_to_markdown(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
    include_tool_calls: bool = True,
    include_metadata: bool = True,
    max_message_length: int = 10000,
    output_basename: str | None = None,
    scaffolded: bool = True,
) -> MarkdownExportResult
```

**Parameters**:
- `chain`: The conversation chain to export
- `sessions`: All session dictionaries for lookup
- `include_tool_calls`: Whether to include tool details (default: True)
- `include_metadata`: Show session stats header (default: True)
- `max_message_length`: Truncation threshold for messages
- `output_basename`: Base filename for linking subagent files
- `scaffolded`: If True, tool calls summarized inline with detail files; if False, expanded inline

**Logic Flow**:

1. **Build Span Lookups** (lines 1317-1348):
   ```python
   # Map tool_use_id -> task span for subagent extraction
   task_spans_by_id: dict[str, dict[str, Any]] = {}

   # Map parent_id -> child spans for hierarchy
   all_spans_by_parent: dict[str, list[dict[str, Any]]] = {}
   ```

2. **Gather Metrics** (lines 1354-1395):
   - Count main thread vs ancillary turns
   - Track model usage
   - Sum tokens (prompt + completion)
   ```python
   metrics = {
       "duration_seconds": int(chain.duration_minutes * 60),
       "total_turns": main_thread_turns + ancillary_turns,
       "models_used": model_counts,
       "tokens": {
           "prompt": total_tokens_prompt,
           "completion": total_tokens_completion,
       },
   }
   ```

3. **Write Header** (lines 1397-1413):
   ```markdown
   # Conversation: abc12345...

   ## Metadata
   - **Sessions**: 3
   - **Compactions**: 2
   - **Duration**: 15.3 minutes
   - **Main thread turns**: 12
   - **Ancillary turns**: 45
   - **Models**: claude-sonnet-4: 10, claude-haiku-3-5: 47
   ```

4. **Process Sessions** (lines 1456-1730):
   - For each session in chain order:
     - Maybe add compaction marker (lazy)
     - Process main thread spans chronologically
     - Extract user messages, assistant messages, tool calls
     - Handle subagents separately
     - Deduplicate messages

5. **Format Tool Calls** (two modes):

   **Scaffolded Mode** (lines 1542-1564, 1684-1716):
   ```markdown
   > 🔧 **#1 Read**: Read chains.py → [details](./tool_calls/001_abc12345.md)
   > 🔧 **#2 Bash**: Run pytest tests
   >   → `PASSED test_chains.py::test_build_chains`
   ```

   **Non-Scaffolded Mode** (lines 1566-1571, 1718-1729):
   ```markdown
   **🔧 Tool: Read**
   > File: `chains.py`
   > **Result:**
   > [Full file content...]
   ```

6. **Return Result** (lines 1737-1742):
   ```python
   return MarkdownExportResult(
       main_content="\n".join(lines),
       subagents=subagents,  # Separate SubagentExport objects
       tool_calls=tool_calls,  # Separate ToolCallExport objects
       metrics=metrics,
   )
   ```

### Scaffolded Mode Details

**What is "Scaffolded"?** (default: True)

Scaffolded mode produces clean, scannable conversation flow with details in separate files:

**Main File**:
- User messages and assistant responses
- Tool calls summarized in one line
- Links to detail files for large results

**Separate Files**:
- `tool_calls/{number}_{id}.md` - Full tool input/output for large results
- `{basename}_subagent_{id}.md` - Complete subagent conversations

**Small Result Threshold**: 500 characters (line 1049)
- Results ≤ 500 chars: Inlined
- Results > 500 chars: Linked to detail file

**Benefits**:
- Main conversation remains readable
- LLMs can focus on flow without overwhelming detail
- Large outputs don't clutter the narrative

### `export_chain_to_file()`

**Location**: Lines 1745-1811

**Signature**:
```python
def export_chain_to_file(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
    output_path: str,
    **kwargs: Any,
) -> list[str]
```

**Purpose**: Write markdown to file(s), handling subagents and tool calls

**Logic**:

1. **Prepare Basename** (lines 1769-1774):
   ```python
   output_file = Path(output_path)
   output_dir = output_file.parent
   basename = output_file.stem  # e.g., "chain_abc123"
   kwargs["output_basename"] = basename
   ```

2. **Export to Markdown** (line 1776):
   ```python
   result = export_chain_to_markdown(chain, sessions, **kwargs)
   ```

3. **Write Main File** (lines 1778-1780):
   ```python
   output_file.write_text(result.main_content, encoding="utf-8")
   written_files = [str(output_file)]
   ```

4. **Write Subagent Files** (lines 1782-1792):
   ```python
   for subagent in result.subagents:
       subagent_filename = f"{basename}_subagent_{subagent.tool_use_id}.md"
       subagent_path = output_dir / subagent_filename
       subagent_content = generate_subagent_markdown(subagent, chain.chain_id)
       subagent_path.write_text(subagent_content, encoding="utf-8")
       written_files.append(str(subagent_path))
   ```

5. **Write Tool Call Detail Files** (lines 1794-1809):
   ```python
   if result.tool_calls:
       tool_calls_dir = output_dir / "tool_calls"
       tool_calls_dir.mkdir(exist_ok=True)

       for tool_call in result.tool_calls:
           # Only write files for large results (small ones inlined)
           if not tool_call.is_small:
               tool_filename = f"{tool_call.tool_number:03d}_{tool_call.tool_use_id[:8]}.md"
               tool_path = tool_calls_dir / tool_filename
               tool_content = generate_tool_call_markdown(tool_call, chain.chain_id)
               tool_path.write_text(tool_content, encoding="utf-8")
               written_files.append(str(tool_path))
   ```

6. **Return All Paths** (line 1811):
   ```python
   return written_files  # [main, subagent1, subagent2, ..., tool1, tool2, ...]
   ```

### CLI Integration

**Command**: `dal chain-export` (lines 5596-5739 in `cli/main.py`)

**Usage**:
```bash
# Export longest chain as markdown
dal chain-export --source phoenix-local-alex

# Export specific chain by index
dal chain-export --source phoenix-local-alex --index 0

# Export as JSONL (recommended for large chains)
dal chain-export --source phoenix-local-alex --format jsonl -o chain.jsonl

# Export with custom filename
dal chain-export --source phoenix-local-alex --chain-id abc123 -o conversation.md
```

**Flow**:
1. Load sessions from parquet (line 5612)
2. Build conversation chains (line 5626)
3. Filter to multi-session chains (line 5629)
4. Find target chain by ID or index (lines 5639-5663)
5. Export based on format:
   - **markdown**: Call `export_chain_to_file()` (lines 5706-5731)
   - **jsonl**: Call `export_chain_to_jsonl()` (lines 5675-5686)
   - **json**: Call `export_chain_to_json()` (lines 5691-5700)

---

## Summary

The markdown export pipeline is a sophisticated end-to-end system for converting raw OpenTelemetry traces into readable conversation documentation.

**Complete Pipeline Flow**:

1. **Data Sources** - Phoenix local server or Arize cloud platform provide raw trace data
2. **Historical Sync** - `dal sync-historical` performs batched downloads with resume capability
3. **Raw Storage** - JSONL files preserve exact backend data in `~/.dal/data/raw/`
4. **Schema Normalization** - Convert backend-specific formats to unified schema
5. **Session Unification** - Extract session IDs, merge with existing data, deduplicate
6. **Parquet Storage** - Efficient columnar format in `~/.dal/data/parquet/`
7. **Chain Building** - Link sessions using UUID or temporal proximity
8. **Markdown Export** - Reconstruct conversations with compaction handling

**Key Design Principles**:

1. **Resumability**: All sync operations save checkpoints for interruption recovery
2. **Deduplication**: Spans and messages deduplicated by ID and content
3. **Normalization**: Unified schema abstracts backend differences
4. **Reconstruction**: UUID-based linking reassembles multi-session conversations
5. **Filtering**: Pattern-based detection excludes ancillary/internal spans
6. **Scaffolding**: Separate files for tool details keep main conversation readable
7. **Preservation**: Full context summaries maintained from compactions

The result is LLM-friendly documentation that accurately represents multi-session agent conversations while remaining readable and navigable.
