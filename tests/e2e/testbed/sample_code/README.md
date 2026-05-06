# Sample Code

A minimal Python application for testing dev-agent-lens pipeline validation.

## Structure

- `main.py` - Entry point with basic function calls
- `utils/` - Helper module with utility functions
  - `__init__.py` - Module exports
  - `helpers.py` - Greeting and calculation functions

## Purpose

This code exists to be read and explored by Claude Code during automated
testing. It provides predictable content for validating that:

1. File read operations are captured in traces
2. Explore subagents can navigate the directory structure
3. The observability pipeline correctly records tool usage
