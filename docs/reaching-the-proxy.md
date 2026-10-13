# Reaching the capture proxy

The capture proxy is a LiteLLM server that Claude Code talks to instead of
`api.anthropic.com`. It forwards each request unchanged and writes a span for every
call. Capture only works for a client that can reach the proxy, so the first decision
is where the proxy runs and how clients find it.

There are three setups. Pick by who needs to reach the proxy.

| Setup | Who it is for | Clients use | Status |
|---|---|---|---|
| [1. On your machine](#1-on-your-machine) | One person, or trying DAL out | `http://localhost:4000` | Supported |
| [2. On a private network](#2-on-a-private-network) | A team on a VPN or tailnet, plus their containers and sandboxes | `http://<proxy-name>:4000`, a DNS name, never an IP | Supported |
| [3. On the public internet](#3-on-the-public-internet) | Devices that cannot join the private network | `https://<public-host>` with a per-person key | Not shipped |

Which Claude Code surfaces honour the proxy setting (terminal, headless `-p`, desktop
app, IDE panels) is a separate question from how they reach it. For that, and for the
`claude-lens` wrapper versus a `settings.json` entry, see [Proxy setup](proxy-setup.md).

## 1. On your machine

The self-host stack in `deploy/` includes the proxy as the optional `capture`
profile. It needs Docker with the compose plugin and nothing else. No Anthropic API
key lives on the proxy: a Claude Code login is passed through to Anthropic as is.

```sh
git clone https://github.com/Teraflop-Inc/dev-agent-lens.git
cd dev-agent-lens
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
```

Edit `deploy/.env`. Set these four lines; leave the rest as they are.

```
DAL_STORE_USER=<pick a name>
DAL_STORE_PASSWORD=<pick a long password>
LITELLM_MASTER_KEY=<pick another long password>
DAL_LITELLM_BIND=127.0.0.1
```

`DAL_LITELLM_BIND=127.0.0.1` keeps the proxy on this machine only. The example file
binds `0.0.0.0`, which is setup 2.

Start the stack:

```sh
docker compose -f deploy/compose.yml --profile capture up -d --build
curl -s http://127.0.0.1:4000/health/liveliness   # "I'm alive!"
curl -s http://127.0.0.1:4318/health              # ok received=0 written=0 ...
```

The first start builds the DAL image from the checkout, which takes a few minutes.

Send one prompt through the proxy:

```sh
ANTHROPIC_BASE_URL=http://localhost:4000 claude -p "reply with the word ok"
curl -s http://127.0.0.1:4318/health              # received is now above 0
```

`received` counts spans the receiver accepted. To see them as rows:

```sh
docker compose -f deploy/compose.yml exec dal-sync bash -c \
  'DAL_SPAN_LAYOUT=typed dal store verify --from-parquet "$DAL_RAW_GLOB"'
docker compose -f deploy/compose.yml exec -e DAL_SPAN_LAYOUT=typed dal-sync \
  dal store query "SELECT source, model_name, count(*) AS calls FROM spans GROUP BY 1, 2"
```

Expect a row with `source` = `claude-code` (the `CAPTURE_SOURCE` default) and the model
Claude Code used, plus a row with no model for the proxy's own spans. Spans can take up
to 30 seconds to land, because the receiver buffers before it writes.

To route every Claude Code session on this machine, not only the ones you start with
the variable, read the tradeoff in [Proxy setup](proxy-setup.md) first. Pointing all of
Claude Code at a proxy means Claude Code stops working whenever the proxy is down.

## 2. On a private network

Run the same stack (setup 1) on one machine that everyone can reach over the VPN or
tailnet, with the proxy bound to that network instead of loopback. Clients set
`ANTHROPIC_BASE_URL=http://<proxy-name>:4000`. [The install runbook](../deploy/RUNBOOK.md)
walks a team install from start to finish.

### Name the proxy, never pin its address

Clients must use a DNS name for the proxy: the VPN's own name for the machine (for
example a Tailscale MagicDNS name) or a record you control. Do not write the proxy's IP
address into configs, scripts or installers.

A VPN address belongs to the machine's VPN identity, not to the machine. When that
identity is lost, the machine signs up again and usually gets a new address. Every
pinned copy of the old address then fails, and in scripts, scheduled jobs and sandboxes
that failure is easy to miss until someone looks for spans that were never written.

What keeps the address stable on a VPN such as Tailscale:

| Risk | Fix |
|---|---|
| The node's key expires and the machine has to sign up again | Turn off key expiry for the proxy node in the VPN's admin console |
| The VPN's state is on disk that is wiped on restart or redeploy (a container's filesystem, a cloud VM's root disk) | Keep the VPN daemon's state directory on a persistent volume |
| The VPN daemon dies and nothing restarts it | Make the daemon, not a log tail or a shell, the process the supervisor watches, so a crash restarts the machine |
| The machine is rebuilt from scratch | Most VPNs let an admin assign a fixed address to a node; clients still use the name |

With those in place, a restart of the VPN daemon brings the proxy back on the same
address. The name is still the only thing clients should hold, because it is what keeps
working if the address does change.

### Containers and sandboxes that cannot resolve the name

Some runtimes do not use the host's resolver. A Docker container, or a micro-VM guest
whose DNS is not the VPN's, will fail to resolve the VPN name even though the host can.

Resolve the name on the host when the container or sandbox starts, and pass the result
in, for example:

```sh
PROXY_IP=$(tailscale ip -4 <proxy-name>)   # or: dig +short <proxy-name>
docker run -e ANTHROPIC_BASE_URL="http://$PROXY_IP:4000" ...
```

Keep the name in the config and do the lookup at start. Each run then gets the current
address, and nothing stores it. If the lookup is skipped, the name is still in the URL
and the guest fails at once with a resolution error instead of quietly reaching an old
address.

The guest also needs network access to the proxy's address. A runtime that denies
egress by default needs an allow rule for that address and port.

### Check it after a change

From each kind of client (a laptop, an always-on box, one container or sandbox), before
and after restarting the VPN daemon on the proxy machine:

```sh
curl -s http://<proxy-name>:4000/health/liveliness   # "I'm alive!"
```

From inside a container or sandbox, use the resolved address as above.

## 3. On the public internet

Not shipped. A device that cannot join the private network has no supported path to
the proxy yet.

What a public endpoint needs before it is safe:

- **TLS** in front of the proxy. Claude Code sends the user's login token on every
  request.
- **Per-person keys.** LiteLLM virtual keys identify the caller and can be revoked one at
  a time. Do not rely on an IP allowlist: client addresses move, and so can the proxy's
  (see setup 2).
- **A decision about passthrough.** The bundled proxy config lets any Claude Code login
  through without the master key. That is fine on a private network and wrong on the
  internet, where it makes the proxy an open relay for anyone with an Anthropic login.

Until then, run setup 1 on the device that cannot reach the network, or bring its
session files in afterwards ([Capture setup](codex-capture.md)).
