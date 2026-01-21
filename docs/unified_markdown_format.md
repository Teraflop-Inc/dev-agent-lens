# AGREED FORMAT: Unified Markdown Export Specification

**Version**: 1.3
**Agreed By**: Agent A (Claude Session Specialist) & Agent B (LiteLLM Pipeline Specialist)
**Date**: 2026-01-19
**Updated**: 2026-01-20 (v1.1: moved timestamps to PIPELINE_SPECIFIC; v1.2: added compaction handling; v1.3: compactions MUST be inline/chronological)

---

## Overview

This document specifies the exact format for exporting Claude Code conversations to markdown. Both the Claude Session pipeline and the LiteLLM/Phoenix pipeline MUST produce output conforming to this specification.

**Design Principles**:
1. **Deterministic**: Same input always produces identical output
2. **Exact string matching**: Unit tests use `assert output == expected`
3. **Pipeline-specific isolation**: Fields that differ between pipelines are in marked sections

---

## File Structure

```
{session_id}_export/
├── {session_id}.md                    # Main conversation
├── subagent_explore_1.md              # Subagent files (named by type + sequence)
├── subagent_general_purpose_1.md
├── compaction_1_summary.txt            # Compaction summaries (if >500 chars)
├── compaction_2_summary.txt
└── tool_results/                       # Large tool results (>2000 chars)
    ├── 001_read.txt
    ├── 002_bash.txt
    └── 003_grep.txt
```

### File Naming Conventions

| File Type | Naming Rule | Example |
|-----------|-------------|---------|
| Main session | `{full_36_char_uuid}.md` | `a2c1f62a-5ee0-4a49-9187-9c2130d8deac.md` |
| Subagent | `subagent_{type}_{sequence}.md` | `subagent_explore_1.md` |
| Compaction summary | `compaction_{n}_summary.txt` | `compaction_1_summary.txt` |
| Tool result | `tool_results/{nnn}_{tool_name}.txt` | `tool_results/001_read.txt` |

### Subagent Type Normalization

Transform the `subagent_type` field from Task tool input:
- Lowercase
- Replace `-` with `_`
- Replace spaces with `_`

| Input | Output |
|-------|--------|
| `Explore` | `explore` |
| `general-purpose` | `general_purpose` |
| `Plan` | `plan` |
| `claude-code-guide` | `claude_code_guide` |

### Subagent Sequence Numbering

- 1-indexed
- Per-type counter (first Explore = 1, second Explore = 2)
- Order by first appearance in conversation (chronological)

---

## Main Conversation Format

```markdown
# Session: {session_id_first_8_chars}

## Metadata

- **Session ID**: `{full_36_char_uuid}`

<!-- BEGIN PIPELINE_SPECIFIC -->
{pipeline_specific_content}
<!-- END PIPELINE_SPECIFIC -->

---

## Conversation

{conversation_content}

---

*Exported from session `{session_id}`*
*{N} user turns, {M} assistant turns, {P} tool calls, {Q} subagents*
```

**NOTE**: Timestamps (Started/Ended) are now inside PIPELINE_SPECIFIC because Phoenix and Claude JSONL have fundamentally different timestamp sources that cannot be reconciled.

### PIPELINE_SPECIFIC Content

**Claude Pipeline**:
```markdown
<!-- BEGIN PIPELINE_SPECIFIC -->
- **Started**: {YYYY-MM-DD HH:MM:SS UTC} (Claude only)
- **Ended**: {YYYY-MM-DD HH:MM:SS UTC} (Claude only)
- **Project**: `{cwd_path}` (Claude only)
- **Branch**: `{git_branch_or_no_branch}` (Claude only)
- **Summary**: {summary_or_no_summary} (Claude only)
<!-- END PIPELINE_SPECIFIC -->
```

**LiteLLM Pipeline**:
```markdown
<!-- BEGIN PIPELINE_SPECIFIC -->
- **Started**: {YYYY-MM-DD HH:MM:SS UTC} (LiteLLM only)
- **Ended**: {YYYY-MM-DD HH:MM:SS UTC} (LiteLLM only)
- **Tokens**: {count} (LiteLLM only)
- **Models Used**: {comma_list} (LiteLLM only)
<!-- END PIPELINE_SPECIFIC -->
```

**Empty values**:
- No branch: `*[No branch]*`
- No summary: `*[No summary]*`

