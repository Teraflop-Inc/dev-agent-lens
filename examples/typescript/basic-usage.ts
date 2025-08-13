#!/usr/bin/env node
/**
 * Basic Usage Example
 * Demonstrates how to use Claude Code SDK with Dev-Agent-Lens observability
 */

import { query, Options } from '@anthropic-ai/claude-code';
import dotenv from 'dotenv';

// Load environment variables
dotenv.config();

async function runWithObservability() {
  // Configure SDK options for Claude Code query
  const options: Options = {
    // Model configuration - will use LiteLLM proxy for observability
    model: 'claude-sonnet-4-20250514', // This will route through our LiteLLM proxy
    maxTurns: 10,
    customSystemPrompt: 'You are a helpful TypeScript development assistant with full observability through Dev-Agent-Lens.'
  };

  try {
    console.log('✅ Starting Claude Code query with observability');
    
    // Send a query using the Claude Code SDK
    console.log('📤 Sending query to Claude...');
    const response = query({
      prompt: 'Hello! Please explain what TypeScript is and give me 3 benefits of using it. Keep your response concise.',
      options
    });
    
    // Handle streaming responses
    console.log('📥 Receiving response:');
    for await (const message of response) {
      if (message.type === 'assistant') {
        // Extract text content from assistant messages
        const content = message.message.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (block.type === 'text') {
              process.stdout.write(block.text);
            }
          }
        }
        // Each message is traced in Arize through LiteLLM proxy
      } else if (message.type === 'result') {
        if (message.subtype === 'success') {
          console.log('\n✅ Query completed successfully');
          console.log(`Duration: ${message.duration_ms}ms`);
          console.log(`Turns: ${message.num_turns}`);
          console.log(`Cost: $${message.total_cost_usd.toFixed(4)}`);
        } else {
          console.log('\n❌ Query failed:', message.subtype);
        }
      }
    }
    
    console.log('\n📊 Check Arize dashboard for traces: https://app.arize.com');
  } catch (error) {
    console.error('❌ Error:', error);
    // Errors are also tracked in Arize through the proxy
  }
}

// Run if this is the main module
if (require.main === module) {
  runWithObservability().catch(console.error);
}

export { runWithObservability };