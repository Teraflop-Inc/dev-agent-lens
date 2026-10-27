# Proxy Setup

This guide covers setting up the LiteLLM proxy to capture Claude Code traces.

Where to run the proxy so clients can reach it (one machine, a private network, or the
public internet) is covered in [Reaching the proxy](reaching-the-proxy.md).

## Prerequisites

- Docker and Docker Compose
- Claude Code CLI (`curl -fsSL https://claude.ai/install.sh | sh`)
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com/settings/keys))

## Configuration

### Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | Fallback API key (OAuth takes priority) |
| `ARIZE_API_KEY` | For Arize | Arize cloud API key |
| `ARIZE_SPACE_KEY` | For Arize | Arize space identifier |
| `CLAUDE_LENS_PROJECT` | No | Project name for trace routing (default: `dev-agent-lens`) |
| `CLAUDE_LENS_PROXY_URL` | No | Proxy URL (default: `http://localhost:4000`) |

*OAuth passthrough works automatically for Pro/Max plans.

## Starting the Proxy

### Option A: Phoenix (Local)

```bash
docker compose --profile phoenix up -d
```

Phoenix UI available at http://localhost:6006

### Option B: Arize (Cloud)

```bash
docker compose --profile arize up -d
```

View traces at [app.arize.com](https://app.arize.com)

## Using Claude Code

```bash
# Use the wrapper script
./claude-lens

# Or install globally
sudo cp claude-lens /usr/local/bin
claude-lens
```

The wrapper script:
- Checks if the proxy is running
- Configures Claude Code to route through LiteLLM
- Passes OAuth tokens automatically

### Headless and scripted runs (`-p`)

Unrecognised arguments are passed straight through to Claude Code, so headless runs work as
you'd expect and **are captured**:

```bash
./claude-lens -p "summarise this repo"
```

`claude -p` invoked directly is **not** captured. It never sees `ANTHROPIC_BASE_URL`, so it
talks to the API host and produces no spans. This is easy to miss because nothing fails — the
run succeeds, it just leaves no trace. The local transcript under `~/.claude/projects/` is
still written either way, so the data isn't lost; only the telemetry is. If a script, cron
job, CI step, or `Task`-style worker shells out to `claude`, point it at `claude-lens`.

> **Putting `ANTHROPIC_BASE_URL` in `~/.claude/settings.json` instead is a tradeoff.**
> It catches every invocation, including headless jobs and the VS Code extension, but every
> Claude Code session then depends on the proxy: when it is down or unreachable, Claude Code
> stops working instead of going untraced. It also switches off Remote Control, which Claude
> Code disables whenever `ANTHROPIC_BASE_URL` is not api.anthropic.com (the same applies under
> `claude-lens`), and it does nothing for the desktop app, which ignores it. The wrapper's
> health check keeps failure explicit and scoped to runs you asked to trace.
> [Capture by surface](capture-by-surface.md) lays out each option per surface, with the
> verification query for each.

### Custom Proxy URL

```bash
./claude-lens --proxy-url http://localhost:4001
# Or via environment
CLAUDE_LENS_PROXY_URL=http://remote:4000 ./claude-lens
```

## Project Switching

Route traces to different projects for isolation:

```bash
# Set project and restart proxy
export CLAUDE_LENS_PROJECT=my-test-project
docker compose --profile phoenix down
docker compose --profile phoenix up -d

./claude-lens  # Traces now go to my-test-project
```

> **Note:** Project name is set at container startup. You must restart the proxy to change projects.

**Backend mapping:**
- Phoenix: Uses `openinference.project.name` resource attribute
- Arize: Uses `OTEL_SERVICE_NAME`

## Model Routing

The proxy uses wildcard routing - Claude Code selects the model, the proxy passes it through:

- `claude-*` patterns supported
- `anthropic/*` patterns supported
- Optional aliases: `sonnet`, `haiku`

No model override - Claude Code decides which model to use.

## Managing the Proxy

```bash
# Start
docker compose --profile phoenix up -d

# Stop
docker compose --profile phoenix down

# View logs
docker compose logs -f litellm-proxy

# Restart after config changes
docker compose restart

# Health check
curl http://localhost:4000/health
```

## Troubleshooting

**Proxy not responding:**
```bash
docker compose ps                    # Check if running
curl http://localhost:4000/health    # Health check
docker compose logs -f litellm-proxy # View logs
```

**OAuth issues:**
- Check logs for OAuth token detection messages
- Ensure `ANTHROPIC_API_KEY` is set as fallback

**Traces not appearing:**
- Verify project name matches what you're filtering by
- Check Phoenix/Arize for the correct project
- Filter: `status_code = 'OK' and attributes.llm.token_count.total > 0`

## Key Files

| File | Purpose |
|------|---------|
| `claude-lens` | Wrapper script for Claude Code |
| `docker-compose.yml` | Service definitions |
| `litellm_config.yaml` | Model routing and callbacks |
| `.env` | Local environment config |
