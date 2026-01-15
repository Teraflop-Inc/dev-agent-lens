# dev-agent-lens Status Report

**Generated**: 2026-01-14
**Last Push**: 46 commits ahead of origin/main

---

## Executive Summary

The dev-agent-lens project has undergone significant development since the last push, with **46 commits** adding approximately **3,700+ lines** of new functionality. Key developments include:

1. **SQLite Direct Access for Phoenix** - Major performance improvement for historical sync
2. **Streaming NDJSON Export** - Efficient export for dense time windows
3. **Markdown Export Fixes** - Critical bug fixes for conversation export quality
4. **Analysis Module** - New conversation chain building and export functionality

---

## Recent Commits (Since Last Push)

### Infrastructure & Sync (Most Recent)
| Commit | Description |
|--------|-------------|
| `733a6c5` | feat(query): auto-detect Parquet sources for session lookups |
| `1c9eac7` | feat(sync): add streaming NDJSON export for dense time windows |
| `36cb663` | feat(sync): SQLite mode uses 500k batch limit by default |
| `a6bb499` | feat(sync): add SQLite direct access and --history flag |
| `28e3222` | feat(sync): loop until complete instead of single fetch |
| `1dacaec` | fix(sync): prevent data loss when hitting span limit |

### Documentation & Testing
| Commit | Description |
|--------|-------------|
| `88d416d` | docs: fix CLI options in README and sync-historical |
| `677a483` | docs: Add installation and clarify sync workflow |
| `914dd86` | docs: Remove internal analysis docs from repo |
| `89e0dd3` | docs: Reorganize documentation with concise README |
| `0127b11` | test(cli): add comprehensive tests for config and sync-historical |

### Core Features (Theme Implementation)
| Commit | Description |
|--------|-------------|
| `4b24fdb` | feat: Add generic PatternMatch infrastructure |
| `e3f2c08` | feat(llm): Add Theme 4 LLM Analysis Framework with Parquet support |
| `12af04d` | feat(cli): Add Parquet backend support to CLI commands (Theme 3) |
| `689a807` | feat(query): Add DuckDB-based Parquet query backend (Theme 2) |
| `112d06f` | feat(export): Add Parquet export with ZSTD compression |

---

## Active Work Areas

### 1. Markdown Export Pipeline (UNSTABLE - Needs Redesign)

**Location**: `dev_agent_lens/analysis/chains.py` (85,468 bytes)

**Status**: ⚠️ LARGELY UNUSABLE - Requires coherent acceptance criteria before continuing

**Things We've Learned**:
- "Warmup" messages are legitimate session initialization events that must be accounted for
- Message length thresholds need user-configurable toggles (when to inline vs link)
- Empty compaction sections were caused by unconditional marker placement
- Code ordering matters - filters must run in correct sequence
- Ancillary messages (routing signals) were leaking into main thread
- Raw JSON/Python literals in user messages need parsing fallbacks

**Example of Current State**:
```
v10 (broken): Assistant greeting appears first, compaction immediately after
v11 (partial): User "*[Session initialization]*" appears - but still incomplete
```

---

## User Critiques (Critical Feedback)

### The Markdown Export Is Not Working

The markdown export feature is **largely unusable and unstable**. Coding agents have struggled to handle the full feature set and cannot intuit what _should_ be happening. The fundamental problem is a lack of clear acceptance criteria.

**Core Issues**:

1. **No clear specification** - What should the output look like? What messages should appear between compactions? What's the expected user/assistant ratio?

2. **Compaction logic is too complicated** - The logic for handling context window boundaries is convoluted. We may need to:
   - Start with conversations that DON'T have compactions (we can create test data)
   - Build acceptance criteria for simple cases first
   - Then incrementally handle compaction edge cases

3. **LiteLLM wrapping may be corrupting data** - There's a suspicion that LiteLLM's wrapping of API calls is interfering with our pipeline. The raw data structure may be getting mangled before we see it.

4. **Missing first message and inter-compaction content** - The export consistently fails to show:
   - The actual first user message that triggered the conversation
   - Content that should appear between compaction markers

### Helpful Discovery: `~/.claude` Folder

The `~/.claude` folder contains actual session data (`history.jsonl`) that shows what really happened. This is a valuable ground truth source.

**Recommended debugging approach**: Coding agents in development loops should cross-reference:
1. Messages in `~/.claude/history.jsonl` for the session
2. What appears in the markdown export
3. Note: The Claude JSONL might not have everything, but it's a sanity check

