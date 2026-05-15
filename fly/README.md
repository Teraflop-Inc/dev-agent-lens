# Fly deploy: dev-agent-lens

Two Fly apps backing the Solutions-Fabric workspace-runner observability stack
(ENG2-1194). Both run in the `teraflop` org, primary region `iad`.

| App | What | Public URL | Internal URL |
|---|---|---|---|
| `sf-phoenix` | Arize Phoenix UI + OTLP collector | `https://sf-phoenix.fly.dev` (CF Access) | `sf-phoenix.internal:4317` (OTLP gRPC) |
| `sf-litellm` | Patched LiteLLM proxy (`aowen14/litellm-oauth-fix`) | `https://sf-litellm.fly.dev` (Bearer-auth) | — |

Both apps write to the **same Supabase Postgres** that already backs
`SandboxAgentCapture` (ENG2-1194 M1) — Phoenix uses schema `phoenix`, the
workspace runner uses `workspace_<project>`. Join across them via `session_id`.

## Prereqs

1. `flyctl auth login` (Teraflop org access)
2. The Supabase pooler DSN from the existing `dev-agent-lens/.env`
   (`PHOENIX_SQL_DATABASE_URL`)
3. An Anthropic API key

## Deploy order (Phoenix first, LiteLLM depends on it)

```bash
# 1. sf-phoenix
flyctl apps create sf-phoenix --org teraflop
flyctl volumes create phoenix_data --app sf-phoenix --region iad --size 1
flyctl secrets set --app sf-phoenix \
  PHOENIX_SQL_DATABASE_URL="postgresql://...@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
flyctl deploy --app sf-phoenix --config fly/phoenix.fly.toml

# 2. sf-litellm
flyctl apps create sf-litellm --org teraflop
flyctl secrets set --app sf-litellm \
  ANTHROPIC_API_KEY="sk-ant-..." \
  LITELLM_MASTER_KEY="$(openssl rand -hex 32)"
flyctl deploy --app sf-litellm --config fly/litellm.fly.toml
```

## Smoke

```bash
# Phoenix /v1/health (internal only — exec inside one of the Fly machines)
flyctl ssh console --app sf-phoenix -C "curl -sf http://localhost:6006/healthz"

# LiteLLM with master key
LITELLM_MASTER_KEY=$(flyctl secrets list --app sf-litellm --json | jq -r '.[]|select(.Name=="LITELLM_MASTER_KEY")|.Digest')
# (^ Digest is just the hash — actual value isn't exposed; copy from your password manager when set)

curl https://sf-litellm.fly.dev/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"ping"}]}'
```

A successful response should also produce a span visible at
`https://sf-phoenix.fly.dev/projects/sf-workspaces` (behind Cloudflare Access).

## Cloudflare Access (Phoenix UI)

`sf-phoenix.fly.dev` is publicly reachable by default. Front it with Cloudflare
Access using:
- Application type: Self-hosted
- Application domain: `sf-phoenix.fly.dev` (or a CNAME like `phoenix.solutionsfabric.com`)
- Policy: emails ending in `@teraflop.io` (Google SSO IdP)

## Secret rotation

```bash
# Rotate LiteLLM master key
flyctl secrets set --app sf-litellm LITELLM_MASTER_KEY="$(openssl rand -hex 32)"
flyctl deploy --app sf-litellm --config fly/litellm.fly.toml  # restart picks up the new secret

# Update every client that hits sf-litellm (workspace shim env, eval harness, etc.)
```

## Notes / gotchas

- `phoenix:latest`'s arm64 image SIGILLs on Apple Silicon (see compose comment) —
  irrelevant on Fly, which is amd64 native.
- LiteLLM's `arize_phoenix` callback uses HTTP/OTLP under the hood, **not** gRPC,
  for the trace export. The env vars in `litellm.fly.toml` set both endpoints so
  whichever the callback prefers is correct.
- `sf-phoenix.internal` resolves over IPv6 only. Fly machines support it natively;
  it does NOT work from outside the org's private network.
- Both apps idle-suspend except `sf-phoenix`, which has `min_machines_running=1`
  (Phoenix needs to stay up to receive OTLP). LiteLLM auto-wakes on the first
  request after a suspend.
