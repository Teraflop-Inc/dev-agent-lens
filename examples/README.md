# Claude Agent SDK Examples

This directory contains example implementations of the Claude Agent SDK with Dev-Agent-Lens observability.

## Prerequisites

1. **Start the Dev-Agent-Lens proxy** (from the repository root):
   ```bash
   docker-compose up -d
   ```

2. **Set up environment variables** in each example directory:
   ```bash
   cp .env.example .env
   # Edit .env and add your ANTHROPIC_API_KEY
   ```

## Python Examples

### Setup and Run

We use [uv](https://github.com/astral-sh/uv) for fast Python dependency management.

#### Install uv (if not already installed):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### Run examples:

```bash
cd examples/python

# Run basic usage example (API key authentication)
uv run basic_usage.py

# Run OAuth authentication example (OAuth pass-through)
uv run basic_usage_oauth.py

# Run observable agent examples (includes security analysis and incident response)
uv run observable_agent.py
```

uv automatically manages virtual environments and dependencies from `pyproject.toml`.

## TypeScript Examples

### Setup and Run

```bash
cd examples/typescript

# Install dependencies
npm install

# Run examples using npm scripts
npm run basic      # Basic usage example
npm run tools      # Custom tools example
npm run review -- ./path/to/file.ts  # Code review
npm run docs -- ./src  # Generate documentation

# Or run directly with tsx
npx tsx basic-usage.ts
npx tsx custom-tools.ts
npx tsx code-review.ts ./path/to/file.ts
npx tsx doc-generator.ts ./src ./docs
```

### Alternative: Run with Bun

```bash
cd examples/typescript

# Install dependencies with Bun
bun install

# Run examples
bun run basic-usage.ts
bun run custom-tools.ts
bun run code-review.ts ./path/to/file.ts
bun run doc-generator.ts ./src
```

## Example Descriptions

### Python Examples

#### `basic_usage.py`
- Demonstrates basic SDK setup with observability
- Shows how to use the default model
- Handles streaming responses
- Uses API key authentication
- Includes error handling

#### `basic_usage_oauth.py` 🆕
- OAuth authentication example with LiteLLM proxy
- Tests OAuth pass-through functionality (Case 3)
- Short prompt for fast response times
- Environment configuration validation
- Authentication troubleshooting guide

#### `observable_agent.py`
- Advanced agent implementation with session management
- Multiple specialized agents:
  - **SecurityAnalysisAgent**: Analyzes code for vulnerabilities
  - **IncidentResponseAgent**: Handles incident response with severity calculation
- Structured JSON responses
- Session history tracking
- Batch query processing

### TypeScript Examples

#### `basic-usage.ts`
- Basic SDK configuration with proxy
- Streaming response handling
- Default model usage
- Error tracking in Arize

#### `custom-tools.ts`
- Defines custom tools for the SDK
- Implements metric analysis tools
- System health checking capabilities
- Tool execution tracing

#### `code-review.ts`
- Complete code review agent
- Analyzes files for:
  - Best practices
  - Security issues
  - Performance problems
  - Code quality
- Outputs structured JSON results
- CLI interface for reviewing files

#### `doc-generator.ts`
- Generates API documentation from source files
- Supports TypeScript and JavaScript
- Output formats: Markdown or JSON
- Batch processing for directories
- Creates index files for navigation

## Observability Features

All examples include full observability through Dev-Agent-Lens:

1. **Request Tracking**: Every API call is logged
2. **Token Usage**: Monitor token consumption
3. **Performance Metrics**: Track response times
4. **Tool Execution**: Trace custom tool calls
5. **Error Monitoring**: Capture and trace errors

View traces in your Arize dashboard: https://app.arize.com

## Environment Variables

Each example directory contains a `.env.example` file with required configuration:

### API Key Authentication (basic_usage.py)
- `ANTHROPIC_API_KEY`: Your Anthropic API key
- `ANTHROPIC_BASE_URL`: Proxy URL (default: `http://localhost:8082`)
- `ANTHROPIC_MODEL`: (Optional) Override the default model

### OAuth Authentication (basic_usage_oauth.py) 🆕
- `ANTHROPIC_AUTH_TOKEN`: OAuth token from get-token.ts
- `ANTHROPIC_BASE_URL`: LiteLLM proxy URL (`http://localhost:4000`)
- `ANTHROPIC_API_KEY`: (Optional) API key fallback

#### OAuth Setup Commands:
```bash
# Generate OAuth token
bun run get-token.ts > credentials.json

# Set OAuth environment variables
export ANTHROPIC_AUTH_TOKEN=$(cat credentials.json | jq -r .accessToken)
export ANTHROPIC_BASE_URL=http://localhost:4000

# Start LiteLLM proxy with OAuth support
docker compose up -d
```

## Tips for Running with uv

uv provides several advantages for Python development:

```bash
# Run scripts with automatic dependency resolution (no venv needed!)
uv run script.py

# Run with specific Python version
uv run --python 3.11 script.py

# Sync dependencies from pyproject.toml
uv sync

# Add a new dependency
uv add package-name
```

## Troubleshooting

### Proxy not running
```bash
# Check if proxy is healthy
curl http://localhost:8082/health

# Restart proxy if needed
docker-compose restart
```

### Missing dependencies
```bash
# Python
uv sync  # Install dependencies from pyproject.toml

# TypeScript
npm install
```

### API key issues
Ensure your `.env` file contains a valid `ANTHROPIC_API_KEY`

### View proxy logs
```bash
docker-compose logs -f litellm-proxy
```

## Additional Resources

- [Claude Agent SDK Documentation](https://docs.anthropic.com/en/api/agent-sdk/overview)
- [Dev-Agent-Lens Guide](../../claude-code-sdk-guide.md)
- [LiteLLM Documentation](https://docs.litellm.ai)
- [Arize AI Platform](https://docs.arize.com)