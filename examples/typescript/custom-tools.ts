#!/usr/bin/env node
/**
 * Custom Tools Example
 * Demonstrates how to add custom tools to Claude Agent SDK with observability
 */

import { ClaudeSDKClient, ClaudeCodeOptions, Tool } from '@anthropic-ai/claude-agent-sdk';
import dotenv from 'dotenv';

// Load environment variables
dotenv.config();

// Define custom tools
const customTools: Tool[] = [
  {
    name: 'analyze_metrics',
    description: 'Analyze application metrics',
    input_schema: {
      type: 'object',
      properties: {
        metric_type: { 
          type: 'string',
          enum: ['latency', 'throughput', 'error_rate', 'cpu', 'memory']
        },
        time_range: { 
          type: 'string',
          description: 'Time range like "1h", "24h", "7d"'
        }
      },
      required: ['metric_type']
    },
    handler: async (params: any) => {
      // Tool execution is traced in Arize
      console.log('🔧 Tool called: analyze_metrics with params:', params);
      
      // Simulate metric analysis
      const mockData = {
        metric_type: params.metric_type,
        time_range: params.time_range || '1h',
        value: Math.random() * 100,
        trend: Math.random() > 0.5 ? 'increasing' : 'decreasing',
        anomalies: Math.floor(Math.random() * 5)
      };
      
      return { 
        result: `Analyzed ${params.metric_type} metrics for ${params.time_range || '1h'}`,
        data: mockData
      };
    }
  },
  {
    name: 'check_system_health',
    description: 'Check overall system health',
    input_schema: {
      type: 'object',
      properties: {
        components: {
          type: 'array',
          items: { type: 'string' },
          description: 'List of components to check'
        }
      }
    },
    handler: async (params: any) => {
      console.log('🔧 Tool called: check_system_health');
      
      const components = params.components || ['api', 'database', 'cache'];
      const health = components.map((comp: string) => ({
        component: comp,
        status: Math.random() > 0.2 ? 'healthy' : 'degraded',
        responseTime: Math.floor(Math.random() * 1000)
      }));
      
      return { health_check: health };
    }
  }
];

async function runAdvancedAgent() {
  const sdkOptions: ClaudeCodeOptions = {
    apiKey: process.env.ANTHROPIC_API_KEY,
    baseUrl: 'http://localhost:8082',
    // model is optional - uses Claude Agent SDK's default if not specified
    tools: customTools,
    systemPrompt: 'You are a DevOps assistant with metric analysis capabilities. Use the available tools to analyze system performance.',
    // Enable JSON output for structured responses
    outputFormat: 'json',
    maxTurns: 10
  };

  const client = new ClaudeSDKClient(sdkOptions);
  
  try {
    await client.start();
    console.log('✅ Advanced agent started with custom tools');
    
    // Query that will trigger tool usage
    console.log('📤 Requesting system analysis...');
    const response = await client.query(
      'Analyze the performance metrics for the last 24 hours and check system health for all components'
    );
    
    // All tool calls are traced in Arize
    console.log('📥 Analysis result:', JSON.stringify(response, null, 2));
    
    await client.close();
    console.log('✅ Session completed');
    console.log('📊 Tool calls visible in Arize: https://app.arize.com');
  } catch (error) {
    console.error('❌ Error:', error);
  }
}

// Run if this is the main module
const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  runAdvancedAgent().catch(console.error);
}

export { runAdvancedAgent, customTools };