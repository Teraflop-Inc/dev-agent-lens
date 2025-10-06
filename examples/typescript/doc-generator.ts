#!/usr/bin/env node
/**
 * Documentation Generator Example
 * Generates API documentation for TypeScript files
 */

import { ClaudeSDKClient, ClaudeCodeOptions } from '@anthropic-ai/claude-agent-sdk';
import { readdirSync, readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { join, basename } from 'path';
import dotenv from 'dotenv';

// Load environment variables
dotenv.config();

interface DocGenerationOptions {
  sourceDir: string;
  outputDir?: string;
  filePattern?: RegExp;
  format?: 'markdown' | 'json';
}

async function generateDocs(options: DocGenerationOptions) {
  const {
    sourceDir,
    outputDir = join(sourceDir, 'docs'),
    filePattern = /\.(ts|js)$/,
    format = 'markdown'
  } = options;

  // Ensure output directory exists
  if (!existsSync(outputDir)) {
    mkdirSync(outputDir, { recursive: true });
    console.log(`📁 Created output directory: ${outputDir}`);
  }

  const client = new ClaudeSDKClient({
    apiKey: process.env.ANTHROPIC_API_KEY,
    baseUrl: 'http://localhost:8082',
    // Using default model - no model parameter specified
    systemPrompt: `You are a technical documentation expert. Generate comprehensive API documentation.
    
    For each file, document:
    - Overview and purpose
    - Exported functions with parameters and return types
    - Exported classes and their methods
    - Exported interfaces and types
    - Usage examples
    - Important notes or caveats
    
    Format the output as clean, well-structured ${format === 'markdown' ? 'Markdown' : 'JSON'}.`,
    outputFormat: 'text',
    maxTurns: 1
  });
  
  try {
    await client.start();
    console.log('📚 Documentation Generator Started');
    console.log(`📂 Source: ${sourceDir}`);
    console.log(`📝 Output: ${outputDir}`);
    console.log('');
    
    // Get all files matching pattern
    const files = readdirSync(sourceDir).filter(f => filePattern.test(f));
    
    if (files.length === 0) {
      console.log('⚠️  No files found matching pattern');
      await client.close();
      return;
    }
    
    console.log(`Found ${files.length} file(s) to document\n`);
    
    const results = [];
    
    for (const file of files) {
      const filePath = join(sourceDir, file);
      const content = readFileSync(filePath, 'utf-8');
      
      console.log(`📄 Generating docs for ${file}...`);
      
      const prompt = `Generate ${format} documentation for this ${getLanguageFromFile(file)} file:
      
      File: ${file}
      
      Code:
      \`\`\`${getLanguageFromFile(file).toLowerCase()}
      ${content}
      \`\`\``;
      
      const docs = await client.query(prompt);
      
      // Save documentation
      const outputFileName = format === 'markdown' 
        ? `${basename(file, getExtension(file))}.md`
        : `${basename(file, getExtension(file))}.json`;
      const outputPath = join(outputDir, outputFileName);
      
      if (format === 'json') {
        // Parse and format JSON
        try {
          const jsonDocs = JSON.parse(docs);
          writeFileSync(outputPath, JSON.stringify(jsonDocs, null, 2));
        } catch {
          // If not valid JSON, wrap in object
          writeFileSync(outputPath, JSON.stringify({ documentation: docs }, null, 2));
        }
      } else {
        writeFileSync(outputPath, docs);
      }
      
      console.log(`   ✅ Saved to ${outputFileName}`);
      results.push({ file, outputPath });
      
      // Each file's documentation generation is traced in Arize
    }
    
    await client.close();
    
    // Generate index file
    if (format === 'markdown') {
      const indexContent = `# API Documentation\n\nGenerated on ${new Date().toISOString()}\n\n## Files\n\n${
        results.map(r => `- [${r.file}](./${basename(r.outputPath)})`).join('\n')
      }\n\n---\n\n*Generated with Claude Agent SDK + Dev-Agent-Lens observability*`;
      
      writeFileSync(join(outputDir, 'README.md'), indexContent);
      console.log('\n📋 Generated index: README.md');
    }
    
    console.log('\n✨ Documentation generation complete!');
    console.log(`📊 ${results.length} files documented`);
    console.log('🔍 Session traced in Arize dashboard');
    
    return results;
  } catch (error) {
    console.error('❌ Documentation generation failed:', error);
    throw error;
  }
}

function getLanguageFromFile(filename: string): string {
  const ext = getExtension(filename);
  const langMap: Record<string, string> = {
    '.ts': 'TypeScript',
    '.tsx': 'TypeScript',
    '.js': 'JavaScript',
    '.jsx': 'JavaScript',
    '.mjs': 'JavaScript',
    '.cjs': 'JavaScript'
  };
  return langMap[ext] || 'JavaScript';
}

function getExtension(filename: string): string {
  const match = filename.match(/\.[^.]+$/);
  return match ? match[0] : '';
}

// CLI interface
const isMainModule = import.meta.url === `file://${process.argv[1]}`;
if (isMainModule) {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.log('Usage: tsx doc-generator.ts <source-dir> [output-dir] [--json]');
    console.log('Examples:');
    console.log('  tsx doc-generator.ts ./src');
    console.log('  tsx doc-generator.ts ./src ./docs');
    console.log('  tsx doc-generator.ts ./src ./api-docs --json');
    process.exit(1);
  }

  const sourceDir = args[0];
  const outputDir = args[1] && !args[1].startsWith('--') ? args[1] : undefined;
  const format = args.includes('--json') ? 'json' : 'markdown';

  generateDocs({ sourceDir, outputDir, format })
    .then(() => process.exit(0))
    .catch(() => process.exit(1));
}

export { generateDocs, DocGenerationOptions };