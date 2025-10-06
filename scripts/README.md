# Dev-Agent-Lens Scripts

Utility scripts for managing and analyzing Dev-Agent-Lens data.

## Export Arize Data

Export trace data from Arize AX platform for analysis and reporting.

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

### Getting Your Arize Credentials

1. **ARIZE_API_KEY**:
   - Log in to [Arize Dashboard](https://app.arize.com)
   - Go to Settings → API Keys
   - Generate a new API key or copy an existing one

2. **ARIZE_SPACE_KEY**:
   - In Arize Dashboard, click on your space name in the top navigation
   - Copy the Space ID (this is your ARIZE_SPACE_KEY)

3. **ARIZE_MODEL_ID**:
   - This should match the model ID configured in your `litellm_config_arize.yaml`
   - Default is `dev-agent-lens` (set in `.env.example`)

### Usage Examples

#### Export data for Oct 1, 2025 (default - JSONL format)
```bash
uv run python scripts/export_arize_data.py
```

#### Export all available data
```bash
uv run python scripts/export_arize_data.py --all
```

#### Export data for a custom date range
```bash
uv run python scripts/export_arize_data.py --start-date 2025-10-01 --end-date 2025-10-06
```

#### Export to CSV format
```bash
uv run python scripts/export_arize_data.py --output traces.csv --format csv
```

#### Export as Parquet format
```bash
uv run python scripts/export_arize_data.py --output traces.parquet --format parquet
```

#### Combine options
```bash
uv run python scripts/export_arize_data.py \
  --start-date 2025-10-01 \
  --end-date 2025-10-31 \
  --output october_traces.jsonl \
  --format jsonl
```

### Output

The script exports trace data to JSONL, CSV, or Parquet format containing:
- Span IDs and trace information
- Timestamps (start/end times)
- Input/output data
- Token counts and costs
- Model information
- Custom attributes and metadata

### Troubleshooting

**Error: ARIZE_API_KEY environment variable is not set**
- Ensure you've set the `ARIZE_API_KEY` environment variable or added it to `.env`

**Error: No trace data found**
- Verify that Dev-Agent-Lens is running and sending traces to Arize
- Check that the `ARIZE_MODEL_ID` matches what's configured in LiteLLM
- Ensure the date range contains actual trace data
- Verify you have access to the Arize space

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
