# Claude and Codex capture

DAL reads Claude session files from `~/.claude/projects` and Codex session files
from `~/.codex/sessions`. CLI and desktop Codex sessions use the same directory.
Choose a repository scope deliberately: session files can contain prompts, code,
and tool output.

## Local storage

```sh
uv sync --extra otlp
uv run dal store use "$HOME/.dal/spans"
uv run dal ingest-sessions --agent codex --include '*my-work-repo*' \
  --project codex-sessions --to-store --yes
uv run dal ingest-sessions --agent claude-code --include '*my-work-repo*' \
  --project claude-sessions --to-store --yes
```

Use `uv run dal ingest-sessions --help` for the supported agent names and options.
Set your git `user.email`, or pass `--user`, to identify the person. An identity
file maps that identifier to a display name; do not commit your real mapping.

## Every-minute capture on macOS

Create `~/.dal/codex-capture.env`, mode 600:

```sh
DAL_CODEX_ENDPOINT=http://127.0.0.1:4318
DAL_CODEX_INCLUDE='*my-work-repo*'
DAL_CODEX_PROJECT=codex-sessions
DAL_CODEX_DAYS=2
```

The endpoint must be your receiver, reachable over localhost or a trusted private
network. Do not expose the unauthenticated OTLP receiver directly to the internet.
A public host requires an authenticated TLS gateway and client authentication.

```sh
scripts/install_codex_capture.sh
# Inspect the log:
tail -20 ~/.dal/codex-capture.log
# Remove the scheduled job:
scripts/install_codex_capture.sh --remove
```

The job reads the configuration file each time, so changing destinations does not
require reinstalling it. It retries recent sessions on subsequent runs; the store
uses stable span IDs to avoid inserting the same source span twice.

## Verify persistence

An HTTP success means the receiver accepted a batch; it does not prove the typed
layout is current. Query the raw layout for ingestion, then the typed layout for
analysis. Use the appropriate `source` and a bounded time range.

```sh
uv run dal store query --layout raw --format json \
  "SELECT source, count(*) AS spans, max(start_time) AS latest FROM spans GROUP BY source"
uv run dal store query --layout typed --format json \
  "SELECT agent, person, count(DISTINCT session_id) AS sessions FROM spans GROUP BY agent, person"
```

See [the query cookbook](query-cookbook-store.md) for model, token, tool, and ticket
queries. Local file capture and proxy capture can describe the same work: filter
by source rather than adding both sets of usage numbers together.
