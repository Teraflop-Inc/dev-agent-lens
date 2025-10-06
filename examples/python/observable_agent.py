#!/usr/bin/env python3
"""
Observable Agent Example
Advanced Claude agent with session management and full observability
"""

import asyncio
import json
import os
from typing import Dict, Any, Optional
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

class ObservableClaudeAgent:
    """Claude agent with full observability through Dev-Agent-Lens."""
    
    def __init__(self, agent_type: str = 'general'):
        """
        Initialize the agent with a specific type.

        Args:
            agent_type: Type of agent ('sre', 'security', 'performance', 'general')
        """
        self.agent_type = agent_type
        self.options = ClaudeAgentOptions(
            system_prompt=self._get_system_prompt(agent_type),
            max_turns=10
        )
        self.client = None
        self.session_history = []
    
    def _get_system_prompt(self, agent_type: str) -> str:
        """Get the appropriate system prompt for the agent type."""
        prompts = {
            'sre': '''You are an SRE specialist focused on incident response.
                     Analyze issues, provide root cause analysis, and suggest remediation.
                     Return structured JSON responses.''',
            'security': '''You are a security engineer reviewing code for vulnerabilities.
                          Identify security issues, rate their severity, and provide fixes.
                          Return structured JSON responses.''',
            'performance': '''You are a performance engineer optimizing applications.
                            Analyze performance bottlenecks and suggest optimizations.
                            Return structured JSON responses.''',
            'general': '''You are a helpful development assistant.
                         Provide clear, actionable advice.
                         Return structured JSON responses.'''
        }
        return prompts.get(agent_type, prompts['general'])
    
    async def start(self):
        """Initialize the Claude client."""
        self.client = ClaudeSDKClient(self.options)
        await self.client.connect()
        print(f'🚀 {self.agent_type.upper()} Agent started')
        # Session start is logged in Arize
    
    async def analyze(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Analyze a query with full observability.
        
        Args:
            query: The analysis query
            context: Optional context data
        
        Returns:
            Analysis results as a dictionary
        """
        if not self.client:
            await self.start()
        
        # Build the full query with context
        full_query = query
        if context:
            full_query = f"{query}\n\nContext:\n{json.dumps(context, indent=2)}"
        
        # Track in session history
        self.session_history.append({
            'timestamp': datetime.now().isoformat(),
            'query': query,
            'context': context
        })
        
        print(f'🔍 Analyzing: {query[:100]}...')
        
        # Send query (traced in Arize)
        await self.client.query(full_query)
        
        # Collect full response
        response_parts = []
        async for message in self.client.receive_response():
            # Handle different message types
            if hasattr(message, 'content'):
                if isinstance(message.content, list):
                    for block in message.content:
                        if hasattr(block, 'text'):
                            response_parts.append(block.text)
                elif isinstance(message.content, str):
                    response_parts.append(message.content)
            elif hasattr(message, 'text'):
                response_parts.append(message.text)
            else:
                response_parts.append(str(message))
        
        # Join all response parts
        full_response = ''.join(response_parts)
        
        # Try to parse as JSON, fallback to plain text
        try:
            result = json.loads(full_response)
        except json.JSONDecodeError:
            result = {'response': full_response}
        
        # Add to history
        self.session_history[-1]['result'] = result
        
        return result
    
    async def batch_analyze(self, queries: list) -> list:
        """
        Analyze multiple queries in sequence.
        
        Args:
            queries: List of queries to analyze
        
        Returns:
            List of analysis results
        """
        results = []
        for i, query in enumerate(queries, 1):
            print(f'\n[{i}/{len(queries)}] Processing query...')
            result = await self.analyze(query)
            results.append(result)
        return results
    
    async def close(self):
        """Clean up client and save session."""
        if self.client:
            await self.client.disconnect()
            print(f'📊 {self.agent_type.upper()} Agent session closed')
            # Session end is logged in Arize
    
    def get_session_summary(self) -> Dict[str, Any]:
        """Get a summary of the current session."""
        return {
            'agent_type': self.agent_type,
            'total_queries': len(self.session_history),
            'session_history': self.session_history
        }

# Specialized agent implementations

class SecurityAnalysisAgent(ObservableClaudeAgent):
    """Specialized agent for security analysis."""
    
    def __init__(self):
        super().__init__('security')
    
    async def analyze_code_security(self, code: str, language: str = 'python') -> Dict[str, Any]:
        """Analyze code for security vulnerabilities."""
        query = f"""Analyze this {language} code for security vulnerabilities:
        
        ```{language}
        {code}
        ```
        
        Identify:
        1. Security vulnerabilities with severity ratings
        2. OWASP Top 10 issues if present
        3. Recommended fixes with code examples
        """
        return await self.analyze(query)

class IncidentResponseAgent(ObservableClaudeAgent):
    """Specialized agent for incident response."""
    
    def __init__(self):
        super().__init__('sre')
    
    async def handle_incident(self, incident_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle an incident with full analysis."""
        query = f"""Analyze this incident and provide response guidance:
        
        Service: {incident_data.get('service', 'Unknown')}
        Error: {incident_data.get('error', 'Unknown error')}
        Metrics: {json.dumps(incident_data.get('metrics', {}), indent=2)}
        
        Provide:
        1. Root cause analysis
        2. Immediate mitigation steps
        3. Long-term prevention measures
        4. Monitoring recommendations
        """
        
        result = await self.analyze(query, context=incident_data)
        
        # Add incident-specific metadata
        result['incident_id'] = incident_data.get('id', 'unknown')
        result['severity'] = self._calculate_severity(incident_data)
        
        return result
    
    def _calculate_severity(self, incident_data: Dict[str, Any]) -> str:
        """Calculate incident severity based on metrics."""
        error_rate = incident_data.get('metrics', {}).get('error_rate', 0)
        if isinstance(error_rate, str):
            error_rate = float(error_rate.rstrip('%'))
        
        if error_rate > 10:
            return 'critical'
        elif error_rate > 5:
            return 'high'
        elif error_rate > 1:
            return 'medium'
        else:
            return 'low'

# Example usage functions

async def security_analysis_example():
    """Example of security analysis."""
    agent = SecurityAnalysisAgent()
    
    try:
        # Sample vulnerable code
        vulnerable_code = """
import sqlite3
from flask import request

def get_user(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    # SQL injection vulnerability
    query = f"SELECT * FROM users WHERE id = {user_id}"
    cursor.execute(query)
    return cursor.fetchone()

def store_password(password):
    # Weak password storage
    return hashlib.md5(password.encode()).hexdigest()
        """
        
        result = await agent.analyze_code_security(vulnerable_code)
        
        print('\n🔒 Security Analysis Results:')
        print(json.dumps(result, indent=2))
        
        # All interactions are visible in Arize dashboard
    finally:
        await agent.close()

async def incident_response_example():
    """Example of incident response."""
    agent = IncidentResponseAgent()
    
    try:
        incident = {
            'id': 'INC-2024-001',
            'service': 'api-gateway',
            'error': 'High latency and increased error rate detected',
            'metrics': {
                'p99_latency': '5000ms',
                'error_rate': '2.5%',
                'requests_per_second': 1000,
                'cpu_usage': '85%',
                'memory_usage': '92%'
            },
            'timestamp': datetime.now().isoformat()
        }
        
        result = await agent.handle_incident(incident)
        
        print('\n🚨 Incident Response:')
        print(f"Incident ID: {result['incident_id']}")
        print(f"Severity: {result['severity'].upper()}")
        print('\nAnalysis:')
        print(json.dumps({k: v for k, v in result.items() 
                         if k not in ['incident_id', 'severity']}, indent=2))
        
    finally:
        await agent.close()

async def main():
    """Main entry point for examples."""
    print('Observable Claude Agent Examples')
    print('=' * 50)
    
    # Run examples
    print('\n1. Security Analysis Example')
    print('-' * 30)
    await security_analysis_example()
    
    print('\n2. Incident Response Example')
    print('-' * 30)
    await incident_response_example()
    
    print('\n' + '=' * 50)
    print('✅ All examples completed')
    print('📊 View traces in Arize: https://app.arize.com')

if __name__ == '__main__':
    asyncio.run(main())