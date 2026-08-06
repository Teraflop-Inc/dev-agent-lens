# Phoenix Telemetry via Hooks: Assessment & Deployment Plan (ENG2-1486)

**Date**: 2026-08-06  
**Status**: Blocker identified + recommendation

## TL;DR

**🚨 The sf-phoenix reachability blocker is NOT resolved.** sf-phoenix.fly.dev has no public IP, so it's unreachable from developer laptops and customer machines. The plugin marketplace does **not** fix this — the marketplace is an *install* mechanism (it registers hooks and drops the plugin code), not a *transport*. The plugin still POSTs spans to whatever backend endpoint you configure, which defaults to `http://localhost:6006` for Phoenix. There is no reachable self-hosted Phoenix today.

**➡️ The one public endpoint that works today is Arize AX (`otlp.arize.com:443`).** DAL's `.env` already carries `ARIZE_API_KEY` + `ARIZE_SPACE_KEY`. The tradeoff: spans land in Arize's SaaS, **not** our self-hosted Phoenix. See Part 2b.

**✅ Adopt the NEW actively-maintained repo** (`Arize-ai/coding-harness-tracing`). The older `Arize-ai/arize-claude-code-plugin` is **superseded in practice** — not archived, no deprecation notice on `main`, but its last push was 2026-03-31 and active development has clearly moved. The new repo has 16 hooks vs 9, better error handling, and — importantly — uses **Python hooks with native Windows `.exe` shims** instead of bash, which materially de-risks the Windows question the old bash plugin raised.

**⚠️ LOCAL TESTING BLOCKED** — Docker unavailable in this VM, so we cannot run a local Phoenix here to prove the mechanics end-to-end. We verified the plugin code and transport paths by reading the source.

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

**Note on the marketplace:** installing via `claude plugin marketplace add ...` does not change any of this. It registers the hook commands in `~/.claude/settings.json` and installs the plugin code. The span transport is entirely separate: the plugin reads `PHOENIX_ENDPOINT` (default `http://localhost:6006`) or `ARIZE_API_KEY`+`ARIZE_SPACE_ID` (default `otlp.arize.com:443`) and POSTs there itself. Marketplace ≠ a reachable backend.

### Options to Resolve (ranked by practicality)

| Option | Pros | Cons | Customer-Ready |
|--------|------|------|-----------------|
| **A: Arize AX (public OTLP)** | `otlp.arize.com:443` is public and reachable **today**; creds already in DAL `.env`; no infra to stand up | Spans land in Arize SaaS, not our Phoenix; DAL's Phoenix analysis tooling won't see them | ✅ Yes |
| **B: Customer-run Phoenix** | Customer controls data; keeps everything on Phoenix | Adds Phoenix infrastructure burden on the customer | ✅ Yes |
| **C: Allocate public IP on sf-phoenix + Auth** | Keeps data on our Phoenix | Exposes a currently-unauthenticated service; requires OAuth before exposure | ⚠️ Partially |
| **D: Tailnet routing** | Works for internal team | Cornerstone not on our tailnet; doesn't solve the customer case | ❌ No |
| **E: Local Phoenix in VM** | Would prove the mechanics | Docker not available in this VM; endpoint still unsolved | ❌ No |

### Recommended Path

**For a working pipeline TODAY: Option A (Arize AX).** Its endpoint is public, the credentials already exist in DAL's `.env`, and the new repo sends to it over plain HTTP/JSON (no gRPC, no extra Python deps — see Part 2b). The cost is data location: telemetry lands in Arize's SaaS rather than our self-hosted Phoenix, so it won't flow through DAL's existing Phoenix-based analysis.

**If keeping data on our Phoenix is a hard requirement:** Option B (customer-run Phoenix) or Option C (expose sf-phoenix with auth). Both require net-new infrastructure or a security review before they're usable — neither is available today.

---

## Part 2: Plugin Assessment

### Old Plugin: Arize-ai/arize-claude-code-plugin

