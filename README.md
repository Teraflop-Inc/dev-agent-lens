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
Claude Code CLI � LiteLLM Proxy (localhost:8082) � Anthropic API
                       �
                   Arize AI (Observability)
```

## Prerequisites

- Docker and Docker Compose
- Claude Code CLI installed
- Anthropic API key
- Arize AI account (for observability)

## Quick Start

1. **Set up environment variables:**

```bash
export ANTHROPIC_API_KEY="your-anthropic-key"
export ARIZE_API_KEY="your-arize-key"
export ARIZE_SPACE_KEY="your-arize-space-key"

# Optional: Custom Arize model configuration
export ARIZE_MODEL_ID="litellm-proxy"
export ARIZE_MODEL_VERSION="local-dev"
```

2. **Start the proxy:**

```bash
docker-compose up -d
```

3. **Use Claude Code with the proxy:**

```bash
./claude-lens [your-claude-code-arguments]
```

4. Optionally copy Claude Lens to `/usr/local/bin`

```bash
sudo cp claude-lens /usr/local/bin
```

The proxy will transparently handle all Claude Code interactions while providing full observability.

## Configuration

### Model Mapping

The proxy maps Claude Code model names to Anthropic API models via `litellm_config.yaml`:

- `claude-sonnet-4-20250514` � `anthropic/claude-sonnet-4-20250514`
- `claude-3-5-sonnet-20241022` � `anthropic/claude-3-5-sonnet-20241022`
- `claude-3-haiku-20240307` � `anthropic/claude-3-haiku-20240307`
- Aliases: `sonnet`, `haiku`

### Services

- **Proxy Port**: 8082 (external) � 4000 (internal)
- **Health Check**: <http://localhost:8082/health>
- **OpenTelemetry**: Configured for Arize endpoint

## Key Files

- `claude-proxy` - Wrapper script that starts Claude Code with proxy configuration
- `docker-compose.yml` - Service definition and environment setup
- `litellm_config.yaml` - Model routing and callback configuration
- `Dockerfile` - Custom LiteLLM build with modifications
- `litellm/` - Custom fork of LiteLLM with project-specific enhancements

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

## Development

The setup includes a custom LiteLLM build located in the `./litellm` directory, which contains modifications specific to this proxy implementation.

## Troubleshooting

- Ensure the proxy is running: `docker-compose ps`
- Check proxy health: `curl http://localhost:8082/health`
- View logs: `docker-compose logs -f`
- Verify environment variables are set correctly

## Benefits

- **Complete Observability**: Full visibility into Claude Code usage
- **Zero Configuration**: Works transparently with existing Claude Code workflows
- **Enterprise Ready**: Built-in monitoring and cost tracking
- **Model Management**: Centralized configuration for all Claude models
- **Extensible**: Based on LiteLLM's robust proxy framework

