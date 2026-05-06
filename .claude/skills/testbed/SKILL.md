---
name: testbed
description: E2E pipeline testing. Use when validating changes to tracing, sync, analysis, or export. Guides running tests, extending test coverage, and cleanup.
---

# E2E Pipeline Testbed

Use this skill when you need to:
- Validate that code changes don't break the observability pipeline
- Add new E2E tests for features you're implementing
- Clean up test artifacts

## Quick Reference

```bash
# Run tests
uv run dal run testbed                      # Full test (tracing + subagents)
uv run dal run testbed --prompt minimal.txt # Smoke test (faster)

# Cleanup
uv run dal run cleanup --list               # See test artifacts
uv run dal run cleanup --all                # Delete all test data
```

## When to Run Tests

Run the testbed after modifying:

| Component | Why Test |
|-----------|----------|
| `clients/` | Trace retrieval, API parsing |
| `core/` | Schema normalization, span unification |
| `analysis/` | Chain building, pattern detection |
| `export/` | Output format correctness |
| `cli/` | Command behavior |
| LiteLLM configs | Trace capture, project routing |

## Pipeline Coverage Status

```
[✓] Request Capture    Claude Code → LiteLLM → Phoenix
[ ] Trace Sync         Phoenix → dal sync → storage
[ ] Analysis           Chains, patterns, reconstruction
[ ] Export             Training data, reports
```

**If you're working on an uncovered stage, add E2E tests.**

## Extending the Testbed

### Adding a New Test Prompt

Create `tests/e2e/testbed/prompts/my_test.txt`:

```
PIPELINE_TEST_MODE ACTIVE

[Describe operations Claude should perform]

1. OPERATION: [Specific instruction]
2. OPERATION: [Specific instruction]

CONFIRMATION: Output "PIPELINE_TEST_COMPLETE: [description]"
```

Run it: `uv run dal run testbed --prompt my_test.txt`

### Adding New Assertions

Edit `dev_agent_lens/testing/orchestrator.py` in the `_validate` method:

```python
def _validate(self, spans_df: pd.DataFrame) -> TestResult:
    assertions = {}

    # Add your assertion
    assertions["my_check"] = your_validation_logic(spans_df)

    return TestResult(
        passed=all(assertions.values()),
        assertions=assertions,
        ...
    )
```

### Testing Beyond Tracing

For sync/analysis/export validation:

```bash
# 1. Generate traces
uv run dal run testbed --prompt my_test.txt

# 2. Sync the test project
uv run dal sync --source phoenix-local --project dal-test-<run-id>

# 3. Run analysis
uv run dal analyze --project dal-test-<run-id>

# 4. Export and validate
uv run dal export --project dal-test-<run-id> --format jsonl
```

## Test Isolation

Each run is isolated:
- Phoenix project: `dal-test-<run-id>`
- Run directory: `tests/e2e/testbed/runs/run-<run-id>/`
- Claude session: `~/.claude/projects/...-testbed-runs-run-<run-id>`

## Cleanup Safety

**Protected (never deleted):**
- Phoenix: `dev-agent-lens`, `default` projects
- Sessions: Only testbed paths (`tests-e2e-testbed-runs-run-*`)

```bash
uv run dal run cleanup --phoenix-only   # Only Phoenix projects
uv run dal run cleanup --sessions-only  # Only Claude sessions
uv run dal run cleanup --stale 24       # Older than 24 hours
```

## Full Documentation

For architecture details, troubleshooting, and advanced usage:
```bash
cat tests/e2e/testbed/README.md
```