**Status**: Superseded in practice (NOT formally deprecated or archived)  
**Last commit on `main`**: 2026-03-31 (4 months old at ticket creation)  
**Repo `pushedAt`**: 2026-07-22 — but that's an unmerged branch (`duncankmckinnon-patch-1`) push, not a release; `main`'s README carries no deprecation notice and `gh repo view` reports `isArchived: false`  
**Maintenance**: Active development has moved to `Arize-ai/coding-harness-tracing`; this repo is stale but still functional

> Verified 2026-08-06: `gh repo view Arize-ai/arize-claude-code-plugin --json isArchived,pushedAt` → `{"isArchived": false, "pushedAt": "2026-07-22T20:32:15Z"}`. The only "DEPRECATED" text lives on the unmerged `duncankmckinnon-patch-1` branch, not `main`.

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

### Verdict: ADOPT THE NEW REPO — DON'T DEPEND ON THE STALE ONE

**Recommendation**: Adopt `Arize-ai/coding-harness-tracing`.

- ✅ Actively maintained (pushed 2026-08-06)
- ✅ 77% more hook coverage (16 vs 9)
- ✅ Better error tracking (PostToolUseFailure, StopFailure, PermissionDenied)
- ✅ **Python hooks with native Windows `.exe` shims** (POSIX: `venv/bin/<hook>`, Windows: `venv/Scripts/<hook>.exe` — see `core/setup/__init__.py:venv_bin`). This is a better Windows story than the old repo's bash hooks.
- ✅ Multi-harness framework (extensible if we support other coding harnesses later)
- ✅ Clear migration path for existing users

**Risks**: The new repo requires a Python venv to be provisioned at install time (its hooks are console-script entry points, not standalone bash). That's a different install footprint than the old pure-bash plugin — confirm the installer provisions the venv correctly on the target OS. The Windows *bash-hook* risk from the old plugin does not apply here, but a Windows *Python/venv* smoke test is still warranted.

---

## Part 2b: Arize AX Backend — the endpoint that actually works today

The sf-phoenix blocker means there is no reachable **self-hosted Phoenix**. But the plugin's *other* backend, Arize AX, is a public hosted service — and it's the one route that requires no infrastructure work on our side.

### Why it's viable right now

- **Public endpoint**: `otlp.arize.com:443` is internet-reachable from any laptop, including Cornerstone's Windows machines. No IP allocation, no tailnet, no customer-hosted service.
- **Credentials already exist**: DAL's `.env` carries `ARIZE_API_KEY` and `ARIZE_SPACE_KEY` (verified present 2026-08-06). These are the same credentials the LiteLLM proxy already uses for the Arize path (`docker-compose.yml`, `litellm_config_arize.yaml`).
- **No gRPC / no extra Python deps**: The new repo sends Arize spans over plain **HTTP/JSON via `urllib.request`** (stdlib) — it POSTs to `https://otlp.arize.com/v1/traces` with an `authorization: Bearer <api_key>` header and a `space_id` header (`core/common.py:751-785`). The old README's `pip install opentelemetry-proto grpcio` requirement is **gone** in this repo.

### The credential-name gotcha (must verify before relying on it)

The plugin reads **`ARIZE_SPACE_ID`** (`tracing/claude_code/constants.py:39`, `core/common.py:146`). DAL's `.env` provides **`ARIZE_SPACE_KEY`**. Arize renamed "space key" → "space id" historically, so the *value* is very likely the same credential under a new name — but this is **unverified**. Before depending on it, do one test send with `ARIZE_SPACE_ID` set to the current `ARIZE_SPACE_KEY` value and confirm a span lands in Arize. Do **not** assume they're interchangeable without that check.

### The tradeoff — read before choosing this

Spans land in **Arize's SaaS, not our self-hosted Phoenix.** Concretely:

