# E2E Pipeline Testbed

End-to-end testing infrastructure for validating the complete dev-agent-lens pipeline.

## Purpose

This testbed validates the **full lifecycle** of observability data:

```
Claude Code Request → LiteLLM Proxy → Phoenix/Arize → Sync → Storage → Analysis → Export
```

Each stage can be tested independently or as part of the full pipeline.

## Quick Start

```bash
# Run the stress test (validates tracing + subagent spans)
uv run dal run testbed

# Run minimal smoke test (faster, validates basic tracing)
uv run dal run testbed --prompt minimal.txt

# List test artifacts
uv run dal run cleanup --list

# Clean up test data
uv run dal run cleanup --all
```

## Architecture

```
tests/e2e/testbed/
├── prompts/              # Test prompts for Claude Code
│   ├── minimal.txt       # Smoke test: single Read operation
│   └── stress_test.txt   # Full test: Read + Explore subagent
├── sample_code/          # Target codebase for test operations
│   ├── main.py
│   └── utils/
├── runs/                 # Per-run directories (gitignored)
│   └── run-<id>/         # Isolated environment for each test
└── .claude.md            # Instructions for Claude in test mode
```

## Pipeline Stages

### Stage 1: Request Capture (Currently Implemented)
- Claude Code executes prompts in isolated run directories
- LiteLLM proxy intercepts all API calls
- Traces sent to Phoenix (local) or Arize (cloud)

**Validated by:** `dal run testbed`

### Stage 2: Trace Sync (Partially Implemented)
- `dal sync` pulls traces from observability backend
- Spans normalized to common schema
- Stored in Oxen data repository

**To validate:** After testbed run, verify spans appear in sync output

### Stage 3: Analysis (Not Yet Implemented)
- Conversation chain building
- Pattern detection
- Session reconstruction

**Needs:** Test prompts that generate specific patterns to validate

### Stage 4: Export (Not Yet Implemented)
- Training data export
- Report generation
- Dataset creation

**Needs:** End-to-end test that exports and validates output format

## Writing New Tests

### Adding a New Prompt

Create a new file in `prompts/`:

```
prompts/my_test.txt
```

```
PIPELINE_TEST_MODE ACTIVE

[Describe what operations Claude should perform]

1. OPERATION_NAME: [Specific instruction]
2. OPERATION_NAME: [Specific instruction]

3. CONFIRMATION: Output "PIPELINE_TEST_COMPLETE: [description]"
```

Run it:
```bash
uv run dal run testbed --prompt my_test.txt
```

### Adding New Assertions

Edit `dev_agent_lens/testing/orchestrator.py`, in the `_validate` method:

```python
def _validate(self, spans_df: pd.DataFrame) -> TestResult:
    assertions = {}

    # Existing assertions
    assertions["has_llm_spans"] = ...
    assertions["has_read_tool"] = ...

    # Add your new assertion
    assertions["my_new_check"] = your_validation_logic(spans_df)

    return TestResult(
        passed=all(assertions.values()),
        assertions=assertions,
        ...
    )
```

### Testing the Full Pipeline

To test beyond tracing (sync, analysis, export):

```python
# In your test prompt or script:

# 1. Run testbed to generate traces
uv run dal run testbed --prompt my_test.txt

# 2. Sync the test project
uv run dal sync --source phoenix-local --project dal-test-<run-id>

# 3. Run analysis on synced data
uv run dal analyze --project dal-test-<run-id>

# 4. Export and validate
uv run dal export --project dal-test-<run-id> --format jsonl
```

## Test Isolation

Each test run is isolated:
- **Phoenix project**: `dal-test-<run-id>` (unique per run)
- **Run directory**: `tests/e2e/testbed/runs/run-<run-id>/`
- **Claude session**: `~/.claude/projects/...-testbed-runs-run-<run-id>`

This allows multiple concurrent tests without interference.

## Cleanup

Test data accumulates in three places:

| Location | Cleanup Command |
|----------|-----------------|
| Phoenix projects | `dal run cleanup --phoenix-only` |
| Claude sessions | `dal run cleanup --sessions-only` |
| Both | `dal run cleanup --all` |

**Safety guarantees:**
- Phoenix: Only `dal-test-*` projects deleted; `dev-agent-lens`, `default` protected
- Sessions: Only testbed sessions deleted (path must contain `tests-e2e-testbed-runs-run-`)

## Extending the Testbed

When adding new features to dev-agent-lens, consider:

1. **Does this affect tracing?** → Add/modify prompts to exercise new code paths
2. **Does this affect sync?** → Add validation that synced data contains expected attributes
3. **Does this affect analysis?** → Add test cases with known patterns to validate detection
4. **Does this affect export?** → Add format validation for exported data

The goal is **living documentation through tests** - if a feature isn't tested end-to-end, it's not fully validated.

## Troubleshooting

### Container won't start
```bash
# Check if Phoenix is running
curl http://localhost:6006/health

# Start Phoenix
docker compose --profile phoenix up -d phoenix

# Check test container logs
docker compose --profile test-phoenix logs litellm-test-phoenix
```

### No spans found
1. Verify the container is using the correct project name:
   ```bash
   docker inspect private-dev-agent-lens-litellm-test-phoenix-1 | grep OTEL_SERVICE_NAME
   ```
2. Check Phoenix UI at http://localhost:6006 for the test project

### Timeout during Claude Code execution
- Default timeout is 300s
- For long-running tests, the orchestrator may need adjustment
- Check if Claude is waiting for user input (shouldn't happen in --print mode)
