#!/usr/bin/env python3
"""
Basic Usage Example
Demonstrates how to use Claude Agent SDK with Dev-Agent-Lens observability
"""

import asyncio
import os
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

async def run_with_observability():
    """Basic example of using Claude Agent SDK with observability."""

    # Configure SDK with proxy (using default model)
    options = ClaudeAgentOptions(
        #api_key=os.getenv('ANTHROPIC_API_KEY'),
        #base_url=os.getenv('ANTHROPIC_BASE_URL', 'http://localhost:4000'),
        # model is optional - Claude Agent SDK will use its default model if not specified
        # model='claude-3-5-sonnet-20241022',  # Uncomment to override default
        system_prompt='You are a Python development assistant with full observability',
        max_turns=5
    )

    async with ClaudeSDKClient(options) as client:
        print("✅ Claude SDK client started with observability")

        # Send query
        print("📤 Sending query to Claude...")
        await client.query("""Review this Python code for best practices and suggest improvements:

```python
import requests
import json

def get_user_data(user_id):
    # Get user data from API
    url = f"https://api.example.com/users/{user_id}"
    response = requests.get(url)
    data = json.loads(response.text)
    return data

def process_users(user_ids):
    results = []
    for id in user_ids:
        user = get_user_data(id)
        if user['status'] == 'active':
            results.append(user)
    return results

# Usage
users = process_users([1, 2, 3, 4, 5])
print(f"Found {len(users)} active users")
```

Please identify potential issues and suggest improvements for error handling, performance, and code quality.""")

        # Process streaming response
        print("📥 Receiving response:")
        async for message in client.receive_response():
            print(message, end="", flush=True)
            # Each message is traced in Arize

        print("\n")
        print("✅ Session completed successfully")
        print("📊 Check Arize dashboard for traces: https://app.arize.com")


async def main():
    """Main entry point."""
    try:
        await run_with_observability()
    except Exception as e:
        print(f"❌ Error: {e}")
        # Errors are also tracked in Arize


if __name__ == "__main__":
    asyncio.run(main())

