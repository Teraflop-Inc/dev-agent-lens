#!/bin/sh
# Entrypoint for sf-tailscale-router (ENG2-1234).
#
# Brings up tailscaled in userspace-networking mode, registers as a node on
# the Teraflop tailnet, advertises Fly's 6PN /48 (so tailnet peers can reach
# Fly app machines by raw IPv6), and uses `tailscale serve` to expose
# specific TCP services at well-known ports on this node's MagicDNS name.
#
# Why both subnet routing AND tailscale serve:
#   - Subnet routing gives raw IPv6 reachability into the Fly mesh
#     (`<machine-ipv6>:<port>` works from any tailnet peer).
#   - `tailscale serve` exposes friendly TCP ports on the router's MagicDNS
#     hostname (`sf-tailscale-router.<tailnet>.ts.net:6006` → Phoenix UI),
#     which is more useful for day-to-day team workflows.
#   - In userspace-networking mode, tailscaled accepts inbound traffic to
#     advertised subnet IPs (forwarded via the userspace netstack) AND to
#     ports registered via `tailscale serve` on its own tailnet IP — but
#     NOT to arbitrary ports bound on the container's loopback. That's why
#     we need `serve` explicitly, not just a port-bound dnsmasq/etc.
#
# Required env:
#   TS_AUTHKEY    — Tailscale auth key (set via `flyctl secrets set`).
# Optional env:
#   TS_HOSTNAME   — defaults to FLY_APP_NAME ("sf-tailscale-router").
#   TS_ROUTES     — comma-separated subnets to advertise. Default is the
#                   Teraflop Fly org's IPv6 /48: fdaa:58:460c::/48.
#   TS_SERVE      — newline- or semicolon-separated "PORT=tcp://host:port"
#                   entries to register with `tailscale serve --tcp`. Default
#                   exposes Phoenix UI (6006) and LiteLLM proxy (4000).

set -eu

: "${TS_AUTHKEY:?TS_AUTHKEY is required (flyctl secrets set TS_AUTHKEY=...)}"
TS_HOSTNAME="${TS_HOSTNAME:-${FLY_APP_NAME:-sf-tailscale-router}}"
TS_ROUTES="${TS_ROUTES:-fdaa:58:460c::/48}"
# Default: expose Phoenix UI and LiteLLM proxy via friendly ports. Each
# entry is "<tailnet-port>=<target-url>". The target uses Fly's stable
# app-level DNS (`<app>.internal`) so we ride out machine restarts.
TS_SERVE_DEFAULT="6006=tcp://sf-phoenix.internal:6006
4000=tcp://sf-litellm.internal:4000"
TS_SERVE="${TS_SERVE:-$TS_SERVE_DEFAULT}"

TS_SOCK=/var/run/tailscale/tailscaled.sock

echo "[router] starting tailscaled (userspace networking)..."
/usr/sbin/tailscaled \
    --tun=userspace-networking \
    --state=/var/lib/tailscale/tailscaled.state \
    --socket="$TS_SOCK" \
    --socks5-server=localhost:1055 \
    >/var/log/tailscaled.log 2>&1 &
TAILSCALED_PID=$!

# Wait for tailscaled to open its control socket. We can't use
# `tailscale status` as the readiness check because it exits non-zero
# when the daemon is up but not yet logged in — exactly the state we're
# trying to detect to then *do* the login.
i=0
until [ -S "$TS_SOCK" ]; do
    i=$((i + 1))
    if [ "$i" -gt 300 ]; then
        echo "[router] tailscaled socket never appeared after 30s; daemon log follows:" >&2
        cat /var/log/tailscaled.log >&2 || true
        exit 1
    fi
    sleep 0.1
done

echo "[router] authenticating as ${TS_HOSTNAME}; advertising routes ${TS_ROUTES}"
/usr/bin/tailscale --socket="$TS_SOCK" up \
    --authkey="${TS_AUTHKEY}" \
    --hostname="${TS_HOSTNAME}" \
    --advertise-routes="${TS_ROUTES}" \
    --accept-routes=false \
    --reset

# Reset any prior serve config so re-deploys converge on whatever the
# current TS_SERVE says — without this, removed entries linger.
/usr/bin/tailscale --socket="$TS_SOCK" serve reset || true

# Register each TCP forwarder. `tailscale serve --tcp` only accepts
# localhost targets, so we run a socat relay per entry that translates
# localhost:<port> ↔ Fly's IPv6 mesh target (e.g. sf-phoenix.internal:6006).
# Flow:
#   tailnet peer → tailscaled (serve) → localhost:port → socat → 6PN target
echo "$TS_SERVE" | tr ';' '\n' | while IFS= read -r entry; do
    # Skip blank lines (common when TS_SERVE has trailing newline).
    [ -z "$entry" ] && continue
    port="${entry%%=*}"
    target_url="${entry#*=}"
    port=$(echo "$port" | tr -d '[:space:]')
    target_url=$(echo "$target_url" | tr -d '[:space:]')
    if [ -z "$port" ] || [ -z "$target_url" ]; then
        echo "[router] WARN: skipping malformed serve entry: $entry" >&2
        continue
    fi
    # Strip "tcp://" prefix to get bare host:port for socat.
    target="${target_url#tcp://}"
    target_host="${target%:*}"
    target_port="${target##*:}"

    echo "[router] socat 127.0.0.1:$port -> $target_host:$target_port (v6)"
    # Listen on IPv4 loopback (where `tailscale serve --tcp tcp://localhost`
    # will deliver connections) and forward to the Fly IPv6 mesh target.
    # socat handles the v4→v6 protocol translation per-connection.
    # NB: no brackets around the hostname — socat reserves [...] for
    # literal IPv6 addresses, hostnames must be unwrapped.
    # `reuseaddr` + `fork` for concurrent connections.
    socat \
        "TCP4-LISTEN:$port,bind=127.0.0.1,reuseaddr,fork" \
        "TCP6:$target_host:$target_port,nodelay" \
        >>/var/log/socat.log 2>&1 &

    echo "[router] tailscale serve --tcp=$port tcp://localhost:$port"
    /usr/bin/tailscale --socket="$TS_SOCK" serve --bg --tcp="$port" "tcp://localhost:$port"
done

echo "[router] up. Approve the subnet route in admin if not already:"
echo "         https://login.tailscale.com/admin/machines"
echo "[router] exposed:"
/usr/bin/tailscale --socket="$TS_SOCK" serve status || true

# Surface the daemon log, but tie PID 1's lifetime to tailscaled itself —
# NOT to the tail. ENG2-1269: tailscaled was OOM-killed while PID 1 kept
# tailing its log, so Fly showed a healthy `started` machine for hours
# while the tailnet node was dark. Exiting here (non-zero, paired with
# `restart.policy = "always"` in router.fly.toml) makes Fly restart the
# machine, which re-runs this script and brings tailscaled back.
tail -F /var/log/tailscaled.log &

ec=0
wait "$TAILSCALED_PID" || ec=$?
echo "[router] tailscaled exited (code $ec) — exiting so Fly restarts the machine (ENG2-1269)" >&2
exit 1
