# Technical Debt: Markdown Export Pipeline

**Created**: 2026-01-21
**Context**: Extracted from AGENT_GUIDE_MARKDOWN_EXPORT.md, filtered for v2 (event-based JSONL) pipeline relevance.

---

## 1. chains.py has zero tests (HIGH RISK)

**File**: `dev_agent_lens/analysis/chains.py` (3,331 lines)

This file contains `export_chain_to_jsonl()` - our canonical event-based export function. It has no test coverage.

**Risk**: Any regression in this function breaks the entire LiteLLM export pipeline.

**Recommended action**: Add unit tests for:
- `export_chain_to_jsonl()` - basic conversation export
- `_extract_ordered_messages()` - message ordering logic
- `_extract_cumulative_messages_from_raw_span()` - diff-based extraction
- Edge cases: empty chains, compactions, subagents

---

## 2. Code duplication (DONE)

**Status**: ✅ Cleaned up on 2026-01-21

Removed duplicate `truncate()` and `normalize_subagent_type()` from:
- markdown.py → now imports from markdown_renderer
- markdown_litellm.py → now imports from markdown_renderer

Left alone:
- query/export.py - has different `truncate()` signature (intentional)

---

## 3. Silent exception handler

**File**: `dev_agent_lens/export/markdown_litellm.py:129`

```python
except Exception:
    pass
```

This is in `_parse_timestamp()`. Silent failure makes debugging timestamp issues impossible.

**Recommended action**: Add logging or return a sentinel value that can be detected.

---

## 4. Common Gotchas (Knowledge Transfer)

These are not bugs but important context for anyone working on the pipeline:

### Subagent traces are independent
Subagent execution happens in a **separate trace**, NOT as children of the Task span.
- ❌ Wrong: Traverse child spans of Task tool call
- ✅ Correct: Use `tool_result` from the Task response

### Session ID formats differ
- **Phoenix**: 32-char hex strings (`6a333d9730112aa89c13f43c68493689`)
- **Claude JSONL**: Standard UUIDs (`a651594d-4722-4c3f-993c-dc00f90e18a3`)

The Claude session UUID (in metadata) links sessions across compactions.

### Two metadata formats
```python
# Lambda2 format (dotted keys)
span["attributes.metadata.requester_metadata.user_id"]

# Local format (nested dicts)
span["attributes"]["metadata"]["requester_metadata"]["user_id"]
```

Must handle both in extraction logic.

---

## Priority

| Item | Priority | Effort | Status |
|------|----------|--------|--------|
| chains.py tests | High | Medium | TODO |
| Code duplication | Medium | Low | ✅ Done |
| Silent exception | Low | Low | TODO |
| Gotchas documentation | N/A | N/A | Documented above |
