# Capture by surface

Claude Code and Codex run in many places: a terminal, a headless script, a desktop app,
an editor panel, a phone driving a laptop, a cloud sandbox. Each one reaches DAL by a
different path, and some paths don't reach some surfaces at all. This page says, per
surface, which path works, how you know, and which one to count.

The tables come from a daily canary: one prompt per surface, then a query for that prompt
in each path. Every cell names the tool version and the date it was last proven.
**Last regenerated: 2026-10-27**, from 28 daily runs (2026-09-30 to 2026-10-27).

## The three paths

| Path | What it records | Needs from the surface | Set up with |
|---|---|---|---|
| **Proxy** | every model call, live: model, tokens, account id, request and response | the surface sends its API traffic to a base URL you choose | [Proxy setup](proxy-setup.md), [Reaching the proxy](reaching-the-proxy.md) |
| **Hooks** | one span per lifecycle event, tagged with the surface (`claude.surface`) | the surface runs lifecycle hooks from a settings file | a hook script you register (see [Hooks](#hooks)) |
| **Session files** | the whole session after the fact: prompts, turns, tool calls, model, tokens | the surface writes a transcript to the local disk | `dal ingest-sessions` ([Capture setup](codex-capture.md)) |

The proxy and session files both carry tokens for the same calls. **Count tokens from one
of them per surface, never both** (see [What to count](#what-to-count)). Hooks carry no
tokens; they are where the surface name comes from.

## Claude Code

### Matrix

Cells: **working** = the canary's prompt landed in that path; **impossible** = it can't,
and the reason is in [Why a cell is impossible](#why-a-cell-is-impossible); **unverified** =
no proof either way.

| Surface | How it was routed | Proxy | Hooks | Session files |
|---|---|---|---|---|
| Terminal `claude` | shell env | working · 2.1.311 · 2026-10-27 | working · 2.1.311 · 2026-10-27 | working · 2.1.311 · 2026-10-27 |
| Headless `claude -p` | shell env | working · 2.1.311 · 2026-10-27 | working · 2.1.311 · 2026-10-27 | working · 2.1.311 · 2026-10-27 |
| Subagent (`-p` spawning a Task) | shell env | working · 2.1.311 · 2026-10-27 | working · 2.1.311 · 2026-10-27 | working · 2.1.311 · 2026-10-27 |
| VS Code extension | global `~/.claude/settings.json` | working · 2.1.287 · 2026-10-27 | working · 2.1.287 · 2026-10-27 | working · 2.1.287 · 2026-10-27 |
| VS Code extension | project `<folder>/.claude/settings.json` | working · 2.1.287 · 2026-10-27 | working · 2.1.287 · 2026-10-27 | working · 2.1.287 · 2026-10-27 |
| Desktop app, local session | global `~/.claude/settings.json` | **impossible** · 2.19675.0 · 2026-10-26 | working · 2.19675.0 · 2026-10-27 | working · 2.19675.0 · 2026-10-27 |
| Desktop app, local session | project `<folder>/.claude/settings.json` | **impossible** · 2.19675.0 · 2026-10-26 | working · 2.19675.0 · 2026-10-27 | working · 2.19675.0 · 2026-10-27 |
| Desktop app, local session | Desktop's third-party inference configuration | unverified | unverified | unverified |
| Remote Control | any | **impossible** · 2.1.285 · 2026-10-26 | working · 2.1.285 · 2026-10-27 | working · 2.1.285 · 2026-10-27 |
| Cloud session (claude.ai/code) | n/a | **impossible** · web · 2026-10-26 | **impossible** · web · 2026-10-26 | **impossible** · web · 2026-10-26 |
| Containers and sandboxes | `ANTHROPIC_BASE_URL` resolved at start | unverified | unverified | unverified |

The terminal, headless and subagent rows also pass on the older pinned version, 2.1.285, on
the same date. Remote Control runs the local CLI, so its version is the CLI's. The
desktop's version is the app's.

Containers and sandboxes run the CLI, so they should behave like the terminal row, but the
canary doesn't drive one. Getting the proxy address into one is covered in
[Reaching the proxy](reaching-the-proxy.md#containers-and-sandboxes-that-cannot-resolve-the-name);
session files need the container's `~/.claude/projects` copied out or mounted before it is
thrown away.

### Recommended recording method

| Surface | Tokens and model from | Surface name from | Person from |
|---|---|---|---|
| Terminal, headless, subagents | proxy | hooks | account id on the proxy span |
| VS Code extension | proxy | hooks | account id on the proxy span |
| Desktop app | session files | hooks | `--user` on ingest, or git `user.email` |
| Remote Control | session files | hooks (`claude.surface` = `cli`, `claude.bridged` = true) | `--user` on ingest, or git `user.email` |
| Cloud session | nothing reaches DAL | | |

For the surfaces the proxy covers, session files are still worth ingesting as a backfill for
runs that missed the proxy (a forgotten wrapper, the proxy down). Keep them under their own
source and don't add their tokens to the proxy's.

### How to route each surface through the proxy

Four options, each with its own way to fail:

| Option | Reaches | How it fails |
|---|---|---|
| `claude-lens` wrapper | terminal, headless, subagents of a wrapped run | A run started as plain `claude` goes untraced. Nothing errors; the spans are just missing. |
| `ANTHROPIC_BASE_URL` in the shell profile | terminal, headless, anything started from that shell | GUI apps started from the Dock or Start menu never see it. |
| `env` in global `~/.claude/settings.json` | terminal, headless, VS Code | Every Claude Code session on the machine depends on the proxy. With the proxy down or unreachable, Claude Code stops working instead of going untraced. Also switches Remote Control off. The desktop app ignores it. |
| `env` in a project `<folder>/.claude/settings.json` | sessions opened in that folder, in the terminal and VS Code | Same dependency, scoped to the folder. In an interactive session it applies only after the folder is trusted. The desktop app ignores it. |

Which tradeoff is right depends on the deployment. A global setting catches everything,
including headless jobs and editor panels, at the cost of Claude Code going down with the
proxy. The wrapper fails safe and scoped, at the cost of silent gaps. Some teams use the
project file for work folders and the wrapper everywhere else.

VS Code also has its own setting, `claudeCode.environmentVariables` in VS Code's user
settings, which Anthropic's gateway guide recommends for the extension. The canary routes
VS Code through `settings.json`, so that route is the proven one.

**The desktop app.** It sets `ANTHROPIC_BASE_URL` itself for a local session, so a value
from the shell or any settings file never reaches the session
([Claude Code desktop docs](https://code.claude.com/docs/en/desktop)). Its only gateway
route is the third-party inference configuration (Help → Troubleshooting → Enable
Developer Mode, then Developer → Configure Third-Party Inference, or deployed by an
administrator; see
[Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect#desktop-app)).
That mode authenticates to the gateway with a credential the organization issues instead of
the person's Claude login, and while it is on, Remote Control and Anthropic-hosted cloud
environments are unavailable. DAL's proxy forwards the person's own Claude login, so the
third-party mode is a different billing and auth setup, not a capture switch. It is
unverified with DAL; use session files and hooks for desktop.

**Remote Control.** Claude Code turns Remote Control off whenever `ANTHROPIC_BASE_URL`
points at a host other than api.anthropic.com, or a gateway credential is set
([gateway guide](https://code.claude.com/docs/en/llm-gateway-connect#slack-cloud-sessions-and-remote-control)).
A global proxy setting therefore removes `/remote-control` from every session on the
machine, and `claude-lens` removes it from the sessions it starts. Start plain `claude`
for a session you will drive remotely; hooks and session files still capture it.

### Hooks

Hooks are the only path that records which surface a session ran on. Claude Code runs
every hook registered in a settings file, including from the desktop app and VS Code, and
sets `CLAUDE_CODE_ENTRYPOINT` in the hook's environment: `cli`, `claude-desktop`,
`claude-vscode`, `sdk-cli`. Remote Control sessions report `cli` and also set
`CLAUDE_CODE_BRIDGE_SESSION_ID`.

DAL does not ship a hook script yet. A hook that DAL can read posts one OTLP trace span per
event to the receiver's `/v1/traces`, with:

| Attribute | Value | Where it lands in `spans` |
|---|---|---|
| resource `openinference.project.name` | the source name to file it under, such as `claude-code-surfaces` | `source` |
| `claude.surface` | `$CLAUDE_CODE_ENTRYPOINT` | `attributes_rest->'claude'->>'surface'` |
| `claude.bridged` | true when `$CLAUDE_CODE_BRIDGE_SESSION_ID` is set | `attributes_rest->'claude'->>'bridged'` |
| `claude.session_id` | `session_id` from the hook's stdin JSON | `session_id` |
| `claude.cwd` | `cwd` from the hook's stdin JSON | `attributes_rest->'claude'->>'cwd'` |
| `user.id` | `oauthAccount.accountUuid` from `~/.claude.json` | `person`, through `identity.yaml` |

Register it in `~/.claude/settings.json` for `SessionStart`, `UserPromptSubmit` and `Stop`,
and make it exit 0 whatever happens, so a down receiver never blocks a session:

```json
{
  "hooks": {
    "SessionStart":     [{"hooks": [{"type": "command", "command": "/path/to/hook"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/path/to/hook"}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "/path/to/hook"}]}]
  }
}
```

The hook stdin carries the prompt (`UserPromptSubmit`) and the last reply (`Stop`). Leave
them out of the span unless the people being recorded have agreed to it.

### Every capture mechanism Claude Code documents

| Mechanism | Used | Why |
|---|---|---|
| `ANTHROPIC_BASE_URL` in the environment | yes, proxy | The documented gateway switch for the CLI. |
| `env` block in user, project or local `settings.json` | yes, proxy | Reaches the CLI and VS Code. The desktop app ignores it. |
| Managed settings `env` | not measured | Same key, deployed by an organization. Expected to behave like the user file. |
| `claudeCode.environmentVariables` (VS Code setting) | not measured | The gateway guide's recommended VS Code route; `settings.json` is the proven one. |
| Desktop third-party inference configuration | no | The only desktop gateway route, but it replaces the person's login with a gateway credential and disables Remote Control. |
| `--settings <file-or-json>` | yes, hooks | Per-session settings; the canary registers its hooks this way. |
| Hooks | yes | The only source of the surface name. |
| Session transcripts in `~/.claude/projects` | yes | The widest path: needs only a transcript on disk. |
| Native OpenTelemetry (`CLAUDE_CODE_ENABLE_TELEMETRY`) | no | Exports metrics and log events, with model, tokens and account id per request ([monitoring docs](https://code.claude.com/docs/en/monitoring-usage)). DAL's receiver accepts OTLP traces only; Claude Code's traces need `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA` and are beta. Telemetry export variables are ignored in project and local settings files. A candidate for desktop that nobody has measured. |
| `--output-format stream-json` | no | Per-run stdout; fine for one script, not a capture path. |
| `ANTHROPIC_CUSTOM_HEADERS` | no | Could tag proxy calls (for example with a person for headless runs on a shared token). Not needed while the account id is on every call. |
| Agent SDK `env` option | inherits | The SDK spawns the CLI, so it is the headless row. |

### Why a cell is impossible

| Cell | Reason |
|---|---|
| Desktop app, proxy, via `settings.json` | Desktop sets `ANTHROPIC_BASE_URL` itself and ignores the value from a settings file in a local session. |
| Remote Control, proxy | Claude Code disables Remote Control when `ANTHROPIC_BASE_URL` is not api.anthropic.com. |
| Cloud session, all three | It runs on Anthropic's machine: no access to your proxy, your hooks endpoint or your disk. Gateway variables set on a cloud environment are not applied. |

The canary re-checks impossible cells weekly, so a change in a new version shows up as a
cell that starts working.

## Codex

### Matrix

| Surface | How it was routed | Proxy | Hooks | Session files |
|---|---|---|---|---|
| Codex CLI (`codex`, interactive) | user-level provider in `~/.codex/config.toml` | working · 0.160.0 · 2026-10-05, unverified since | working · 0.160.0 · 2026-10-05, unverified since | working · 0.160.0 · 2026-10-05, unverified since |
| `codex exec` | same | working · 0.160.0 · 2026-10-05, unverified since | working · 0.164.0 · 2026-10-27 | working · 0.160.0 · 2026-10-05, unverified since |
| Codex desktop app | user-level provider in `~/.codex/config.toml` | working · 26.928.40906 · 2026-10-02, unverified since | working · 26.928.40906 · 2026-10-02, unverified since | working · 26.928.40906 · 2026-10-02, unverified since |
| Codex cloud | n/a | unverified | unverified | unverified |

The canary's Codex login was signed out on 2026-10-05, so every Codex cell that needs a model
call has had no fresh proof since. The `exec` hooks cell still passes because the hook runs
before the failed call. The pinned lane, 0.159.0, has the same results. The desktop row comes
from a one-off canary run on 2026-10-02, before the daily run covered the desktop app.

### Recommended recording method

| Surface | Tokens and model from | Surface name from | Person from |
|---|---|---|---|
| Codex CLI and `exec` | proxy | hooks | `x-dal-user` header on the proxy call (an email in `identity.yaml`) |
| Codex desktop app | proxy | hooks | `x-dal-user` header in the provider block |

### Routing Codex through the proxy

Codex's provider is set in the user-level `~/.codex/config.toml`, or on the command line
with `-c`. A project `.codex/config.toml`
cannot set it: Codex ignores `model_provider`, `model_providers`, `openai_base_url`, `otel`
and `notify` there
([Codex advanced config](https://learn.chatgpt.com/docs/config-file/config-advanced.md)).

With a ChatGPT login, the proxy needs a pass-through route to the ChatGPT Codex backend;
`fly/litellm_config_fly.yaml` has one. The provider block, as the canary sets it:

```sh
codex -c 'model_provider="dal"' \
  -c 'model_providers.dal={name="DAL",base_url="http://<proxy>:4000/<codex-route>",wire_api="responses",requires_openai_auth=true,http_headers={"x-dal-user"="you@example.com"}}'
```

`requires_openai_auth = true` keeps the ChatGPT login. The `x-dal-user` header is how the
proxy names the person, and it must be an email `identity.yaml` lists: a personal git email
leaves every span with `person` empty, without an error. Proxy spans for Codex also carry no
`agent` value yet.

Written as `model_provider` and `[model_providers.dal]` in the user-level
`~/.codex/config.toml` instead of passed with `-c`, the same block also routes the desktop
app (ChatGPT.app 26.928.40906, 2026-10-02).

### Every capture mechanism Codex documents

| Mechanism | Used | Why |
|---|---|---|
| `model_provider` / `model_providers` (user-level, or `-c`) | yes, proxy | The only way to point Codex at a proxy while keeping a ChatGPT login. |
| `openai_base_url` | no | Redirects the built-in OpenAI provider; the route above uses a custom provider instead. |
| Hooks (`~/.codex/hooks.json` or `[hooks]` in `config.toml`) | yes | On by default. A hook runs only after a person trusts it with `/hooks`, and trust is tied to the hook's hash, so keep the registration stable and change the script it calls. Stdin carries `session_id`, `cwd`, `hook_event_name` and `model` ([Codex hooks](https://learn.chatgpt.com/docs/hooks.md)). The Claude Code hook contract above works with `claude.surface` set to `codex`. |
| Session rollout files in `~/.codex/sessions` | yes | CLI, `exec`, IDE and desktop share the directory. |
| `[otel]` (user-level) | no | Exports log events and metrics; DAL's receiver accepts traces only. Prompts are redacted unless `log_user_prompt` is set. |
| `notify` | no | Fires only on `agent-turn-complete`; hooks cover more. |
| `codex exec --json` | no | Per-run stdout events; fine for one script, not a capture path. |

## What to count

| Question | Sum this | Not this |
|---|---|---|
| Tokens and cost on the CLI, headless, subagents, VS Code | proxy source | session files for the same sessions |
| Tokens on the desktop app and Remote Control | session-file source | |
| Sessions per surface | hook spans, distinct `session_id` per surface | proxy spans (no surface) |
| Per person | either token source, with `person` from `identity.yaml` | both added together |

## Check a surface yourself

Send one prompt from the surface with a nonce in it, wait 30 seconds for the receiver to
flush, rebuild the typed layout, then:

```sql
-- proxy: rows with a model and tokens
SELECT source, model_name, person, count(*) AS calls, sum(tokens_prompt) AS prompt_tokens
FROM spans WHERE source = 'claude-code' AND start_time > now() - INTERVAL 1 HOUR
GROUP BY ALL;

-- hooks: one row per surface
SELECT attributes_rest->'claude'->>'surface' AS surface,
       attributes_rest->'claude'->>'bridged' AS bridged,
       person, count(DISTINCT session_id) AS sessions
FROM spans WHERE source = 'claude-code-surfaces' AND start_time > now() - INTERVAL 1 HOUR
GROUP BY ALL;

-- session files: after `dal ingest-sessions --project claude-sessions --to-store`
SELECT source, model_name, person, count(*) AS spans
FROM spans WHERE source IN ('claude-sessions', 'codex-sessions')
  AND start_time > now() - INTERVAL 1 HOUR
GROUP BY ALL;
```

Expected: the proxy query returns a row with the model the surface used and nonzero
tokens, plus a row with an empty `model_name` for the proxy's own spans; the hooks query
returns the surface name (`cli`, `claude-desktop`, `claude-vscode`, `sdk-cli` for headless,
and `cli` with `bridged` = true for Remote Control); the session-file query returns the
session. On a store that holds a lot of history, add a filter on the `day` column so the
query reads only recent files. `person` empty means the
account id or email is missing from `identity.yaml`. Use your own source names if you
changed `CAPTURE_SOURCE` or the hook's project.

## Identity

| Path | Identifies the person by | Put in `identity.yaml` |
|---|---|---|
| Claude Code proxy and hooks | account id, from `~/.claude.json` `oauthAccount.accountUuid` | `accounts: [{uuid: ...}]` |
| Codex proxy | the `x-dal-user` email | `email:` |
| Session files | `dal ingest-sessions --user`, else git `user.email` | `users:` or `email:` |

An identifier `identity.yaml` doesn't list leaves `person` empty; the sync log names the ids
it saw that have no entry.

## Known gaps

- **Cloud sessions** (claude.ai/code, and Codex cloud) leave nothing DAL can reach.
- **Claude session files are ingested by hand.** `scripts/install_codex_capture.sh`
  schedules Codex session files every minute on macOS; Claude's have no scheduled job yet.
- **Claude desktop's proxy route** (third-party inference mode) is unverified with DAL.
- **DAL ships no hook script.** The contract above is what one must send.
