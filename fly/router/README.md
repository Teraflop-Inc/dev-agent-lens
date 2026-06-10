# sf-tailscale-router

Tailscale-on-Fly router that gives Teraflop team members on the tailnet
two ways to reach Fly apps in the `teraflop` org. ENG2-1234.

## What it does

1. **Subnet routing** — advertises `fdaa:58:460c::/48` (the Fly org's IPv6
   slice of `fdaa::/16`) so any tailnet peer can hit Fly machines directly
   by their IPv6 address. Works for any port; works for every Fly app in
   the org with zero per-app config.
2. **`tailscale serve` + socat relays** — registers TCP forwarders that
   accept connections on the router's tailnet MagicDNS name and proxy
   them to specific `<app>.internal` targets. Friendlier than memorizing
   IPv6 addresses, and survives Fly machine restarts.

## Reach a service from the tailnet

After deploy + admin approvals (below), team members on the tailnet get:

| Service          | URL                                              |
|------------------|--------------------------------------------------|
| Phoenix UI       | `http://sf-tailscale-router:6006/`               |
| LiteLLM proxy    | `http://sf-tailscale-router:4000/`               |

(Replace `sf-tailscale-router` with whatever suffix MagicDNS assigns if
multiple nodes exist — `tailscale status | grep sf-tailscale-router`.)

Or go directly via IPv6 (requires no MagicDNS, works for any port):

```bash
# Phoenix
curl -6 "http://[fdaa:58:460c:a7b:895:aec8:37dc:2]:6006/"

# Any other Fly app in the org — look up its machine IP with `flyctl status`
```

## Deploy

```bash
cd dev-agent-lens

# First time only — create the app and a 1GB persistent volume for
# tailscaled state (without persistence, every redeploy creates a new
# tailnet identity → ghost nodes accumulate).
flyctl apps create sf-tailscale-router --org teraflop
flyctl volumes create tailscale_state --app sf-tailscale-router --region iad --size 1

# Stash the auth key. Mint at
#   https://login.tailscale.com/admin/settings/keys
# with: reusable=on, ephemeral=off, expiration=90d.
read -s TS_AUTHKEY
flyctl secrets set TS_AUTHKEY="$TS_AUTHKEY" --app sf-tailscale-router
unset TS_AUTHKEY

flyctl deploy --app sf-tailscale-router --config fly/router.fly.toml
```

## Post-deploy admin steps

**Approve the subnet route.** Visit
<https://login.tailscale.com/admin/machines>, find `sf-tailscale-router`,
click "Edit route settings", and approve `fdaa:58:460c::/48`. Without
this the route is advertised but no peers will use it.

That's the only manual step. No split-DNS config needed (the friendly
URLs use the router's tailnet hostname, not `*.internal`).

## Adding a new exposed service

Add an entry to `TS_SERVE` in `router.fly.toml` (newline- or
semicolon-separated). Format: `<router-port>=tcp://<app>.internal:<port>`.

```toml
[env]
  TS_SERVE = """
  6006=tcp://sf-phoenix.internal:6006
  4000=tcp://sf-litellm.internal:4000
  4317=tcp://sf-phoenix.internal:4317
  """
```

`flyctl deploy` re-runs `tailscale serve reset` on boot and rebuilds the
forwarders, so removed entries unstick cleanly.

## Verify

From your laptop, with Tailscale running:

```bash
# Route is approved and accepted
tailscale status --json | jq '.Peer | to_entries[] | .value |
  select(.HostName | startswith("sf-tailscale-router")) |
  {HostName, Online, PrimaryRoutes}'

# Service-level: Phoenix UI
curl -sI http://sf-tailscale-router:6006/

# Service-level: LiteLLM
curl -sI http://sf-tailscale-router:4000/

# Direct subnet route (any Fly app, any port)
curl -6 "http://[<fly-machine-ipv6>]:<port>/"
```

## Why this architecture (vs. wrapping each app's image)

- `sf-phoenix` is **distroless** — no shell, no apt, can't `apk add tailscale`.
  Wrapping it would mean rebuilding from a non-distroless base, losing
  Arize's minimal supply-chain story.
- `sf-litellm` is Wolfi and could be wrapped, but doing it for only one
  of two apps is the worst of both worlds.
- A subnet router is **O(1) infrastructure**: a new Fly app gets
  tailnet access for free, with no image work. Adding a friendly TCP
  forwarder is a one-line `TS_SERVE` change.

## Why both subnet routing AND `tailscale serve`

These solve different problems:

- **Subnet routing** handles arbitrary destinations: any Fly machine, any
  port. Works without per-target config but requires the consumer to
  know IPv6 addresses (which change on machine recycling).
- **`tailscale serve`** gives **stable, friendly URLs** anchored to the
  router's MagicDNS name. The router resolves `<app>.internal` at
  connection time, so machine restarts don't break the forwarder.

Both are layered on the *same* tailnet node, so there's no extra
operational cost to having both.

## The userspace-networking + socat sandwich

`tailscaled` runs in userspace mode (no kernel TUN required → no
`NET_ADMIN` / `--privileged` needed on Fly). Consequences:

1. Subnet-routed traffic transits via the userspace netstack — works for
   TCP destined for the advertised subnet. Does **not** work for ICMP
   (you can't `ping6` Fly machines from the tailnet through the router).
2. Inbound traffic to the router's own tailnet IP only reaches services
   registered via `tailscale serve`. Arbitrary `0.0.0.0:port` listeners
   are invisible to tailnet peers in userspace mode.
3. `tailscale serve --tcp` only accepts `localhost`/`127.0.0.1` as the
   forward target. To proxy onward to a 6PN IPv6 target, we sandwich a
   `socat` instance: `tailscale serve --tcp=N tcp://localhost:N` →
   `socat TCP4-LISTEN:N,bind=127.0.0.1 TCP6:<target>:N`.

Whole stack:

```
peer laptop ── wireguard ──> userspace tailscaled (router)
                              ├─[serve TCP N]── 127.0.0.1:N → socat → fdaa:58:460c:…:N
                              └─[subnet route fdaa:58:460c::/48]── direct to target
```

## Operational notes

- **Single machine, no standby.** A standby machine would need its own
  volume + would register a second tailnet identity competing for the
  same hostname. Keep it at one.
- **Persistent volume on `/var/lib/tailscale`.** Without this the node
  identity is regenerated each deploy and the admin's route approval
  doesn't transfer.
- **`TS_AUTHKEY` lives only on this app.** It was briefly set on
  `sf-litellm` and `sf-phoenix` during early experimentation and has
  been cleaned up.
- **LiteLLM and Phoenix both bind `::` (dual-stack).** Fly's mesh is
  IPv6-only; an IPv4-only listener gets ECONNREFUSED from the router.
  Phoenix had this fixed in M2 (`PHOENIX_HOST=::`); LiteLLM got the same
  treatment in this work (`--host ::` in `litellm.fly.toml`).
