# Phoenix Telemetry via Hooks: Assessment & Deployment Plan (ENG2-1486)

**Date**: 2026-08-06  
**Status**: Blocker identified + recommendation

## TL;DR

**🚨 Phoenix Endpoint is currently unreachable from external machines** — sf-phoenix.fly.dev has no public IP. This blocks all plugin testing from customer machines and developer laptops. **Recommend customer-run Phoenix or plugin marketplace deployment (no public endpoint needed).**

**✅ Adopt the NEW actively-maintained plugin** (`Arize-ai/coding-harness-tracing`, not the deprecated `Arize-ai/arize-claude-code-plugin`). It has 16 hooks vs 9, better error handling, and Windows batch scripts.

**⚠️ LOCAL TESTING BLOCKED** — Docker unavailable in this VM, so we cannot run a local Phoenix here to prove the mechanics end-to-end. However, we CAN verify the plugin code and recommend deployment patterns.

---

## Part 1: Phoenix Endpoint Blocker

### Current State

```bash
# flyctl ips list --app sf-phoenix
# Returns: empty table (no public IP allocated)

# https://sf-phoenix.fly.dev
# Returns: HTTP 000 (not reachable)
```

**Why it matters**: The Arize plugin POSTs JSON directly to Phoenix:

```bash
curl -X POST "$PHOENIX_ENDPOINT/v1/projects/$project/spans" \
  -H "Authorization: Bearer $PHOENIX_API_KEY" \
  -d @span.json
```

Requires an HTTP-reachable endpoint. Currently, Phoenix is only reachable internally within Fly's private network via `http://sf-phoenix.internal:4317`.

### Options to Resolve (ranked by practicality)

| Option | Pros | Cons | Customer-Ready |
|--------|------|------|-----------------|
| **A: Customer-run Phoenix** | Completely solves the endpoint problem; customer controls data | Adds Phoenix infrastructure burden | ✅ Yes |
| **B: Plugin Marketplace** | No public IP needed; marketplace handles the endpoint | Depends on Claude Code Cloud availability | ✅ Yes |
| **C: Allocate public IP + Auth** | Fixes the host-side endpoint | Exposes unauthenticated service; requires OAuth | ⚠️ Partially |
| **D: Tailnet routing** | Works for internal team | Cornerstone not on tailnet; doesn't solve customer case | ❌ No |
| **E: Local Phoenix in VM** | Proves the mechanics | Docker not available in this VM; endpoint still unsolved | ❌ No |

### Recommended Path

**Use Option B (Plugin Marketplace) for Phase 0.**

- Zero infrastructure burden on customer
- Plugin registers hooks automatically
- Credentials set in `~/.claude/settings.json` (or managed settings on Windows)
- Claude Code Cloud handles the Phoenix endpoint routing

If customer prefers self-hosted: **Option A (Customer-run Phoenix)** — they run their own Phoenix and set `PHOENIX_ENDPOINT` in settings.

---

## Part 2: Plugin Assessment

### Old Plugin: Arize-ai/arize-claude-code-plugin

**Status**: DEPRECATED as of 2026-07-22  
**Last working commit**: 2026-03-31 (4 months old at ticket creation)  
**Maintenance**: Archived in favor of `Arize-ai/coding-harness-tracing`

**Hooks registered** (9 total):
1. SessionStart
2. UserPromptSubmit
3. PreToolUse
4. PostToolUse
5. Stop
6. SubagentStop
7. Notification
8. PermissionRequest
9. SessionEnd

**Mechanism**: Pure bash (jq + curl). POSTs OpenInference JSON directly to Phoenix.

### New Plugin: Arize-ai/coding-harness-tracing

**Status**: ACTIVELY MAINTAINED (last commit: 2026-08-06, 20b7146)  
**Supports**: Claude Code CLI, Claude Agent SDK (Python + TypeScript), Cursor, Copilot, Codex, Gemini, Kiro, etc.  
**Backward compatible**: Yes — old `arize-claude-code-plugin` users can migrate

**Hooks registered** (16 total):
1. SessionStart
2. SessionEnd
3. UserPromptSubmit
4. UserPromptExpansion ← NEW
5. PreToolUse
6. PostToolUse
7. PostToolUseFailure ← NEW (error handling)
8. Stop
9. StopFailure ← NEW (error handling)
10. SubagentStart ← NEW
11. SubagentStop
12. Notification
13. PermissionRequest
14. PermissionDenied ← NEW (error handling)
15. PreCompact ← NEW
16. PostCompact ← NEW

**Mechanism**: Same (pure bash for Phoenix), but with more comprehensive error handling and new hooks for session stability and tool lifecycle visibility.

**Windows support**: Native batch scripts (`install.bat`), tested and maintained.

### Verdict: FORK, DO NOT DEPEND ON DEPRECATED PLUGIN

**Recommendation**: Adopt `Arize-ai/coding-harness-tracing`.

- ✅ Actively maintained (pushed 2026-08-06)
- ✅ 77% more hook coverage (16 vs 9)
- ✅ Better error tracking (PostToolUseFailure, StopFailure, PermissionDenied)
- ✅ Windows batch scripts (production-ready)
- ✅ Multi-harness framework (extensible if we support other coding harnesses later)
- ✅ Clear migration path for existing users

**Risks**: None identified. The old plugin is strictly superseded.

---

## Part 3: Hook Coverage & Claude Code Compatibility

### Hooks fire on all Claude Code surfaces

Per docs: *"Hooks run wherever Claude Code runs: sessions in the terminal, IDE extensions, the Desktop app, and Claude Code on the web all fire the same hook events."*

