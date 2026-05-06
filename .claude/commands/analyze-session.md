# Analyze a Claude Code Session

Help the user find and analyze their Claude Code sessions based on what they remember about the conversation.

## How This Works

The user will describe a session semantically - e.g., "the conversation where I was debugging authentication" or "my session about refactoring the database layer". Your job is to search through their sessions to find the right one.

## Step 1: Ensure Parquet Export Exists

Check for the cached Parquet export. If missing or stale, create it:

```bash
# Check if export exists
ls -la ~/.dal/claude-sessions.parquet 2>/dev/null || dal export-events --output ~/.dal/claude-sessions.parquet
```

## Step 2: Search with DuckDB

Query the Parquet file to find matching sessions:

```bash
# Search by content keywords
duckdb -c "
SELECT DISTINCT session_id, project_path, timestamp
FROM '~/.dal/claude-sessions.parquet'
WHERE text ILIKE '%authentication%'
  AND event_type = 'user'
ORDER BY timestamp DESC
LIMIT 10
"

# Search multiple terms
duckdb -c "
SELECT DISTINCT session_id, project_path, timestamp
FROM '~/.dal/claude-sessions.parquet'
WHERE (text ILIKE '%database%' OR text ILIKE '%refactor%')
ORDER BY timestamp DESC
LIMIT 10
"

# Find recent sessions
duckdb -c "
SELECT DISTINCT session_id, project_path, MAX(timestamp) as last_msg
FROM '~/.dal/claude-sessions.parquet'
WHERE event_type = 'user'
GROUP BY session_id, project_path
ORDER BY last_msg DESC
LIMIT 20
"
```

If multiple matches, show them to the user. Preview first message to help identify:

```bash
duckdb -c "
SELECT text
FROM '~/.dal/claude-sessions.parquet'
WHERE session_id = '<session_id>' AND event_type = 'user'
ORDER BY timestamp
LIMIT 1
"
```

## Step 3: Locate JSONL and Export

The `project_path` from DuckDB maps to the JSONL location:

```bash
# JSONL path pattern
~/.claude/projects/{encoded-project-path}/{session_id}.jsonl

# Export to markdown
dal claude-session-logs-to-markdown <session-jsonl-path> -o /tmp/session-analysis/
```

## Step 4: Read and Analyze

Read the exported markdown files and provide:

1. **Session Summary**: What was the main task? What was accomplished?
2. **Tool Usage**: Which tools were used? Any patterns or inefficiencies?
3. **Error Analysis**: Were there errors? How were they resolved?
4. **Subagent Activity**: What work was delegated? How did subagents perform?
5. **Recommendations**: What could improve future sessions?

## Examples

User: "Analyze my session where I was fixing the login bug"
-> Query Parquet for "login", "auth", "bug", "fix"

User: "Look at the conversation from this morning about API refactoring"
-> Query recent sessions, filter by "API", "refactor"

## Reference

- Session format details: `docs/claude_code_session_storage.md`
- Export format: `docs/unified_markdown_format.md`
