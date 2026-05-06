# Quickstart: Exporting Claude Code Sessions to Markdown

This guide shows how to export Claude Code session traces to human-readable markdown for analysis, review, or sharing with AI agents.

---

## Prerequisites

- Claude Code sessions stored in `~/.claude/projects/`
- `dal` CLI installed (`uv sync` in the repo root)

---

## Option 1: Run the CLI Yourself

Export a Claude Code session JSONL file directly to markdown.

### Find Your Session

Sessions are stored at `~/.claude/projects/{encoded-project-path}/{session-id}.jsonl`

```bash
# List all projects
ls ~/.claude/projects/

# List sessions for a specific project
ls ~/.claude/projects/-Users-me-myproject/*.jsonl | grep -v agent-
```

### Export to Markdown

```bash
# Basic export (creates session.md in current directory)
dal claude-session-logs-to-markdown ~/.claude/projects/-Users-me-project/abc123.jsonl

# Export to a specific directory
dal claude-session-logs-to-markdown ~/.claude/projects/-Users-me-project/abc123.jsonl -o ./exports/

# Include file-history-snapshot entries
dal claude-session-logs-to-markdown session.jsonl --include-snapshots
```

### Output Structure

The export creates:
- `{session-id}.md` - Main conversation markdown
- `subagent_{type}_{n}.md` - One file per subagent (if any)

### Options

| Flag | Description |
|------|-------------|
| `-o, --output-dir PATH` | Output directory (default: current) |
| `--max-result-length N` | Truncate tool results to N chars (default: 1000) |
| `--no-timestamps` | Exclude timestamps from messages |
| `--include-snapshots` | Include file-history-snapshot entries |

---

## Option 2: Give the Output to an Agent

After exporting, provide the markdown to any AI agent for analysis.

### Example Workflow

1. **Export the session:**
   ```bash
   dal claude-session-logs-to-markdown ~/.claude/projects/-Users-me-project/session.jsonl -o ./exports/
   ```

2. **Give the markdown to an agent** (e.g., Claude Code, ChatGPT, etc.):
   ```
   I've attached a Claude Code session export. Please:
   1. Summarize what was accomplished
   2. Identify any errors or issues
   3. Suggest improvements for next time
   ```

3. **Attach the files:**
   - `session.md` (main conversation)
   - Any `subagent_*.md` files (if subagents were used)

### What Agents Can Do With Session Exports

- Analyze coding decisions and patterns
- Review tool usage efficiency
- Identify repeated mistakes or friction points
- Generate documentation from implementation sessions
- Create post-mortems for debugging sessions
- Extract learnings for team knowledge bases

---

## Option 3: Use a Skill (Claude Code Automation)

For Claude Code users, there's a built-in skill that automates finding and analyzing sessions.

### The Skill

See [.claude/commands/analyze-session.md](../.claude/commands/analyze-session.md)

The skill lets you describe a session semantically - the agent will search through your sessions to find it:

```
/analyze-session

# Or just describe what you're looking for:
"Find and analyze my session where I was debugging the authentication flow"
"Look at the conversation from yesterday about API refactoring"
"Analyze the session where I implemented the markdown export"
```

The agent will:
1. Search `~/.claude/projects/` for sessions matching your description
2. Export the matching session to markdown
3. Read and analyze the conversation

---

## Exporting LiteLLM/Phoenix Traces

If you're using the LiteLLM proxy with Phoenix tracing, use `chain-export` instead:

```bash
# List available chains
dal chain-list --source phoenix-local

# Export a chain to markdown
dal chain-export --source phoenix-local --index 0 --format markdown

# Export to JSONL (recommended for programmatic access)
dal chain-export --source phoenix-local --index 0 --format jsonl
```

See [sync-historical.md](sync-historical.md) for setting up trace synchronization.

---

## Markdown Format Reference

The exported markdown follows a unified format compatible with both Claude JSONL and LiteLLM traces. Key sections:

- **Header**: Session metadata (timestamps, models, tokens)
- **User/Assistant turns**: Conversation messages
- **Tool sections**: Tool calls with input/output
- **Subagent sections**: Links to subagent conversation files
- **PIPELINE_SPECIFIC blocks**: Source-specific metadata (tokens, compactions)

See [unified_markdown_format.md](unified_markdown_format.md) for full specification.

---

## Session Storage Reference

Claude Code stores sessions at `~/.claude/projects/`:

| Path Pattern | Contents |
|--------------|----------|
| `{project}/*.jsonl` | Main session files |
| `{project}/agent-*.jsonl` | Subagent traces |

Key fields for finding sessions:
- `sessionId`: Unique conversation identifier
- `parentUuid`: Message chain linking (null = first message)
- `agentId`: Subagent identifier (links agent files to main session)

See [claude_code_session_storage.md](claude_code_session_storage.md) for complete documentation.