### Potential Investigation Paths

1. **Human analysis on Arize platform** - Manually inspect raw trace data in the Arize UI for sessions we're trying to fix. Humans could write observations to a file, or have an agent transcribe what they see.

2. **Proxy experiment** - Send Claude Code's baseURL to a different location, proxy it separately to see if the LiteLLM + Arize combination is the issue.

3. **Self-referential debugging** - Use Claude Code to query itself programmatically:
   - Drive behavior we understand
   - Hunt through the data
   - Sync and analyze how data should flow
   - Generate acceptance criteria from observed behavior

4. **Simplify first** - Create test conversations WITHOUT compactions to establish baseline behavior, then add complexity.

---

## Plan of Attack Needed

The current approach of iterative bug-fixing is not working. We need:

1. **Clear acceptance criteria** - What does a correct export look like?
2. **User stories** - Who is using this and what do they need?
3. **Test data** - Simple conversations (no compactions) to establish baseline
4. **Ground truth comparison** - Systematic comparison against `~/.claude` data
5. **Investigation of LiteLLM/Arize pipeline** - Is data being corrupted upstream?

---

## AI Analysis Module Dependency Warning

**Important**: The entire `dev_agent_lens/analysis/` module was built assuming a different data structure than what the markdown export currently produces.

**Impact**: Once the markdown export issues are resolved, the analysis module will need refactoring to match the corrected data format. Do not assume the analysis code will work correctly until the export pipeline is stable.

**Affected components**:
- `chains.py` - Chain building and export
- `threads.py` - Thread classification
- `sessions.py` - Session analysis
- `tokens.py` - Token counting
- All dependent analysis features

---

### 2. Historical Sync Improvements

**Location**: `dev_agent_lens/core/historical_sync.py` (+563 lines), `dev_agent_lens/clients/phoenix_sqlite.py` (+761 lines)

**Status**: ✅ SQLite direct access implemented

**Features Added**:
- SQLite direct access for Phoenix (bypasses HTTP API for 10-100x speedup)
- Streaming NDJSON export for memory-efficient large exports
- 500k batch limit by default in SQLite mode
- `--history` flag for viewing sync history
- Auto-retry on rate limits with exponential backoff

**Usage**:
```bash
# SQLite mode (fast, direct DB access)
dal sync-historical --source phoenix-local-alex --sqlite

# Streaming export for dense windows
dal sync-historical --source phoenix-local-alex --streaming

# Check sync history
dal sync-historical --source phoenix-local-alex --history
```

### 3. Analysis Module (New)

**Location**: `dev_agent_lens/analysis/` (new directory)

**Files**:
| File | Size | Purpose |
|------|------|---------|
| `chains.py` | 85KB | Conversation chain building and markdown export |
| `threads.py` | 30KB | Thread classification (main vs ancillary) |
| `subsets.py` | 12KB | Subset extraction utilities |
| `tokens.py` | 10KB | Token counting and analysis |
| `sessions.py` | 9KB | Session analysis utilities |
| `failures.py` | 8KB | Failure detection and analysis |
| `classify.py` | 6KB | Span classification |
| `churn.py` | 7KB | Churn metrics |
| `aggregate.py` | 6KB | Aggregation utilities |

---

## Uncommitted Changes

### Modified Files (Staged)
| File | Status |
|------|--------|
| `dev_agent_lens/cli/main.py` | Modified |
| `dev_agent_lens/core/session.py` | Modified |
| `dev_agent_lens/core/unify.py` | Modified |

### Untracked Files (To Review)
| Category | Files |
|----------|-------|
| **Analysis Module** | `dev_agent_lens/analysis/` (entire directory) |
| **Fabric Module** | `dev_agent_lens/fabric/` |
| **Tests** | `tests/analysis/`, `tests/e2e/`, `tests/fabric/` |
| **Documentation** | `docs/markdown_export_pipeline_documentation.md`, `docs/classification_*.md`, `docs/research/` |
| **Reports** | `MARKDOWN_EXPORT_ISSUES.md`, `PARQUET_OPTIMIZATION_REPORT.md`, etc. |
| **Scripts** | Various `verify_*.py`, `analyze_*.py` files |

---

## Known Issues

### Critical (P0)
- None currently - major export bugs have been fixed

