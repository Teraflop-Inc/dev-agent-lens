#!/usr/bin/env node
/**
 * Code Review Agent Example
 * Demonstrates building a code review agent with observability
 */

import { query, Options } from '@anthropic-ai/claude-agent-sdk';
import { readFileSync, existsSync } from 'fs';
import { join } from 'path';
import dotenv from 'dotenv';

// Load environment variables
dotenv.config();

interface CodeReviewResult {
  file: string;
  issues: Array<{
    severity: 'error' | 'warning' | 'info';
    line?: number;
    message: string;
    suggestion?: string;
  }>;
  summary: {
    total_issues: number;
    errors: number;
    warnings: number;
    info: number;
  };
}

async function reviewCode(filePath: string): Promise<CodeReviewResult> {
  // Check if file exists
  if (!existsSync(filePath)) {
    throw new Error(`File not found: ${filePath}`);
  }

  const code = readFileSync(filePath, 'utf-8');
  const fileName = filePath.split('/').pop() || filePath;
  
  const options: Options = {
    model: 'claude-sonnet-4-20250514', // Routes through LiteLLM proxy
    systemPrompt: `You are a senior code reviewer. Provide constructive feedback in JSON format.
    
    Return a JSON object with this structure:
    {
      "issues": [
        {
          "severity": "error|warning|info",
          "line": <line_number_if_applicable>,
          "message": "description of the issue",
          "suggestion": "how to fix it"
        }
      ],
      "best_practices": ["list of followed best practices"],
      "improvements": ["list of suggested improvements"]
    }`,
    maxTurns: 1
  };
  
  try {
    console.log(`🔍 Reviewing ${fileName}...`);
    
    const response = query({
      prompt: `Review this code for:
      1. Best practices and code quality
      2. Potential bugs and security issues
      3. Performance optimizations
      4. Code maintainability
      
      File: ${fileName}
      Language: ${getLanguageFromExtension(filePath)}
      
      Code:
      \`\`\`
      ${code}
      \`\`\``,
      options
    });
    
    let reviewText = '';
    for await (const message of response) {
      if (message.type === 'assistant') {
        const content = message.message.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (block.type === 'text') {
              reviewText += block.text;
            }
          }
        }
      }
    }
    
    // Parse the review response - try to extract JSON from the response
    let reviewData;
    try {
      // Try to find JSON in the response (look for {})
      const jsonMatch = reviewText.match(/\{[\s\S]*\}/);
      if (jsonMatch) {
        reviewData = JSON.parse(jsonMatch[0]);
      } else {
        // If no JSON found, create a simple structure
        reviewData = {
          issues: [{
            severity: 'info',
            message: reviewText.substring(0, 200) + '...',
            suggestion: 'Review the full response for detailed feedback'
          }],
          best_practices: [],
          improvements: []
        };
      }
    } catch (e) {
      // Fallback if JSON parsing fails
      reviewData = {
        issues: [{
          severity: 'info',
          message: 'Could not parse structured review. Raw response: ' + reviewText.substring(0, 200) + '...',
          suggestion: 'Check the full response for detailed feedback'
        }],
        best_practices: [],
        improvements: []
      };
    }
    
    // Format the result
    const result: CodeReviewResult = {
      file: fileName,
      issues: reviewData.issues || [],
      summary: {
        total_issues: reviewData.issues?.length || 0,
        errors: reviewData.issues?.filter((i: any) => i.severity === 'error').length || 0,
        warnings: reviewData.issues?.filter((i: any) => i.severity === 'warning').length || 0,
        info: reviewData.issues?.filter((i: any) => i.severity === 'info').length || 0
      }
    };
    
    // Display results
    console.log('\n📋 Code Review Results:');
    console.log('='.repeat(50));
    console.log(`File: ${result.file}`);
    console.log(`Total Issues: ${result.summary.total_issues}`);
    console.log(`  ❌ Errors: ${result.summary.errors}`);
    console.log(`  ⚠️  Warnings: ${result.summary.warnings}`);
    console.log(`  ℹ️  Info: ${result.summary.info}`);
    
    if (result.issues.length > 0) {
      console.log('\nIssues Found:');
      result.issues.forEach((issue, idx) => {
        const icon = issue.severity === 'error' ? '❌' : 
                    issue.severity === 'warning' ? '⚠️' : 'ℹ️';
        console.log(`\n${idx + 1}. ${icon} [${issue.severity.toUpperCase()}]`);
        if (issue.line) console.log(`   Line ${issue.line}`);
        console.log(`   ${issue.message}`);
        if (issue.suggestion) {
          console.log(`   💡 Suggestion: ${issue.suggestion}`);
        }
      });
    }
    
    if (reviewData.best_practices?.length > 0) {
      console.log('\n✅ Best Practices Followed:');
      reviewData.best_practices.forEach((practice: string) => {
        console.log(`  • ${practice}`);
      });
    }
    
    if (reviewData.improvements?.length > 0) {
      console.log('\n🚀 Suggested Improvements:');
      reviewData.improvements.forEach((improvement: string) => {
        console.log(`  • ${improvement}`);
      });
    }
    
    console.log('\n' + '='.repeat(50));
    console.log('📊 Review session traced in Arize');
    
    return result;
  } catch (error) {
    console.error('❌ Review failed:', error);
    throw error;
  }
}

function getLanguageFromExtension(filePath: string): string {
  const ext = filePath.split('.').pop()?.toLowerCase();
  const langMap: Record<string, string> = {
    'ts': 'TypeScript',
    'tsx': 'TypeScript React',
    'js': 'JavaScript',
    'jsx': 'JavaScript React',
    'py': 'Python',
    'java': 'Java',
    'go': 'Go',
    'rs': 'Rust',
    'cpp': 'C++',
    'c': 'C',
    'cs': 'C#',
    'rb': 'Ruby',
    'php': 'PHP',
    'swift': 'Swift',
    'kt': 'Kotlin'
  };
  return langMap[ext || ''] || 'Unknown';
}

// CLI interface
const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.log('Usage: tsx code-review.ts <file-path>');
    console.log('Example: tsx code-review.ts ./src/api/auth.ts');
    process.exit(1);
  }

  const filePath = args[0];
  reviewCode(filePath)
    .then(() => process.exit(0))
    .catch(() => process.exit(1));
}

export { reviewCode, CodeReviewResult };