- ❌ **DAL's Phoenix-based analysis tooling won't see these spans.** Everything DAL does downstream (`clients/`, `analysis/`, `export/`) reads from Phoenix. Arize-hosted Claude Code telemetry is a separate silo unless/until we build an Arize reader.
- ⚠️ **Data governance**: Cornerstone's Claude Code activity (prompts, tool commands, file paths if content logging is on) would flow to Arize's hosted platform. That's a customer-data-location decision, not just a technical one — confirm it's acceptable to Cornerstone before enabling.
- ✅ **But it works today**, end to end, with zero infrastructure. For a Phase-0 "prove the pipeline captures Claude Code sessions" milestone, this is the lowest-friction path.

### Recommendation

If the goal is **a working capture pipeline this week**, Arize AX is the answer — with the explicit caveat that the data lands in Arize, not Phoenix, and DAL's analysis stack won't consume it yet. If the goal is **keeping everything on our Phoenix**, this path doesn't serve it, and we're back to Options B/C in Part 1 (customer-run Phoenix, or expose sf-phoenix with auth), neither of which is available today.

---

## Part 3: Hook Coverage & Claude Code Compatibility

### Hooks fire on all Claude Code surfaces

Per docs: *"Hooks run wherever Claude Code runs: sessions in the terminal, IDE extensions, the Desktop app, and Claude Code on the web all fire the same hook events."*

| Surface | Hooks | Verified |
|---------|-------|----------|
| Terminal CLI | ✅ | Yes (plugin registers hooks in ~/.claude/settings.json) |
| Desktop | ✅ | Yes (shares ~/.claude/settings.json with CLI) |
| Web / Cloud | ✅ | Hooks fire per Arize docs, but the span still needs a *reachable* endpoint from wherever the hook runs — a cloud session cannot POST to `localhost:6006`. Only a public backend (e.g. Arize AX) works here. |

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

### Pattern 3: Plugin Marketplace + `enabledPlugins` (for cloud/web reach)

**For**: Reaching Claude Code Cloud/web sessions (the only install path that does)  
**How**: Marketplace metadata declares plugin + env vars in `~/.claude/settings.json`  
**Limitations**: Credentials must be in `~/.claude/settings.json` or managed settings (not GitOps-friendly).  
**Important**: This is an *install* path, not a transport. A cloud session's hook still has to POST to a **publicly reachable** backend — it cannot reach `localhost:6006` or `sf-phoenix.internal`. Pair this pattern with Arize AX (or another public endpoint), never with an unreachable Phoenix.

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

- **Windows Python/venv hooks** — New repo uses Python console-script hooks with Windows `.exe` shims, not bash. No Windows CI; Cornerstone to smoke-test that the venv provisions and hooks fire. (The old bash-hook risk is moot on this repo.)
- **Self-hosted Phoenix reachability** — sf-phoenix has no public IP; NOT resolved. Customer-run Phoenix or exposing sf-phoenix with auth are the only Phoenix-preserving options, neither available today.
- **`ARIZE_SPACE_KEY` vs `ARIZE_SPACE_ID`** — likely the same credential renamed, but unverified. One test send required before relying on it.
- **Managed settings at org level** — Requires Cornerstone's infrastructure decisions (GPO, Intune, etc.)

---

## Recommendation Summary

**Use `Arize-ai/coding-harness-tracing`** (actively maintained; the older `arize-claude-code-plugin` is superseded in practice, not deprecated/archived).

**For endpoint — the sf-phoenix blocker is NOT resolved, so pick by goal:**
- **Working pipeline this week** → **Arize AX** (`otlp.arize.com:443`, public, creds already in `.env`). Tradeoff: spans land in Arize's SaaS, not our Phoenix; DAL's analysis stack won't consume them yet.
- **Keep data on our Phoenix** → customer-run Phoenix, or allocate a public IP on sf-phoenix **with auth first**. Neither is available today.
- The plugin **marketplace does not provide an endpoint** — it's install-only. Don't rely on it to reach an unreachable Phoenix.

**For Cornerstone Windows deployment:**
- Recommend GPO-rendered managed settings with a secrets store for API keys
- Provide bootstrap script templates (Arize AX config, since that's the reachable backend)

**Next step**: Cornerstone runs the Windows venv/hook smoke test and confirms whether Arize-hosted data location is acceptable; if not, scope the Phoenix-reachability work as a separate deliverable.