### High Priority (P1)
1. **User message filtering may be over-aggressive** - 84% of messages were filtered in initial tests; Warmup fix improves this but deduplication may still remove legitimate repeated questions
2. **Filter statistics not reported** - Hard to diagnose why messages are missing without filter counts in metadata

### Medium Priority (P2)
1. **Ancillary thread messages not included** - 78,548 ancillary turns in test conversation; may contain useful context
2. **No verbosity control** - Can't choose between minimal vs detailed export

---

## Test Results

### Export Comparison (v10 vs v11)

**Test Conversation**:
- Sessions: 3,996
- Compactions: 2,194
- Duration: 10,472 minutes (~7 days)
- Main thread turns: 1,968
- Ancillary turns: 78,548

**v10 (Before Fix)**:
```markdown
### 🤖 Assistant (claude-haiku-4-5-20251001)
I'm Claude Code, Anthropic's CLI file search specialist...

---
### 🔄 Compaction #1
*Session continued after context window limit*
```
Problem: No user message visible; assistant appears to speak unprompted

**v11 (After Fix)**:
```markdown
### 👤 User
*[Session initialization]*

### 🤖 Assistant (claude-haiku-4-5-20251001)
I'm Claude Code, Anthropic's CLI file search specialist...

### 👤 User
*[Session initialization]*

---
### 🔄 Compaction #1
*Session continued after context window limit*
```
Fixed: User initialization visible before each assistant response

---

## Critical Finding: Session ID Correlation (2026-01-15)

**The baseline test conversations from `~/.claude` are NOT in the synced Phoenix data.**

### Investigation Results

| Finding | Details |
|---------|---------|
| **Phoenix Session IDs** | 32-character hex strings (e.g., `6a333d9730112aa89c13f43c68493689`) |
| **Claude UI Session IDs** | Standard UUIDs with hyphens (e.g., `a651594d-4722-4c3f-993c-dc00f90e18a3`) |
| **Correlation** | **NONE** - These are separate tracking systems |

### Data Coverage

- **Phoenix Data**: 2025-11-25 to 2026-01-07
- **Total Spans**: 812,003
- **Total Sessions**: 15,394
- **Jan 7 2026**: 102,539 spans across 1,355 sessions

### Why Baseline Conversations Are Missing

The Phoenix data only contains traces from Claude Code sessions that were **routed through the LiteLLM proxy** (`claude-lens`). Standard Claude Code sessions (like the baseline test conversations) go directly to Anthropic's API and are NOT captured.

**To get baseline conversations into Phoenix, we must:**
1. Run Claude Code with `baseURL` pointed to the `claude-lens` proxy
2. Create new test conversations through the proxy
3. Sync the new Phoenix traces

### Implication for Testing

The acceptance criteria testing approach needs adjustment:
- **Cannot use existing `~/.claude` conversations** as test data
- **Must create NEW test conversations** via the claude-lens proxy
- **Use Haiku model** for cost efficiency during testing

---

## Next Steps

### Immediate (This Session)
1. [x] Review this status document
2. [x] Investigate session ID correlation
3. [x] Test markdown export with existing Phoenix data
4. [x] Analyze gaps against acceptance criteria

---

## Comprehensive Plan to Fix Markdown Export Pipeline

**Created**: 2026-01-15

### Summary of Investigation

We tested the markdown export pipeline against real Phoenix trace data and identified specific gaps against the acceptance criteria.

### Test Sessions Used

| Session ID | Spans | Type | Findings |
|------------|-------|------|----------|
| `3640c6d7...` | 7 | Simple (Task + response) | User message ✅, Tool call ✅, Missing: final assistant text (was empty in raw data) |
| `3200b4ff...` | 32 | Multi-turn continuation | Assistant messages ✅, Tool calls ✅, Compaction summary ✅, Missing: original user message (expected - continuation) |

### Gap Analysis vs Acceptance Criteria

#### AC1: All data must be present and accounted for

| Data Type | Status | Notes |
|-----------|--------|-------|
| User messages | ⚠️ PARTIAL | Shown when present, but warmup/system-reminders filtered |
| Assistant messages | ⚠️ PARTIAL | Text shown, but some empty in raw data (streaming issue?) |
| Tool calls | ✅ PASS | Tool_use blocks extracted and shown |
| Tool responses | ⚠️ PARTIAL | Results shown but may be truncated |
| Thinking spans | ❓ UNTESTED | Need test data with extended thinking |
| Subagent requests | ✅ PASS | Referenced with links |
| Subagent responses | ⚠️ PARTIAL | Response text shown but full trace not linked |
| Compactions | ✅ PASS | Markers shown correctly |
| Warmup messages | ❓ FILTERED | Currently filtered - need to decide if this is correct |

