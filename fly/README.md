# Fly deploy: dev-agent-lens

Three Fly apps backing the Solutions-Fabric workspace-runner observability
stack (ENG2-1194) + team access (ENG2-1234). All in `teraflop` org, region `iad`.

**Inbound is tailnet-only as of ENG2-1234.** All three apps have zero
public IPs allocated. Outbound (LiteLLM → Anthropic, Phoenix → Supabase
Postgres) is unaffected — it uses Fly's egress NAT and doesn't require
any ingress.

| App | What | Internal URL | Tailnet URL |
|---|---|---|---|
| `sf-phoenix` | Arize Phoenix UI + OTLP collector | `sf-phoenix.internal:4317` (OTLP gRPC), `:6006` (UI) | `http://sf-tailscale-router:6006/` |
| `sf-litellm` | Patched LiteLLM proxy (`aowen14/litellm-oauth-fix`) | `sf-litellm.internal:4000` | `http://sf-tailscale-router:4000/` |
| `sf-tailscale-router` | Subnet router + TCP forwarders for the team's Tailnet | — | (the router itself) |

See `fly/router/README.md` for tailnet access details.

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

# 2. sf-litellm — no secrets needed for pure OAuth pass-through
flyctl apps create sf-litellm --org teraflop
flyctl deploy --app sf-litellm --config fly/litellm.fly.toml
```

## Auth model: OAuth pass-through

Clients ship their own Claude OAuth access token in `Authorization: Bearer`.
The proxy forwards it as-is to Anthropic, so usage attributes to the
token-holder's account — not a shared proxy identity. Phoenix sees the
request as a `litellm_request` span and logs input / output / token counts.

**Important:** use `/v1/messages` (Anthropic-native path), not
`/v1/chat/completions` (OpenAI-normalized path). The latter would force
LiteLLM to fall back to its config-level `ANTHROPIC_API_KEY` instead of
forwarding the request's bearer.

```bash
# Set ANTHROPIC_BASE_URL so the Anthropic SDK / claude-agent-sdk routes
# through the proxy. Pick the path based on where you're calling from:
export ANTHROPIC_BASE_URL=http://sf-tailscale-router:4000     # tailnet (laptops)
export ANTHROPIC_BASE_URL=http://sf-litellm.internal:4000     # another Fly app in teraflop org
export ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...                 # your OAuth access token
```

## Smoke

```bash
# Phoenix UI reachability (from a tailnet peer)
curl -sI http://sf-tailscale-router:6006/

# OAuth pass-through: an end-to-end call that should show up in Phoenix
curl http://sf-tailscale-router:4000/v1/messages \
  -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","messages":[{"role":"user","content":"reply: ping"}],"max_tokens":10}'
```

A successful response (`{"content":[{"type":"text","text":"pong"}],...}`) should
also produce a `litellm_request` span visible at the Phoenix UI
(`http://sf-tailscale-router:6006/`) with `input.value` and `output.value`
matching your prompt + reply.

## Access control

**Current model: tailnet-only (ENG2-1234).** Neither `sf-phoenix` nor
`sf-litellm` has public IPs allocated, and neither declares an
`[http_service]` block — they only listen on Fly's 6PN mesh. Reach them via:

1. **Tailnet** (preferred — see `router/README.md`):
   ```bash
   curl http://sf-tailscale-router:6006/   # Phoenix UI
   curl http://sf-tailscale-router:4000/   # LiteLLM
   ```

2. **`flyctl proxy`** (anyone with `flyctl auth login` + Teraflop org membership):
   ```bash
   flyctl proxy 6006:6006 --app sf-phoenix
   # then open http://localhost:6006 in a browser
   ```

3. **From another Fly app in the org**, hit `.internal` directly:
   ```
   curl http://sf-litellm.internal:4000/v1/messages ...
   ```

**Gotcha: PHOENIX_HOST=:: (dual-stack), not 0.0.0.0.** Fly's `.internal`
DNS resolves to IPv6; `PHOENIX_HOST=0.0.0.0` binds IPv4-only so all
inter-app traffic refuses on the IPv6 path (4317 dual-stacks automatically,
but Uvicorn doesn't). Verified by `flyctl proxy` returning Connection
reset until the bind was switched (ENG2-1194).

## Secret rotation

Only `sf-phoenix`'s `PHOENIX_SQL_DATABASE_URL` is a long-lived secret on the
Fly side. Rotate when the Supabase pooler password is rotated:

```bash
flyctl secrets set --app sf-phoenix \
  PHOENIX_SQL_DATABASE_URL="postgresql://...new-credentials...@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
# Auto-redeploys; Phoenix picks up the new DSN on restart.
```

`sf-litellm` holds no secrets — all auth flows through per-request OAuth
tokens supplied by the client.

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
