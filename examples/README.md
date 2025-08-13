# Claude Code SDK Examples

This directory contains example implementations of the Claude Code SDK with Dev-Agent-Lens observability.

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

### Running with UV

[UV](https://github.com/astral-sh/uv) is a fast Python package manager that simplifies dependency management.

#### Install UV (if not already installed):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### Run Python examples:

```bash
cd examples/python

# Install dependencies with uv
uv pip install -e .

# Run basic usage example
uv run python basic_usage.py

# Run observable agent examples (includes security analysis and incident response)
uv run python observable_agent.py
```

### Alternative: Run with standard Python

```bash
cd examples/python

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install claude-code-sdk python-dotenv

# Run examples
python basic_usage.py
python observable_agent.py
```

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

# Or run directly with ts-node
npx ts-node basic-usage.ts
npx ts-node custom-tools.ts
npx ts-node code-review.ts ./path/to/file.ts
npx ts-node doc-generator.ts ./src ./docs
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
- Includes error handling

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

- `ANTHROPIC_API_KEY`: Your Anthropic API key
- `ANTHROPIC_BASE_URL`: Proxy URL (default: `http://localhost:8082`)
- `ANTHROPIC_MODEL`: (Optional) Override the default model

## Tips for Running with UV

UV provides several advantages for Python development:

```bash
# Create a new virtual environment with uv
uv venv

# Sync dependencies from pyproject.toml
uv pip sync pyproject.toml

# Add a new dependency
uv pip install new-package

# Run scripts with automatic dependency resolution
uv run python script.py

# Run with specific Python version
uv run --python 3.11 python script.py
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
uv pip install -e .
# or
pip install -r requirements.txt

# TypeScript
npm install
# or
bun install
```

### API key issues
Ensure your `.env` file contains a valid `ANTHROPIC_API_KEY`

### View proxy logs
```bash
docker-compose logs -f litellm-proxy
```

## Additional Resources

- [Claude Code SDK Documentation](https://docs.anthropic.com/en/docs/claude-code/sdk)
- [Dev-Agent-Lens Guide](../../claude-code-sdk-guide.md)
- [LiteLLM Documentation](https://docs.litellm.ai)
- [Arize AI Platform](https://docs.arize.com)