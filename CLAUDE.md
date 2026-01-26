# Dev-Agent-Lens

Observability and analysis toolkit for Claude Code sessions. Captures traces via LiteLLM proxy, syncs to local storage, and provides analysis tools.

## Project Structure

```
dev_agent_lens/
├── clients/      # Phoenix and Arize API clients
├── core/         # Schema normalization, sync state
├── analysis/     # Conversation chains, patterns
├── export/       # Training data, reports
├── testing/      # E2E test infrastructure
└── cli/          # `dal` command-line interface
```

## Key Commands

```bash
uv run dal sync                    # Incremental sync from backends
uv run dal run testbed             # Run E2E pipeline test
uv run dal run cleanup --list      # List test artifacts
```

## Development

```bash
# Start Phoenix (local observability)
docker compose --profile phoenix up -d

# Proxy at localhost:4000, Phoenix UI at localhost:6006
```

---

## E2E Testing Expectations

**This project uses end-to-end testing to validate the full data lifecycle.**

### Default Behavior for Agents

When implementing or modifying code in this project:

1. **Run the testbed** after making changes to verify the pipeline still works
2. **Extend the testbed** when adding features that aren't covered by existing tests
3. **Add new test prompts** when implementing functionality that should be validated E2E

The testbed is not just for catching bugs - it's **living documentation** of what the pipeline guarantees. If a behavior isn't tested, it's not guaranteed.

### When to Test

Run `uv run dal run testbed` after modifying:
- `clients/` - trace retrieval, API interactions
- `core/` - schema normalization, data unification
- `analysis/` - chain building, pattern detection
- `export/` - output formats, data transformation
- `cli/` - command behavior, user-facing features
- Docker/LiteLLM configs - trace capture, routing

### When to Extend Tests

Add new E2E tests when:
- Implementing a new feature that affects data flow
- Adding a new analysis capability that should be validated
- Changing output formats that downstream consumers depend on
- The existing tests don't exercise the code path you're changing

### How to Extend

Use `/testbed` for detailed guidance on:
- Creating new test prompts
- Adding assertions to validate behavior
- Testing the full pipeline (trace → sync → analysis → export)

Quick reference:
```bash
uv run dal run testbed --prompt minimal.txt  # Smoke test (~60 spans)
uv run dal run testbed                       # Full test (~65k spans)
uv run dal run cleanup --all                 # Clean up after testing
```

---

## Code Style

- Use `uv run` for all Python commands
- Type hints for public APIs
- `pathlib.Path` over string paths
- `logging` module, not print

## Gotchas

- Phoenix project set via `OTEL_SERVICE_NAME` at container start
- Test containers use ports 4100/4101, not 4000
- Claude sessions stored in `~/.claude/projects/` by working directory
