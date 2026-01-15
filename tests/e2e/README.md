# End-to-End Testing Guide

This directory contains end-to-end tests for the complete claude-lens proxy pipeline:

```
Claude Code ──> claude-lens proxy ──> LiteLLM ──> Phoenix ──> DAL sync/export ──> Validation
```

## Overview

The E2E test suite validates that traces flow correctly through the entire system:

1. **Claude Code Conversation**: User interacts with Claude Code through the claude-lens proxy
2. **Phoenix Ingestion**: LiteLLM sends traces to Phoenix for storage
3. **DAL Sync**: Traces are synced from Phoenix to local parquet files
4. **Markdown Export**: Sessions are exported to readable markdown
5. **Validation**: Exported content is validated against raw trace data

## Prerequisites

### 1. Phoenix Running

Phoenix must be running to collect traces:

```bash
cd ~/Company/dev3/private-dev-agent-lens
docker compose --profile phoenix up -d
```

Verify Phoenix is accessible:
```bash
curl http://localhost:6006/arize_phoenix_version
```

### 2. LiteLLM Proxy Running

The proxy routes Claude API calls through Phoenix for observability:

```bash
# Check if proxy is running
curl http://localhost:4000/health

# If not running, start it:
docker compose up -d
```

### 3. Claude-Lens Script

The `claude-lens` wrapper script configures Claude Code to use the proxy:

```bash
# Should exist at:
~/Company/dev3/private-dev-agent-lens/claude-lens

# Make executable if needed:
chmod +x ~/Company/dev3/private-dev-agent-lens/claude-lens
```

### 4. Environment Setup

Install dependencies:

```bash
uv sync
```

Verify DAL CLI works:

```bash
uv run dal --version
```

## Running Tests

### Infrastructure Check

Run a quick check to verify all components are ready:

```bash
# Standalone check
uv run python tests/e2e/test_proxy_pipeline.py

# Or via pytest
uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyInfrastructure -v
```

This checks:
- Phoenix is running and accessible
- LiteLLM proxy is healthy
- claude-lens script exists
- DAL CLI is available

### Smoke Tests

Run automated tests that don't require manual interaction:

```bash
uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxySmokeTest -v
```

These tests:
- Query Phoenix for recent sessions
- Test the sync mechanism with Phoenix
- Don't require a specific test session

### Full Validation Test (Manual Setup Required)

The full validation test requires a session created through the proxy:

#### Step 1: Run a Test Conversation

Start Claude Code through the proxy:

```bash
~/Company/dev3/private-dev-agent-lens/claude-lens
```

In the Claude Code session, run a simple test conversation:

```
> What is 2+2? Please show your work.
```

Let Claude respond, then exit Claude Code.

#### Step 2: Get the Session ID

1. Open Phoenix UI: http://localhost:6006
2. Navigate to **Traces**
3. Find your recent conversation
4. Copy the **Session ID** (format: `abc123def456...`)

#### Step 3: Run Validation

```bash
TEST_SESSION_ID=<your-session-id> \
  uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyPipeline::test_validate_session_export -v
```

Example:
```bash
TEST_SESSION_ID=3640c6d77574ea64f556583219487860 \
  uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyPipeline::test_validate_session_export -v
```

The test will:
1. Locate the session in Phoenix parquet data
2. Export the session to markdown
3. Validate that all expected content appears in the export:
   - User messages
   - Assistant responses
   - Tool calls
   - Tool results
   - Subagents (if any)

## Test Output

### Success

```
==============================================================================
Validation Report: 3640c6d77574ea64f556583219487860
==============================================================================

✅ User messages: 1/1 found
✅ Assistant messages: 2/2 found
✅ Tool calls: 3/3 found
✅ Tool results: 3/3 found (Inline: 2, Linked: 1)
✅ Subagents: 0/0 found

==============================================================================
✅ RESULT: PASS
==============================================================================

✓ Session validation PASSED
```

### Failure Example

If content is missing from the export:

```
==============================================================================
Validation Report: 3640c6d77574ea64f556583219487860
==============================================================================

✅ User messages: 1/1 found
❌ Assistant messages: 1/2 found
   Missing: Here is my detailed analysis...
✅ Tool calls: 3/3 found
❌ Tool results: 2/3 found
   Missing: 1

==============================================================================
❌ RESULT: FAIL
==============================================================================
```

## Troubleshooting

### Phoenix Not Running

