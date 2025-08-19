# Dev-Agent-Lens with LiteLLM and Arize Integration

A proxy setup for Open Source, Open Telemetry Compliant, or proxyable Developer Agents to add observability, monitoring, and tracing capabilities. The first developer agent is Claude Code.

## Overview

This repository provides a transparent proxy layer for Claude Code that:

- Intercepts Claude Code API calls and routes them through LiteLLM
- Adds AI observability and monitoring via Arize AI
- Maintains full compatibility with the standard Claude Code CLI
- Provides centralized model configuration and management

## Architecture

```
Claude Code CLI → LiteLLM Proxy (localhost:4000) → Anthropic API
                       ↓                              ↓
                  PostgreSQL DB               Arize AI (Observability)
                       ↓
                   Web UI (:4000/ui)
```

## Prerequisites

- Docker and Docker Compose
- Claude Code CLI installed (`curl -fsSL https://claude.ai/install.sh | sh`)
- Anthropic API key (get from https://console.anthropic.com/settings/keys)

## Quick Start (No Authentication)

Get started in under 2 minutes with just an Anthropic API key!

### 1. Setup Environment

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env and add ONLY your Anthropic API key
# ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
```

### 2. Start the Simple Proxy

```bash
# Start the no-auth proxy (no database, no UI) - this is now the default!
docker compose up -d
```

### 3. Use Claude Code

```bash
# Use the wrapper script
./claude-lens

# Or install globally for convenience
sudo cp claude-lens /usr/local/bin
claude-lens
```

**That's it!** Claude Code now routes through LiteLLM for consistent API handling.

## View Observability in Arize

- Open [Arize AI Dashboard](https://app.arize.com)
- Navigate to your project  
- Filter traces: `status_code = 'OK' and attributes.llm.token_count.total > 0`

## Claude Code SDK Examples

This repository includes comprehensive examples for integrating the Claude Code SDK with Dev-Agent-Lens observability in both **TypeScript** and **Python**. These examples demonstrate advanced usage patterns, specialized agents, and full observability integration.

### Available Examples

**TypeScript Examples** (`examples/typescript/`):
- **Basic Usage**: Simple SDK setup with proxy observability
- **Code Review Agent**: Automated code analysis with structured feedback  
- **Custom Tools**: Advanced tool integration and execution tracing
- **Documentation Generator**: Automatic API documentation generation

**Python Examples** (`examples/python/`):
- **Basic Usage**: Core SDK functionality with streaming responses
- **Observable Agent**: Advanced agent framework with:
  - Security Analysis Agent (vulnerability detection)
  - Incident Response Agent (automated incident handling)
  - Session management and history tracking

### Quick Start with Examples
Our examples contain code samples to leverage the Claude Code SDKs for python and typescript, while maintaining the proxy and observability features from Dev-Agent-Lens.

```bash
# 1. Ensure the proxy is running
docker-compose up -d

# 2. Try TypeScript examples
cd examples/typescript
npm install
npm run basic                    # Basic usage
npm run review basic-usage.ts    # Code review

# 3. Try Python examples  
cd examples/python
uv pip install -e .
uv run python basic_usage.py     # Basic usage
uv run python observable_agent.py # Advanced agents
```

All examples automatically route through the LiteLLM proxy for full observability without requiring command-line exports.

📖 **[View Complete Examples Guide →](examples/README.md)**

## Configuration

### Model Routing

The proxy uses wildcard routing in `litellm_config.yaml` to allow Claude Code to select any model:

- **Automatic Pass-through**: Any model Claude Code selects is automatically routed to Anthropic
- **Wildcard Support**: Supports patterns like `claude-*`, `anthropic/*`, and `claude-opus-*`
- **No Model Override**: The proxy doesn't force a specific model - Claude Code decides
- **Optional Aliases**: `sonnet` and `haiku` shortcuts are available but not required

### Services

- **LiteLLM Proxy**: Port 4000
- **Health Check**: <http://localhost:4000/health>
- **OpenTelemetry**: Configured for Arize endpoint (when ARIZE keys are configured)

## Key Files

- `claude-lens` - Wrapper script that starts Claude Code with proxy configuration
- `docker-compose.yml` - Service definition and environment setup
- `litellm_config.yaml` - Model routing and callback configuration
- `.env.example` - Example environment variables file
- `.env` - Your local environment configuration (not tracked in git)

## Monitoring & Observability

All Claude Code interactions are automatically:

- Logged and traced in Arize AI
- Monitored for performance and usage patterns
- Available for cost analysis and optimization
- Tracked with OpenTelemetry standards

### Viewing Traces in Arize

To filter traces in Arize for relevant tool-related interactions, use this filter:

```
status_code = 'OK' and attributes.llm.token_count.total > 0 and attributes.input.value contains "tool" or attributes.input.value contains "text"
```

## Docker Compose Configuration

The `docker-compose.yml` file sets up the LiteLLM proxy:

### LiteLLM Proxy Service
- **Image**: Custom OAuth-enabled image (`aowen14/litellm-oauth-fix:latest`)
- **Port mapping**: 4000 (host) → 4000 (container)
- **Configuration**: Mounts `litellm_config.yaml` for model routing
- **Environment**: Passes through API keys and Arize configuration
- **Health checks**: Automatic health monitoring every 30 seconds
- **Restart policy**: Automatically restarts unless manually stopped

## Development

To modify the proxy configuration:

1. Edit `litellm_config.yaml` to change model mappings or callbacks
2. Update `.env` with your API credentials
3. Restart the proxy: `docker-compose restart`

## Managing the Proxy

### Starting the proxy

```bash
docker-compose up -d
```

### Stopping the proxy

```bash
docker-compose down
```

### Viewing logs

```bash
docker-compose logs -f
```

### Restarting after configuration changes

```bash
docker-compose restart
```

## Troubleshooting

- **Check if services are running**: `docker-compose ps`
- **Verify proxy health**: `curl http://localhost:4000/health`
- **View real-time logs**: `docker-compose logs -f litellm-proxy`
- **Verify environment variables**: Ensure required variables are set in `.env`
- **Claude Lens errors**: The wrapper script will check if the proxy is running before starting Claude Code
- **OAuth issues**: Check logs for OAuth token detection and passthrough messages
- **API key fallback**: Ensure `ANTHROPIC_API_KEY` is set if not using OAuth

## Benefits

- **Complete Observability**: Full visibility into Claude Code usage
- **Zero Configuration**: Works transparently with existing Claude Code workflows
- **Enterprise Ready**: Built-in monitoring and cost tracking
- **Model Management**: Centralized configuration for all Claude models
- **Extensible**: Based on LiteLLM's robust proxy framework