**IMPORTANT**: The `<!-- BEGIN PIPELINE_SPECIFIC -->` and `<!-- END PIPELINE_SPECIFIC -->` markers MUST always be present, even if the section is empty.

---

## Conversation Elements

### User Message

```markdown
### User

{exact_user_message_content}

---
```

### Assistant Message

```markdown
### Assistant

{assistant_text_content}

---
```

### Single Tool Call

```markdown
### Tool: {ToolName}

**Input**:
```text
{key1}: {value1}
{key2}: {value2}
```

**Result** ({char_count} chars):
```{language_hint}
{result_content}
```

---
```

**Tool input formatting rules**:
- One line per key-value pair
- Keys in alphabetical order
- Format: `{key}: {value}`
- Values exceeding 200 chars: show first 197 + `...`

**Language hints**:
| Tool | Input Language | Result Language |
|------|----------------|-----------------|
| Read | `text` | Infer from file extension |
| Write | `text` | Infer from file extension |
| Edit | `text` | Infer from file extension |
| Bash | `text` | `bash` |
| Grep | `text` | `text` |
| Glob | `text` | `text` |
| Others | `text` | `text` |

**File extension to language**:
| Extension | Language |
|-----------|----------|
| `.py` | `python` |
| `.js` | `javascript` |
| `.ts` | `typescript` |
| `.json` | `json` |
| `.md` | `markdown` |
| `.sh` | `bash` |
| `.yaml`, `.yml` | `yaml` |
| Others | `text` |

### Parallel Tool Calls

When multiple `tool_use` blocks appear in a single assistant message:

```markdown
### Parallel Tools ({N} calls)

| # | Tool | Target |
|---|------|--------|
| 1 | {ToolName} | {brief_target} |
| 2 | {ToolName} | {brief_target} |
| N | {ToolName} | {brief_target} |

**Results**:

**[1]** ({char_count} chars):
```{language}
{result_content}
```

**[2]** ({char_count} chars):
```{language}
{result_content}
```

---
```

**Target field by tool**:
| Tool | Target Content |
|------|----------------|
| Read | File path (truncate to 60 chars with `...` prefix if longer) |
| Write | File path |
| Edit | File path |
| Bash | First 50 chars of command |
| Grep | `{pattern}` in `{path}` |
| Glob | `{pattern}` in `{path}` |
| Task | `{subagent_type}: {first_30_chars_of_description}` |
| Others | First 50 chars of first input value |

**Ordering**: By array index in `message.content` (as they appear in source data)

### Subagent Section (in Main Conversation)

```markdown
### Subagent: {type}

**Task**: {description_field}
**Prompt** (first 200 chars):
> {first_197_chars_of_prompt}...

**Result Summary** (first 500 chars):
> {first_497_chars_of_response}...

→ Full conversation: [subagent_{type}_{n}.md](./subagent_{type}_{n}.md)

---
```

**Notes**:
- `{type}` is the normalized subagent type
- Prompt preview: 200 chars max (197 + `...` if truncated)
- Result summary: 500 chars max (497 + `...` if truncated)
- If prompt ≤ 200 chars, show full prompt without `...`
- If result ≤ 500 chars, show full result without `...`

### Tool Result Linking (Large Results)

When tool result exceeds 2000 chars:

```markdown
**Result** ({char_count} chars):
{first_497_chars}...

→ Full result: [tool_results/{nnn}_{tool_name}.txt](./tool_results/{nnn}_{tool_name}.txt)
```

---

## Subagent File Format

```markdown
# Subagent: {type} (subagent_{type}_{n})

## Context

- **Parent Session**: `{parent_session_id}`
- **Started**: {YYYY-MM-DD HH:MM:SS UTC}
- **Ended**: {YYYY-MM-DD HH:MM:SS UTC}

<!-- BEGIN PIPELINE_SPECIFIC -->
{pipeline_specific_content}
<!-- END PIPELINE_SPECIFIC -->

## Task Prompt

{full_task_prompt}

---

## Conversation

{subagent_conversation_OR_summary_only}

---

*Subagent of session `{parent_session_id}`*
```

### PIPELINE_SPECIFIC for Subagent Files

**Claude Pipeline**:
```markdown
<!-- BEGIN PIPELINE_SPECIFIC -->
- **Agent ID**: `{agent_id}` (Claude only)
<!-- END PIPELINE_SPECIFIC -->
```