```
AssertionError: Phoenix not running: Phoenix not reachable (connection refused)
```

**Solution**: Start Phoenix:
```bash
docker compose --profile phoenix up -d
```

### Proxy Not Running

```
SKIPPED: Proxy not running: Proxy not reachable (this is OK for validation-only tests)
```

**Note**: This is a warning, not an error. The validation test can run without the proxy if you already have session data. However, to create new test sessions, you'll need the proxy running.

**Solution**: Start the proxy:
```bash
docker compose up -d
```

### No Parquet Files Found

```
AssertionError: No phoenix-local parquet files found in ~/.dal/data/parquet
```

**Cause**: No data has been synced from Phoenix yet.

**Solution**:
1. Make sure you ran a conversation through the proxy
2. Wait a few seconds for Phoenix to ingest the data
3. Sync data manually:
   ```bash
   uv run dal sync --source phoenix-local --start 2024-01-01
   ```

### Session Not Found

```
ValueError: No spans found for session 3640c6d77574ea64f556583219487860
```

**Cause**: The session ID doesn't exist in the parquet data.

**Solutions**:
1. Double-check the session ID from Phoenix UI
2. Make sure data was synced: `uv run dal sync --source phoenix-local`
3. Verify the session exists in Phoenix UI first

### DAL CLI Not Available

```
AssertionError: DAL CLI not available
```

**Solution**: Install dependencies:
```bash
uv sync
```

## Advanced Usage

### Testing with Specific Models

Create a test conversation using a specific model by modifying your prompt:

```bash
# In claude-lens session, specify model preference
> "Using claude-3-haiku-20240307, what is 2+2?"
```

### Testing Subagents

To test subagent validation, create a conversation that spawns a subagent:

```bash
> "Search for all Python files in the current directory and analyze their imports"
```

This will likely trigger the Agent/Task tool, creating subagent spans.

### Testing Compaction

For compaction testing, create a long conversation:

```bash
> "Explain quantum computing in detail"
> "Now explain machine learning"
> "Compare the two fields"
# ... continue until context window triggers compaction
```

### Custom Validation

You can also run the validation script directly for more control:

```bash
uv run python scripts/validate_export.py \
  --session 3640c6d77574ea64f556583219487860 \
  --verbose
```

## CI/CD Integration

For continuous integration, run the infrastructure and smoke tests:

```bash
# Quick infrastructure check (no manual setup needed)
uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyInfrastructure -v

# Smoke tests (automated, no manual session required)
uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxySmokeTest -v
```

Full validation tests with real sessions should be run manually or in a dedicated E2E environment with pre-generated test sessions.

## Test Matrix

| Test | Automation Level | Phoenix Required | Proxy Required | Manual Setup |
|------|-----------------|------------------|----------------|--------------|
| Infrastructure Check | Fully Automated | Yes | No* | No |
| Smoke Tests | Fully Automated | Yes | No | No |
| Session Validation | Semi-Automated | Yes | No** | Yes |

\* Proxy check will warn if not running but won't fail tests
\*\* Proxy only needed to create new test sessions, not for validation

## Known Limitations

1. **Session Creation Not Automated**: Creating a real Claude Code conversation through the proxy requires manual interaction. We can't easily automate opening Claude Code, typing prompts, and capturing the session ID.

2. **Session ID Must Be Known**: The validation test requires you to provide a session ID. There's no automatic "find the latest session" mechanism (though this could be added).

3. **Phoenix Data Freshness**: There may be a slight delay between conversation completion and data availability in Phoenix. If validation fails immediately after a conversation, wait 10-30 seconds and retry.

4. **Model Availability**: Tests assume standard Claude models are available through the proxy. If using custom models, update configurations accordingly.

## Next Steps

After validating a session successfully:

1. **Review the Export**: Check the generated markdown in `~/.dal/exports/`
2. **Analyze Metrics**: Use `uv run dal analyze-tokens --session <id>` for token analysis
3. **Chain Analysis**: Use `uv run dal chain-export --session <id>` for multi-session chains
4. **Quality Checks**: Run `uv run dal quality --session <id>` for quality analysis

## Related Documentation

- [Validation Script](../../scripts/validate_export.py) - Ground truth validation
- [DAL CLI](../../README.md) - Main CLI documentation
- [Claude-Lens Proxy](../../claude-lens) - Proxy wrapper script
- [Phoenix Setup](../../docker-compose.yml) - Docker configuration
