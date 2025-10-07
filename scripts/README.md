# Dev-Agent-Lens Scripts

Utility scripts for managing and analyzing Dev-Agent-Lens data.

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

The unified `export_traces.py` script auto-detects which backend to use based on your `.env` configuration.

#### Export data from today (default - JSONL format)
```bash
uv run export_traces.py
```

#### Export all available data
```bash
uv run export_traces.py --all
```

#### Export data for a custom date range
```bash
uv run export_traces.py --start-date 2025-10-01 --end-date 2025-10-06
```

#### Export to CSV format
```bash
uv run export_traces.py --output traces.csv --format csv
```

#### Export as Parquet format
```bash
uv run export_traces.py --output traces.parquet --format parquet
```

#### Force specific backend (override auto-detection)
```bash
uv run export_traces.py --backend phoenix
uv run export_traces.py --backend arize
```

#### Combine options
```bash
uv run export_traces.py \
  --start-date 2025-10-01 \
  --end-date 2025-10-31 \
  --output october_traces.jsonl \
  --format jsonl
```

### Output

The script automatically classifies and splits trace data into separate files:

**Main Files:**
- `{filename}.jsonl` - Main dataset (user-agent conversations, LLM requests)
- `{filename}_tools.jsonl` - Tool calls and tool results
- `{filename}_ancillary.jsonl` - Ancillary data (safety checks, title generation, incomplete requests)

**For Arize exports only:**
- `{filename}_raw.jsonl` - Raw unprocessed data (cached to avoid re-downloading)

**Classification Types:**
- `main` - Primary user-agent conversation data
- `tools` - Tool executions (Read, Edit, Bash, etc.)
- `safety` - Bash command safety policy checks
- `summarization` - Title generation prompts
- `incomplete` - Failed or interrupted LLM requests

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
df = pd.read_json('arize_traces.jsonl', lines=True)

# Or if you exported as CSV
# df = pd.read_csv('arize_traces.csv')

# Analyze token usage
print(df['token_count'].sum())

# Find most expensive traces
print(df.nlargest(10, 'cost'))

# Group by model
print(df.groupby('model_name')['token_count'].sum())
```

## Adding More Scripts

To add new utility scripts to this folder:

1. Create your script in the `scripts/` directory
2. Add any new dependencies to `requirements.txt`
3. Update this README with usage documentation
4. Make the script executable: `chmod +x scripts/your_script.py`