**LiteLLM Pipeline**:
```markdown
<!-- BEGIN PIPELINE_SPECIFIC -->
- **Duration**: {seconds}s (LiteLLM only)
- **Tokens**: {count} (LiteLLM only)
- **Tool Calls**: {count} (LiteLLM only)
<!-- END PIPELINE_SPECIFIC -->
```

### Subagent Conversation Section

**When full conversation is available**:
```markdown
## Conversation

### Assistant

{subagent_first_response}

---

### Tool: {ToolName}

{tool_details}

---

### Assistant

{subagent_next_response}

---
```

**When full conversation is NOT available (LiteLLM ~20% case)**:
```markdown
## Conversation

*[Full conversation not available - showing response summary only]*

### Response Summary

{tool_result_content_from_parent}

---
```

---

## Compaction Handling

Both pipelines can now detect compaction:
- **Claude JSONL**: Explicit `type: "system"` + `subtype: "compact_boundary"` message with `compactMetadata`
- **LiteLLM**: `COMPACTION_CONTINUATION_MARKER` text pattern in span input

### Compaction Placement (CRITICAL)

**Compactions MUST appear inline at their chronological position in the conversation.**

❌ **WRONG**: Front-loading all compactions at the top of the file before the conversation
✅ **CORRECT**: Each compaction appears at the point in the conversation where it occurred

This preserves the narrative flow as the agent experienced it. A reader should be able to follow the conversation chronologically, encountering compactions at the moments context was compressed.

**Example of correct ordering**:
```
### User
First message...

### Assistant
First response...

### Compaction #1
[Summary of context compressed here]

### User
Message after first compaction...

### Compaction #2
[Summary of context compressed here]

### User
Message after second compaction...
```

### Compaction Boundary (in Main Conversation)

```markdown
### Compaction #{n}

<!-- BEGIN PIPELINE_SPECIFIC -->
- **Trigger**: {auto|manual} (Claude only)
- **Pre-compaction tokens**: {count} (Claude only)
<!-- END PIPELINE_SPECIFIC -->

> **Context Summary**:
> {first_497_chars_of_summary}...

→ Full summary: [compaction_{n}_summary.txt](./compaction_{n}_summary.txt)

---
```

**Notes**:
- `{n}` is 1-indexed compaction counter
- Summary comes from the `user` message immediately following the `compact_boundary`
- Summary preview: 500 chars max (497 + `...` if truncated)
- If summary ≤ 500 chars, show full summary without link to external file
- Trigger and pre_tokens are in PIPELINE_SPECIFIC (LiteLLM may not have these)

### Compaction Detection

| Pipeline | Detection Method |
|----------|------------------|
| Claude | `type: "system"` + `subtype: "compact_boundary"` |
| LiteLLM | `COMPACTION_CONTINUATION_MARKER` text pattern |

### Compaction Summary Source

| Pipeline | Summary Source |
|----------|----------------|
| Claude | `user` message immediately after `compact_boundary` |
| LiteLLM | Text after continuation marker in span input |

### Footer Stats (when compactions present)

```markdown
*{N} user turns, {M} assistant turns, {P} tool calls, {Q} subagents, {R} compactions*
```

---

## Tool Result File Format

For results exceeding 2000 chars:

```
{full_untruncated_result}
```

Plain text file, no markdown formatting, no header. Just the raw result.

---

## Exact Thresholds

| Content Type | Inline Limit | Truncation |
|--------------|--------------|------------|
| Tool result inline | 500 chars | Show 497 + `...` |
| Tool result external file threshold | 2000 chars | Create file, show 497 + `...` inline |
| Subagent prompt preview | 200 chars | Show 197 + `...` |
| Subagent response summary | 500 chars | Show 497 + `...` |
| Tool input value (per key) | 200 chars | Show 197 + `...` |
| Parallel tool target | 60 chars | Show with `...` prefix for file paths |

---

## Canonical Ordering Rules

| Element | Ordering |
|---------|----------|
| Messages | Chronological by timestamp (ISO-8601 string sort) |
| Parallel tool calls | By array index in source |
| Parallel tool results | Same order as calls |
| Tool input keys | Alphabetical |
| Subagent files (listing) | Alphabetical by filename |
| External tool result files | By sequence number |

---

## Whitespace and Formatting

