#!/bin/bash

# Check if proxy is running
echo "🔍 Checking if LiteLLM proxy is running..."
if ! curl -s http://localhost:8082/health >/dev/null; then
  echo "❌ LiteLLM proxy is not running. Please start it first with: docker-compose up -d"
  exit 1
fi

echo "✅ LiteLLM proxy is running"
echo "🚀 Starting Claude Code with Arize tracing..."

# Set the environment variable and run Claude Code
export ANTHROPIC_BASE_URL=http://localhost:8082
claude "$@"