#### AC2: Full conversation in Markdown

| Requirement | Status |
|-------------|--------|
| Main thread visible | ✅ PASS |
| Ancillary sidelined | ✅ PASS |
| Long tool responses linked | ✅ PASS |
| Everything between compactions | ⚠️ NEEDS VERIFICATION |

#### AC3: First message preservation

| Requirement | Status |
|-------------|--------|
| First user message captured | ⚠️ DEPENDS | Only if present in trace (continuation sessions don't have it) |

#### AC4: Session IDs link across compactions

| Requirement | Status |
|-------------|--------|
| Claude session UUID extraction | ✅ PASS |
| Chain building | ✅ PASS |

#### AC5-6: End-to-end tests

| Requirement | Status |
|-------------|--------|
| claude-lens proxy test | ❌ NOT DONE | Need to create test conversations |
| Subagent e2e test | ❌ NOT DONE | Need test data |

### Identified Issues

1. **Main thread turn count is 0** - Thread classifier marks everything as "ancillary"
2. **Empty LLM output_messages** - Some sessions have empty content (streaming instrumentation issue?)
3. **No ground truth comparison** - Need automated validation against raw span data
4. **Warmup/system-reminder filtering** - Need to document what should be filtered vs shown

### Recommended Fix Plan

#### Phase 1: Fix Thread Classification (P1)
- Review `threads.py` classification logic
- Ensure `Claude_Code_Internal_Prompt_*` spans with user content are marked as main thread
- Add tests with known-good data

#### Phase 2: Add Ground Truth Validation (P1)
- Create script that compares:
  - Raw span INPUT/OUTPUT values
  - Exported markdown content
  - Flags missing data
- Use for regression testing

#### Phase 3: End-to-End Test Infrastructure (P2)
- Set up claude-lens proxy test
- Create simple test conversation through proxy
- Sync and verify data capture
- Automate as CI test

#### Phase 4: Documentation & Acceptance (P2)
- Document expected behavior for each data type
- Create visual examples of correct exports
- Define what should/shouldn't be filtered

---

## 4.1 Behavior Decisions (RESOLVED 2026-01-15)

| Item | Decision | Rationale |
|------|----------|-----------|
| **Warmup messages** | Show as `*[Session initialization]*` | Technical cache-priming message, separate from first user message |
| **System reminders** | Exclude from main thread | Future TODO for optional `--show-system-reminders` flag |
| **Tool call threshold** | 500 chars | Keep current - inline if ≤500, link if >500 |
| **Subagent trace threshold** | 1000 chars | Inline full trace if ≤1000, link if >1000 |
| **Subagent in main thread** | Always show kickoff + response | Even when linking to full trace file |
| **Empty assistant messages** | Data pipeline issue | Not a display decision - investigate upstream |

### Key Clarifications

**Warmup vs First User Message**: These are SEPARATE messages:
```
1. Warmup → "Warmup" (cache priming, show as [Session initialization])
2. First user message → actual user request (always show full content)
```

**System Reminders**: Injected by Claude Code infrastructure (e.g., `<system-reminder>Your todo list...</system-reminder>`). Excluded from main thread for now.

### Short-Term
1. [ ] Add filter statistics to export metadata
2. [ ] Add high-filter-rate warning
3. [ ] Review deduplication aggressiveness

### Medium-Term
1. [ ] Create "verbose" export mode for debugging
2. [ ] Consider including ancillary threads in separate section
3. [ ] Document analysis module API

---

## File Locations Reference

| Purpose | Path |
|---------|------|
| Issues tracker | `MARKDOWN_EXPORT_ISSUES.md` |
| Investigation report | `/private/tmp/markdown_export_investigation_report.md` |
| Pipeline documentation | `docs/markdown_export_pipeline_documentation.md` |
| Export code | `dev_agent_lens/analysis/chains.py` |
| Test exports | `/private/tmp/test_chain_export_v{8,9,10,11}/` |
| Parquet data | `~/.dal/data/parquet/phoenix-local-alex_*.parquet` |
| Raw data | `~/.dal/data/raw/phoenix-local-alex/*.jsonl` |