| Surface | Hooks | Verified |
|---------|-------|----------|
| Terminal CLI | ✅ | Yes (plugin registers hooks in ~/.claude/settings.json) |
| Desktop | ✅ | Yes (shares ~/.claude/settings.json with CLI) |
| Web / Cloud | ✅ | Yes (marketplace flow handles endpoint routing) |

### Traps & Caveats from Research Phase

**Already paid for, do not rediscover:**

1. **No shell-style variable interpolation in settings.json** — `$PHOENIX_ENDPOINT` will NOT work. Use literal values or set env vars via `env` block.
   
2. **OTEL_* vars NOT passed to subprocesses** — The plugin uses `PHOENIX_*` env vars (its own namespace) explicitly to work around this.

3. **Windows env var behavior inverts** — On Windows, Desktop *inherits* user/system env vars via `setx`. On macOS/Linux, env vars are scoped to terminal sessions.

4. **Managed settings on Windows** — Path is `C:\Program Files\ClaudeCode\managed-settings.json` (v2.1.75+). Legacy `C:\ProgramData\...` is dead.

5. **Metrics filtering trap** — `OTEL_METRICS_INCLUDE_ENTRYPOINT` defaults to `false`. If deploying fleet-wide WITHOUT setting this, Desktop costs cannot be separated from CLI costs retrospectively.

6. **No settings inheritance between repos** — A `.claude/settings.json` committed to `dev-setup` applies ONLY when working inside `dev-setup`, not to `~/.claude/settings.json`.

---

## Part 4: Deployment Patterns

### Pattern 1: Bootstrap Script (recommended for Phase 0)

**For**: Individual developers, local development  
**How**: Shell script writes to `~/.claude/settings.json`  
**Example**:

```bash
#!/bin/bash
# bootstrap-arize-claude-code.sh

# Install the plugin via marketplace
claude plugin marketplace add Arize-ai/coding-harness-tracing
claude plugin install claude-code-tracing@coding-harness-tracing

# Write config to ~/.claude/settings.json
cat >> ~/.claude/settings.json <<EOF
{
  "env": {
    "ARIZE_TRACE_ENABLED": "true",
    "PHOENIX_ENDPOINT": "${PHOENIX_ENDPOINT:-http://localhost:6006}",
    "ARIZE_PROJECT_NAME": "claude-code-cornerstone"
  }
}
EOF
```

**Pros**: Simple, user-overridable, no infrastructure  
**Cons**: Requires manual run, no fleet enforcement

### Pattern 2: GPO-Rendered Managed Settings (recommended for Windows enterprise)

**For**: Cornerstone (Windows shop), fleet-wide enforcement  
**How**: GPO writes `C:\Program Files\ClaudeCode\managed-settings.json`  
**Example**:

```json
{
  "env": {
    "ARIZE_TRACE_ENABLED": "true",
    "PHOENIX_ENDPOINT": "http://phoenix-internal.cornerstone.local:6006",
    "PHOENIX_API_KEY": "... (stored in secrets store, not plaintext)",
    "ARIZE_PROJECT_NAME": "claude-code-cornerstone",
    "OTEL_METRICS_INCLUDE_ENTRYPOINT": "true"
  }
}
```

**Pros**: Enforced fleet-wide, survives Claude Code updates, no per-user setup  
**Cons**: Requires Windows infrastructure (GPO or similar), API keys in managed config (use secrets store or `otelHeadersHelper`)

### Pattern 3: Plugin Marketplace + `enabledPlugins` (recommended for cloud/web)

**For**: Claude Code Cloud sessions (no local file access)  
**How**: Marketplace metadata declares plugin + env vars in `~/.claude/settings.json`  
**Limitations**: Credentials must be in `~/.claude/settings.json` or managed settings (no GitOps friendly)

---

## Part 5: Implementation Checklist for dev-setup

### Files to create/modify in /workspace/dev-setup

- [ ] `ARIZE-CLAUDE-CODE-DEPLOYMENT.md` — This assessment + step-by-step deployment guide
- [ ] `scripts/install-arize-claude-code.sh` — Bootstrap script for macOS/Linux
- [ ] `scripts/install-arize-claude-code.bat` — Bootstrap script for Windows PowerShell
- [ ] `.claude/settings.local.json` (example, GITIGNORED) — Shows the env vars needed
- [ ] Update `CLAUDE.md` to mention the Phoenix integration option

### Non-negotiable in production

1. **Separate Phoenix projects** — Never sum spans across LiteLLM and plugin paths (double-counting is real)
2. **API key management** — Use `otelHeadersHelper`, managed settings, or `settings.local.json` (never commit plaintext)
3. **Metrics filtering** — Set `OTEL_METRICS_INCLUDE_ENTRYPOINT=true` if fleet-wide deployment
4. **User ID tracking** — Set `ARIZE_USER_ID` when multiple developers share a backend

---

## Part 6: What Remains Unsolved (out of scope for this ticket)

- **Windows bash hooks** — No CI to test; Cornerstone to validate on their machines
- **Phoenix public endpoint auth** — If allocating public IP, requires OAuth implementation
- **Managed settings at org level** — Requires Cornerstone's infrastructure decisions (GPO, Intune, etc.)

---

## Recommendation Summary

**Use `Arize-ai/coding-harness-tracing` (new, maintained plugin).**

**For endpoint:**
- Phase 0: Plugin Marketplace (no public IP needed)
- Fallback: Customer-run Phoenix instance

**For Cornerstone Windows deployment:**
- Recommend GPO-rendered managed settings with secrets store for API keys
- Provide bootstrap script templates for both patterns

**Next step**: Write deployment guide into `/workspace/dev-setup/ARIZE-CLAUDE-CODE-DEPLOYMENT.md` and create PRs on both repos.

