# E2E Testing - Quick Start

## TL;DR

```bash
# Check infrastructure is ready
uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyInfrastructure -v

# Validate an existing session
TEST_SESSION_ID=<your-session-id> \
  uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyPipeline::test_validate_session_export -v

# Or run the demo script (auto-finds a session)
./tests/e2e/run_validation_demo.sh
```

## What Gets Tested

### ✓ Automated Tests
- Phoenix connectivity and health
- LiteLLM proxy health (warning if not running)
- claude-lens script existence
- DAL CLI availability
- Session listing from Phoenix
- Sync mechanism (if data available)

### ⚠️ Semi-Automated Tests
- **Session validation**: Requires a session ID (manual conversation through proxy)
  - Checks user messages in export
  - Checks assistant responses in export
  - Checks tool calls in export
  - Checks tool results (inline vs linked)
  - Checks subagent spawning (if present)

### ✗ Manual Steps Required
- Creating a test conversation through claude-lens proxy
- Noting the session ID from Phoenix UI
- Providing session ID to validation test

## Prerequisites Checklist

- [ ] Phoenix running: `curl http://localhost:6006/arize_phoenix_version`
- [ ] Proxy running (optional): `curl http://localhost:4000/health`
- [ ] Dependencies installed: `uv sync`
- [ ] Claude-lens script exists: `ls ~/Company/dev3/private-dev-agent-lens/claude-lens`

## Running Tests

### Option 1: Full Test Suite (Recommended for CI)

```bash
uv run pytest tests/e2e/ -v
```

**Expected result**: 5 passed, 2 skipped
- Skipped: Session validation (no TEST_SESSION_ID provided)
- Skipped: Sync test (no recent data, expected)

### Option 2: Infrastructure Only

```bash
uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyInfrastructure -v
```

**Expected result**: 4 passed
- All checks green = ready for testing

### Option 3: Validate Specific Session

```bash
# First, get a session ID:
# 1. Run: ~/Company/dev3/private-dev-agent-lens/claude-lens
# 2. Type: "What is 2+2?"
# 3. Get session ID from Phoenix UI: http://localhost:6006

# Then validate:
TEST_SESSION_ID=abc123def456 \
  uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyPipeline::test_validate_session_export -v
```

**Expected result**: 1 passed with detailed validation report

### Option 4: Demo Script (Easiest)

```bash
# Auto-finds a suitable session and validates it
./tests/e2e/run_validation_demo.sh

# Or validate a specific session
./tests/e2e/run_validation_demo.sh abc123def456
```

**Expected result**: Full validation report with color output

## Interpreting Results

### ✅ All Green
```
✅ User messages: 5/5 found
✅ Assistant messages: 10/10 found
✅ Tool calls: 3/3 found
✅ Tool results: 3/3 found (Inline: 2, Linked: 1)
✅ Subagents: 0/0 found

✅ RESULT: PASS
```

**Meaning**: The export contains all content from the raw traces. Perfect!

### ❌ Content Missing
```
✅ User messages: 1/1 found
❌ Assistant messages: 1/2 found
   Missing: Here is my analysis...
```

**Meaning**: Some content is missing from the export. This indicates:
1. Export logic might be filtering too aggressively
2. Content classification might be incorrect
3. There's a bug in the export pipeline

**Action**: Check the missing content in the raw traces and debug the export logic.

### ⚠️ Warnings
```
⚠️  Warnings:
   Large result (5000 chars) found inline, expected link
```

**Meaning**: Content appears in unexpected format but is present. Not a failure, but worth investigating for optimization.

## Common Issues

### "Phoenix not running"
```bash
docker compose --profile phoenix up -d
```

### "No phoenix-local parquet files found"
```bash
# Sync data from Phoenix
uv run dal sync --source phoenix-local --start 2024-01-01
```

### "No spans found for session X"
- Double-check session ID from Phoenix UI
- Make sure data was synced: `uv run dal sync --source phoenix-local`
- Verify session exists in Phoenix first

### "DAL CLI not available"
```bash
uv sync
```

## Integration with CI/CD

Add to your CI pipeline:

```yaml
- name: E2E Infrastructure Check
  run: uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyInfrastructure -v

- name: E2E Smoke Tests
  run: uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxySmokeTest -v
```

**Note**: Full session validation tests require manual setup and should run in dedicated E2E environments or as manual QA steps.

## Next Steps After Validation

Once a session validates successfully:

```bash
# Analyze token usage
uv run dal analyze-tokens --session <session-id>

# Export to different formats
uv run dal chain-export --session <session-id> --format markdown
uv run dal chain-export --session <session-id> --format jsonl

# Quality analysis
uv run dal quality --session <session-id>

# Check for chains (multi-session conversations)
uv run dal chain-list
```

## Documentation

- **Full Guide**: [README.md](./README.md)
- **Test Code**: [test_proxy_pipeline.py](./test_proxy_pipeline.py)
- **Validation Logic**: [../../scripts/validate_export.py](../../scripts/validate_export.py)
- **Demo Script**: [run_validation_demo.sh](./run_validation_demo.sh)
