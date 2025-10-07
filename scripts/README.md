# Dev-Agent-Lens Scripts

Utility scripts for managing and analyzing Dev-Agent-Lens data.

## Project Structure

```
scripts/
├── main.py                  # Unified CLI entry point (use this!)
├── src/                     # All implementation code
│   ├── export_traces.py     # Export orchestrator
│   ├── export_arize.py      # Arize backend
│   ├── export_phoenix.py    # Phoenix backend
│   ├── analyze_sessions.py  # Session analysis
│   ├── reconstruct_sessions.py  # Session reconstruction
│   └── compare_spans.py     # Span comparison and duplication analysis
├── arize/                   # Arize export output (auto-created)
└── phoenix/                 # Phoenix export output (auto-created)
```

## Quick Start

```bash
# Install dependencies
cd scripts
uv sync

# Export traces (auto-detects backend from .env)
uv run main.py export

# Analyze session reconstruction
uv run main.py analyze <trace_file>

# Reconstruct sessions
uv run main.py reconstruct <trace_file>

# Compare spans to identify duplication patterns
uv run main.py compare <session_file>
```

## Compare Spans (Duplication Analysis)

Analyze spans within reconstructed sessions to identify content duplication and accumulation patterns. This is useful for understanding how conversational AI systems accumulate context across turns.

### Usage

```bash
# Compare all sessions in a file
uv run main.py compare phoenix/phoenix_sessions.jsonl

# Compare a specific session
uv run main.py compare phoenix/phoenix_sessions.jsonl --session 1
```

### What It Checks

The compare script analyzes whether content from earlier spans appears in later spans by checking:

- **Input messages** (`attributes.llm.input_messages`)
- **Input values** (`attributes.input.value`)
- **Output messages** (`attributes.llm.output_messages`)
- **Output values** (`attributes.output.value`)

It uses **fuzzy/substring matching** on actual text content (not metadata) to determine if earlier content is contained within later spans.

### Output

For each session, the script shows:

1. **Containment Analysis**: Whether earlier spans are fully contained in later spans
   - ✅ FULLY CONTAINED: 100% of content from earlier span found in later span
   - ❌ PARTIAL/NOT CONTAINED: Some content missing

2. **Location Details**: Exact positions where content appears in later spans
   - Shows the character position in the accumulated content
   - Displays context around each match

3. **Span-by-Span Comparison**: Message-level comparison between consecutive spans
   - Duplicated message count
   - New message count
   - Overlap percentage

### Example Output

```
Span 1 → Span 3: ✅ FULLY CONTAINED
  Content chunks in Span 1: 6
  Found in Span 3: 6 (100.0%)

  ✅ Contained chunks and their locations:
    [input_msg] at position 0
      Chunk: <system-reminder> This is a reminder...
      Context: ...<system-reminder> This is a reminder that your todo list...

    [input_msg] at position 363
      Chunk: can you tell me what this repo does
      Context: ...tion this message to the user. </system-reminder> can you tell me...
```

This helps verify the hypothesis that **later spans contain the complete history of earlier spans** in conversational AI systems.

## Export Trace Data (Arize or Phoenix)

Export trace data from either Arize AX (cloud) or Phoenix (local) for analysis and reporting. The script auto-detects which backend to use based on environment variables.

### Prerequisites

1. **Install dependencies**:
   ```bash
   cd scripts
   uv sync
   ```

2. **Set environment variables**:

   Copy the example file and add your credentials:
   ```bash
   cd scripts
   cp .env.example .env
   # Edit .env and add your actual Arize credentials
   ```

   Alternatively, export them in your shell:
   ```bash
   export ARIZE_API_KEY='your-arize-api-key'
   export ARIZE_SPACE_KEY='your-arize-space-key'
   export ARIZE_MODEL_ID='dev-agent-lens'  # Optional, defaults to 'dev-agent-lens'
   ```

### Configuration

The export script auto-detects whether to use Arize or Phoenix based on environment variables in `.env`:

#### Option 1: Use Phoenix (Local)

Uncomment these in `.env`:
```bash
PHOENIX_URL=http://localhost:6006
PHOENIX_PROJECT=claude-code-myproject
```

Make sure Phoenix is running locally. If not, start it:
```bash
# From the project root directory
docker compose --profile phoenix up -d
```

#### Option 2: Use Arize (Cloud)

Uncomment these in `.env`:
```bash
ARIZE_API_KEY=your-arize-api-key-here
ARIZE_SPACE_KEY=your-arize-space-key-here
ARIZE_MODEL_ID=litellm
```