1. **Section separator**: Exactly `---` on its own line
2. **Blank lines**: Exactly 1 blank line before `---` separators
3. **No trailing whitespace**: Lines must not end with spaces
4. **File ending**: Single `\n` at end of file
5. **Line endings**: Unix `\n` (not Windows `\r\n`)
6. **Indentation**: None at top level; code blocks have content indentation as-is

---

## Empty/Missing Value Handling

| Field | If Empty/Missing | Output |
|-------|------------------|--------|
| Git branch | null or missing | `*[No branch]*` |
| Session summary | no summary message | `*[No summary]*` |
| Tool result | empty string | `*[Empty result]*` |
| User message content | empty | Skip message entirely |
| Assistant message content | empty | Skip message entirely |
| Assistant message with only tool_use | No `### Assistant` section, just tool sections |

---

## Unit Test Strategy

### Files Compared Exactly (after PIPELINE_SPECIFIC stripping)

- Main session file (`{session_id}.md`)

### Files NOT Required to Match Exactly

- Subagent files (`subagent_*.md`) - Content may differ due to LiteLLM's 80% full/20% summary-only variance
- Tool result files (`tool_results/*.txt`) - Should match, but are supplementary

### Test Process

1. Strip content between `<!-- BEGIN PIPELINE_SPECIFIC -->` and `<!-- END PIPELINE_SPECIFIC -->` (inclusive)
2. Compare remaining content with exact string match
3. For subagent files: Compare structure, allow content differences with warning

---

## Sample Complete Output

### Input (JSONL snippet)

```json
{"type":"summary","summary":"Fix login bug","sessionId":"a2c1f62a-5ee0-4a49-9187-9c2130d8deac"}
{"type":"user","uuid":"u1","message":{"content":"Help me fix the login bug"},"timestamp":"2026-01-19T10:00:00.000Z","cwd":"/project","gitBranch":"main","sessionId":"a2c1f62a-5ee0-4a49-9187-9c2130d8deac"}
{"type":"assistant","uuid":"a1","parentUuid":"u1","message":{"content":[{"type":"text","text":"I'll read the auth file."},{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/project/auth.py"}}]},"timestamp":"2026-01-19T10:00:05.000Z","sessionId":"a2c1f62a-5ee0-4a49-9187-9c2130d8deac"}
{"type":"user","uuid":"u2","parentUuid":"a1","toolUseResult":"def login():\n    return True","timestamp":"2026-01-19T10:00:06.000Z","sessionId":"a2c1f62a-5ee0-4a49-9187-9c2130d8deac"}
{"type":"assistant","uuid":"a2","parentUuid":"u2","message":{"content":[{"type":"text","text":"Found the bug. The login function always returns True."}]},"timestamp":"2026-01-19T10:00:10.000Z","sessionId":"a2c1f62a-5ee0-4a49-9187-9c2130d8deac"}
```

### Output (Main File)

```markdown
# Session: a2c1f62a

## Metadata

- **Session ID**: `a2c1f62a-5ee0-4a49-9187-9c2130d8deac`
- **Started**: 2026-01-19 10:00:00 UTC
- **Ended**: 2026-01-19 10:00:10 UTC

<!-- BEGIN PIPELINE_SPECIFIC -->
- **Project**: `/project` (Claude only)
- **Branch**: `main` (Claude only)
- **Summary**: Fix login bug (Claude only)
<!-- END PIPELINE_SPECIFIC -->

---

## Conversation

### User

Help me fix the login bug

---

### Assistant

I'll read the auth file.

---

### Tool: Read

**Input**:
```text
file_path: /project/auth.py
```

**Result** (28 chars):
```python
def login():
    return True
```

---

### Assistant

Found the bug. The login function always returns True.

---

*Exported from session `a2c1f62a-5ee0-4a49-9187-9c2130d8deac`*
*1 user turns, 2 assistant turns, 1 tool calls, 0 subagents*
```

---

## Acceptance Criteria

1. ✅ Both pipelines produce structurally identical output
2. ✅ PIPELINE_SPECIFIC sections clearly marked
3. ✅ After stripping PIPELINE_SPECIFIC, outputs match exactly
4. ✅ Subagent files generated (content may vary)
5. ✅ Large tool results externalized to files
6. ✅ All thresholds and ordering rules followed

---

*Format specification agreed by Agent A and Agent B on 2026-01-19*
