# Analyze a Claude Code Session

Help the user find and analyze their Claude Code sessions based on what they remember about the conversation.

## How This Works

The user will describe a session semantically - e.g., "the conversation where I was debugging authentication" or "my session about refactoring the database layer". Your job is to search through their sessions to find the right one.

## Session Storage

Sessions are stored in `~/.claude/projects/`:
- `{encoded-project-path}/{session-id}.jsonl` - Main sessions
- `{encoded-project-path}/agent-{id}.jsonl` - Subagent traces (ignore these when searching)

Project paths use dashes: `/Users/me/myproject` → `-Users-me-myproject`

## Step 1: Search for the Session

Based on the user's description, search session content to find matching conversations:

```bash
# Search for keywords from their description
grep -l "authentication" ~/.claude/projects/*/*.jsonl | grep -v agent-

# Search multiple terms
grep -l "database\|refactor" ~/.claude/projects/*/*.jsonl | grep -v agent-

# Find recent sessions if they say "recent" or "yesterday"
ls -lt ~/.claude/projects/*/*.jsonl | grep -v agent- | head -20
```

If you find multiple matches, show them to the user and ask which one. You can peek at the first message to help identify sessions:

```bash
head -5 <session-file> | grep -o '"content":"[^"]*"' | head -1
```

## Step 2: Export to Markdown

Once you've identified the session:

```bash
dal claude-session-logs-to-markdown <session-path> -o /tmp/session-analysis/
```

## Step 3: Read and Analyze

Read the exported markdown files and provide:

1. **Session Summary**: What was the main task? What was accomplished?
2. **Tool Usage**: Which tools were used? Any patterns or inefficiencies?
3. **Error Analysis**: Were there errors? How were they resolved?
4. **Subagent Activity**: What work was delegated? How did subagents perform?
5. **Recommendations**: What could improve future sessions?

## Examples

User: "Analyze my session where I was fixing the login bug"
→ Search for "login", "auth", "bug", "fix" in session files

User: "Look at the conversation from this morning about API refactoring"
→ List recent sessions, search for "API", "refactor"

User: "Find where I was working on the markdown export feature"
→ Search for "markdown", "export" in sessions

## Reference

- Session format details: `docs/claude_code_session_storage.md`
- Export format: `docs/unified_markdown_format.md`