**Getting Arize Credentials:**
1. **ARIZE_API_KEY**: Log in to [Arize Dashboard](https://app.arize.com) → Settings → API Keys
2. **ARIZE_SPACE_KEY**: Click on your space name → Copy the Space ID
3. **ARIZE_MODEL_ID**: Should match your `litellm_config_arize.yaml` (default: `litellm`)

### Usage Examples

The export script auto-detects which backend to use based on your `.env` configuration and automatically saves data to the appropriate folder (`arize/` or `phoenix/`).

```bash
# Export data from today (default - JSONL format)
uv run main.py export

# Export all available data
uv run main.py export --all

# Export data for a custom date range
uv run main.py export --start-date 2025-10-01 --end-date 2025-10-06

# Export to CSV format (saves to arize/arize_traces.csv or phoenix/phoenix_traces.csv)
uv run main.py export --format csv

# Export as Parquet format
uv run main.py export --format parquet

# Force specific backend (override auto-detection)
uv run main.py export --backend phoenix
uv run main.py export --backend arize

# Custom output path (overrides default arize/ or phoenix/ folder)
uv run main.py export --output custom_folder/traces.jsonl
```

### Output

The script automatically classifies and splits trace data into separate files, saving them to `arize/` or `phoenix/` folders:

**Output Files:**
- `{folder}/{filename}.jsonl` - Main dataset (Sonnet conversations only)
- `{folder}/{filename}_tools.jsonl` - Tool calls and tool results
- `{folder}/{filename}_haiku_holdover.jsonl` - Haiku model calls (for further categorization)
- `{folder}/{filename}_litellm_overhead.jsonl` - LiteLLM system overhead/test calls
- `{folder}/{filename}_ancillary.jsonl` - Ancillary data (quota, safety, titles, incomplete)

**For Arize exports only:**
- `arize/{filename}_raw.jsonl` - Raw unprocessed data (cached to avoid re-downloading)

**Classification Types:**
- `main` - Primary Sonnet user-agent conversation data (claude-sonnet-4-5)
- `tools` - Tool executions (Read, Edit, Bash, etc.)
- `haiku_holdover` - All Haiku model calls (needs further categorization)
- `litellm_system_overhead` - LiteLLM test/health check calls
- `quota` - Quota check calls (in ancillary)
- `is_new_topic` - Topic detection calls (in ancillary)
- `safety` - Bash command safety policy checks (in ancillary)
- `summarization` - Title generation prompts (in ancillary)
- `incomplete` - Failed or interrupted LLM requests (in ancillary)

**Data includes:**
- Span IDs and trace information
- Timestamps (start/end times)
- Input/output data
- Token counts and costs (Arize only)
- Model information
- Custom attributes and metadata
- Classification column (for ancillary data)

### Troubleshooting

**Error: No observability backend configured**
- Ensure you've uncommented either Phoenix or Arize credentials in `.env`
- Check `.env.example` for the required variables

**Phoenix-specific issues:**

**Error: Connection refused / Failed to connect**
- Ensure Phoenix is running: `docker compose --profile phoenix up -d` (from project root)
- Verify Phoenix URL is correct (default: `http://localhost:6006`)
- Check that the container is running: `docker ps | grep phoenix`

**Error: No trace data found**
- Verify the project name matches: `PHOENIX_PROJECT=claude-code-myproject`
- Check Phoenix UI at http://localhost:6006 to see if data exists
- Ensure traces are being sent to Phoenix (check instrumentation setup)

**Arize-specific issues:**

**Error: ARIZE_API_KEY not set**
- Add your Arize API key to `.env`

**Error: No trace data found**
- Verify `ARIZE_MODEL_ID` matches what's in `litellm_config_arize.yaml`
- Check date range contains data
- Verify you have access to the Arize space

**General:**

**Error: Missing required package**
- Run `cd scripts && uv sync` to install dependencies

### Data Analysis

Once exported, you can analyze the trace data using pandas:

```python
import pandas as pd

# Load the exported data (JSONL)
df = pd.read_json('arize/arize_traces.jsonl', lines=True)
# Or for Phoenix
# df = pd.read_json('phoenix/phoenix_traces.jsonl', lines=True)

# Or if you exported as CSV
# df = pd.read_csv('arize/arize_traces.csv')

# Analyze token usage (Arize only)
print(df['token_count'].sum())

# Find most expensive traces
print(df.nlargest(10, 'cost'))

# Group by model
print(df.groupby('attributes.llm.model_name')['token_count'].sum())

# Load tools data
tools_df = pd.read_json('arize/arize_traces_tools.jsonl', lines=True)
print(f"Tool calls: {len(tools_df)}")
```

## Adding More Scripts

To add new utility scripts to this folder:

1. Create your script in the `scripts/` directory
2. Add any new dependencies to `requirements.txt`
3. Update this README with usage documentation
4. Make the script executable: `chmod +x scripts/your_script.py`
