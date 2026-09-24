# Dev-Agent-Lens

Capture and query the work of Claude Code, Codex, and the people reviewing it.
DAL stores traces as Parquet and exposes them through DuckDB. Use a directory for
one developer, or an S3-compatible object store for a team. Phoenix is an optional
source for existing history, not a requirement for new session capture.

## Start with local sessions

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/Teraflop-Inc/dev-agent-lens.git
cd dev-agent-lens
uv sync --extra otlp
uv run dal store use "$HOME/.dal/spans"
uv run dal ingest-sessions --agent codex --include '*my-work-repo*' \
  --project codex-sessions --to-store --yes
uv run dal store query --layout raw --format json \
  "SELECT source, count(*) AS spans FROM spans GROUP BY source"
```

Choose the repository scope before importing. Session logs contain prompts,
source code, and tool results. For Claude sessions, use `--agent claude-code` and a
separate project name. [Capture setup](docs/codex-capture.md) covers both agents,
identity, scheduled uploads, and persistence checks.

## Typed queries

The raw layout preserves producer data. The typed layout extracts frequently
queried fields such as model, tokens, person, session, agent, ticket, and tool
kind, while retaining other attributes. Build it explicitly:

```sh
DAL_SPAN_LAYOUT=typed uv run dal store verify \
  --from-parquet "$HOME/.dal/spans/spans_raw/**/*.parquet"
uv run dal store query --layout typed --format json \
  "SELECT agent, model_name, count(*) AS spans, sum(tokens_prompt) AS prompt_tokens FROM spans GROUP BY agent, model_name"
```

A rebuild is a maintenance operation: it replaces the derived layout. Coordinate
writers and readers before rebuilding a shared store. It is not an incremental
streaming index. [Storage formats](docs/span-store-formats.md) explains the raw,
typed, and blob datasets; the [cookbook](docs/query-cookbook-store.md) supplies
queries you can run against them.

## Run for a team

- [Self-hosted deployment](deploy/README.md): Docker Compose, local MinIO, receiver,
  source sync, identity mapping, and verification.
- [External S3 or Cloudflare R2](deploy/HOSTED.md): separate object storage,
  credentials, migration gates, and hosting requirements.
- [Historical import](docs/sync-historical.md): Phoenix/Arize sources and resumable
  sync. The Oxen import script is provided for migration of existing archives.
- [Git events](docs/webhook-events.md): signed GitHub and Forgejo webhooks and
  historical pull-request backfill.
- [Proxy setup](docs/proxy-setup.md): live LLM tracing with LiteLLM.

Keep live deployment settings, identities, credentials, and captured data outside
Git. The public repository contains code and examples, not deployment history or
private session archives.

## Export and analyze

`dal claude-session-logs-to-markdown` exports readable Claude transcripts, including
linked subagents and compaction context. See the
[session export guide](docs/quickstart_session_export.md),
[query cookbook](docs/query-cookbook.md), and
[schema resiliency guide](docs/schema-resiliency.md).

## Development

```sh
uv sync --extra dev --extra s3 --extra otlp
uv run pytest -q
uv run dal run testbed --prompt minimal.txt
```

The testbed requires the configured external capture services and a valid model
credential. Tests marked `integration` require their named live services or
fixtures. See [CLAUDE.md](CLAUDE.md) for development and end-to-end testing details.
