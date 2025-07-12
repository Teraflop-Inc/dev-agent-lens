# Claude Code Proxy with LiteLLM and Arize Integration

A proxy setup for Claude Code that adds observability, monitoring, and tracing capabilities through LiteLLM and Arize AI integration.

## Overview

This repository provides a transparent proxy layer for Claude Code that:

- Intercepts Claude Code API calls and routes them through LiteLLM
- Adds AI observability and monitoring via Arize AI
- Maintains full compatibility with the standard Claude Code CLI
- Provides centralized model configuration and management

## Architecture

```
Claude Code CLI → LiteLLM Proxy (localhost:8082) → Anthropic API
                       ↓
                   Arize AI (Observability)
```

## Prerequisites

- Docker and Docker Compose
- Claude Code CLI installed
- Anthropic API key
- Arize AI account (for observability)

## Environment Variables

The following environment variables are required:

- `ANTHROPIC_API_KEY` - Your Anthropic API key for Claude models
- `ARIZE_API_KEY` - Your Arize AI API key for observability
- `ARIZE_SPACE_KEY` - Your Arize AI space key

Optional environment variables:

- `ARIZE_MODEL_ID` - Model ID shown in Arize UI (default: `litellm-proxy`)
- `ARIZE_MODEL_VERSION` - Model version shown in Arize UI (default: `local-dev`)
- `LITELLM_MASTER_KEY` - Master key for LiteLLM proxy authentication (optional)

## Quick Start

### 1. Prerequisites Check

- Docker and Docker Compose installed
- Claude Code CLI installed (`curl -fsSL https://claude.ai/install.sh | sh`)
- API keys ready (see Environment Variables section)

### 2. Setup Environment

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env and add your API keys
# ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
# ARIZE_SPACE_KEY=your-space-key-here
# ARIZE_API_KEY=your-api-key-here
```

### 3. Start the Service

```bash
# Start the proxy (runs in background)
docker-compose up -d

# Verify it's running (should return healthy endpoints)
curl http://localhost:8082/health
```

### 4. Use Claude Code with Observability

```bash
# Use the wrapper script (recommended)
./claude-lens

# Or install globally for convenience
sudo cp claude-lens /usr/local/bin
claude-lens
```

### 5. View Traces in Arize

- Open [Arize AI Dashboard](https://app.arize.com)
- Navigate to your project
- Filter traces with: `status_code = 'OK' and attributes.llm.token_count.total > 0`

**That's it!** All Claude Code interactions now include full observability and tracing.

## Configuration

### Model Mapping

The proxy maps Claude Code model names to Anthropic API models via `litellm_config.yaml`:

- `claude-sonnet-4-20250514` → `anthropic/claude-sonnet-4-20250514`
- `claude-3-5-sonnet-20241022` → `anthropic/claude-3-5-sonnet-20241022`
- `claude-3-haiku-20240307` → `anthropic/claude-3-haiku-20240307`
- Aliases: `sonnet`, `haiku`

### Services

- **Proxy Port**: 8082 (external) → 4000 (internal)
- **Health Check**: <http://localhost:8082/health>
- **OpenTelemetry**: Configured for Arize endpoint

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

The `docker-compose.yml` file sets up the LiteLLM proxy service with:

- **Image**: Uses the official LiteLLM image (`ghcr.io/berriai/litellm:main-latest`)
- **Port mapping**: 8082 (host) → 4000 (container)
- **Configuration**: Mounts `litellm_config.yaml` for model routing
- **Environment**: Passes through all required API keys and Arize configuration
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

- **Check if proxy is running**: `docker-compose ps`
- **Verify proxy health**: `curl http://localhost:8082/health`
- **View real-time logs**: `docker-compose logs -f`
- **Verify environment variables**: Ensure all required variables are set in `.env`
- **Claude Lens errors**: The wrapper script will check if the proxy is running before starting Claude Code

## Benefits

- **Complete Observability**: Full visibility into Claude Code usage
- **Zero Configuration**: Works transparently with existing Claude Code workflows
- **Enterprise Ready**: Built-in monitoring and cost tracking
- **Model Management**: Centralized configuration for all Claude models
- **Extensible**: Based on LiteLLM's robust proxy framework
