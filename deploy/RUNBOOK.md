# Install runbook

For the person installing Dev Agent Lens on one Linux machine. Every step is a
command and what you should see. Rehearsed end to end on a fresh Ubuntu 24.04 VM
on 2026-09-15; times are from that run.

## 0. What you need

| Thing | Requirement |
|---|---|
| Machine | Linux, x86_64. 4 CPU, 16 GB RAM, 60 GB disk for the first year of a small team. See "How big" below. |
| Software | Docker 24 or newer with the compose plugin (`docker compose version` prints something). Nothing else. |
| Network | The people whose Claude Code you want to see must reach this machine on port 4000 (over the VPN is fine). The machine itself needs no inbound access from the internet, and outbound only to `api.anthropic.com`. |
| Bundle | Five files from us: `dal-images.tgz` (1.1 GB), `compose.yml`, `litellm.yaml`, `identity.yaml`, `.env.example`. |

Put the five files in one directory, say `~/dal`, and run everything from there.

## 1. Load the images (about 1 minute)

```
cd ~/dal
gunzip -c dal-images.tgz | docker load
```

You should see three lines ending in:

```
Loaded image: dal:0.1
Loaded image: quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z
Loaded image: dal-proxy:0.1
```

## 2. Fill in the settings (1 minute)

```
cp .env.example .env
chmod 600 .env
```

Edit `.env`. Four lines matter; leave the rest as they are.

```
DAL_IMAGE=dal:0.1
DAL_STORE_USER=<pick a name>
DAL_STORE_PASSWORD=<pick a long password>
LITELLM_MASTER_KEY=<pick another long password>
```

Leave `PHOENIX_SQL_DATABASE_URL` and `DAL_SOURCES` empty. This install has no
old system to pull from; spans arrive from the proxy.

## 3. Start it (under a minute)

```
docker compose --profile capture up -d
```

You should see `Network dal_default Created`, two volumes, then five containers
`Created` and `Started`: `dal-minio-1`, `dal-dal-sync-1`, `dal-dal-otlp-1`, `dal-dal-oracle-1`,
`dal-litellm-1`. Rehearsed: 31 seconds.

## 4. Check it is up

```
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
```

All five say `Up`; `minio`, `dal-otlp` and `litellm` say `(healthy)` within a minute.
Rehearsed: 97 seconds from the start of step 1 to here.

```
curl -s http://127.0.0.1:4318/health
curl -s http://127.0.0.1:4000/health/liveliness
```

The first prints `ok received=0 written=0 flushes=0 failed=0`. The second prints `"I'm alive!"`.

## 5. Point Claude Code at it

On each person's machine, in `~/.claude/settings.json`, add the `env` block (merge it
if the file already has one). `<store-host>` is this machine's address on the VPN.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://<store-host>:4000"
  }
}
```

This works for the CLI and for the Claude Code desktop app (the **Code** tab; a plain
chat with a folder attached does not go through it). Both read that file. To scope it to
work folders instead of the whole machine, put the same block in `<folder>/.claude/settings.json`;
only sessions opened in that folder route through the proxy. Restart the desktop app after editing. People keep logging in the way they do today;
the proxy forwards their own login to Anthropic and keeps a copy of each call.

Have one person run any prompt. Then on the server:

```
curl -s http://127.0.0.1:4318/health
```

`received` is now greater than zero (one short prompt produced 8). That is capture
working. Rehearsed from a laptop outside the VM through an SSH tunnel standing in for
the VPN: the prompt answered in 7 seconds and the spans were in the store 30 seconds later.

## 5b. Put names on the spans (2 minutes per person)

Every call carries the account id of whoever made it, never a name. `identity.yaml` is the
only place a name comes from. Each person runs this on their machine and sends you the line:

```
python3 -c "import json;print(json.load(open('$HOME/.claude.json'))['oauthAccount']['accountUuid'])"
```

Add one entry per person and restart the sync service (`docker compose restart dal-sync`);
the next rebuild stamps `person` on every span. Ids the file does not name show up in
`docker compose logs dal-sync` as `unnamed account=...`.

```yaml
people:
  - name: Ada Lovelace
    email: ada@example.com
    accounts:
      - {uuid: <the id she sent>, verified: true}
```

## 6. Ask the first question

Do this after step 5 has landed at least one span; on an empty store the rebuild
prints a query error instead of a count. The store rebuilds its query layout every
15 minutes on its own. To see the new spans right away:

```
docker compose exec dal-sync bash -c 'DAL_SPAN_LAYOUT=typed dal store verify --from-parquet "$DAL_RAW_GLOB"'
docker compose exec -e DAL_SPAN_LAYOUT=typed dal-sync dal store query "SELECT model_name, count(*) AS calls, sum(tokens_completion) AS out_tokens FROM spans GROUP BY 1 ORDER BY 2 DESC"
```

A table with one row per model (`DAL_SPAN_LAYOUT=typed` is what selects the query
layout; without it you get the raw columns). From here, `docs/cookbook.md` has the fifteen
questions people ask most.

## 7. Bring in Claude sessions from before today (optional)

Every Claude Code session is also a file on the person's disk. To load a folder of
them, name the work with `--include`: it matches the project path each session was
recorded in, so `'*acme*'` picks up everything under a folder with `acme` in its path
and leaves personal sessions out. There is no "all" on purpose.

```
docker compose run --rm -v ~/.claude/projects:/claude:ro dal-sync \
  ingest-sessions --sessions-dir /claude --include '*acme*' --project my-claude --to-store --yes
```

Three weeks of one person's sessions (23,653 spans) took 12 seconds end to end on a laptop.

## 8. Day to day

| Want | Command |
|---|---|
| Is it healthy | `docker compose logs --tail 5 dal-oracle` (runs daily; says `healthy:` or `BREACH`, `inconclusive:` on a day with no traffic, `operational:` when the check could not run) |
| How much is in it | `docker compose exec dal-sync dal store status` |
| Stop / start | `docker compose --profile capture down` / `up -d` (data stays in the volumes) |
| Update | load the new `dal-images.tgz`, change `DAL_IMAGE` in `.env`, `up -d` again |
| Back up | the data is the two Docker volumes `dal_minio` and `dal_dal-data`; back them up like any volume |

## How big

Measured: about 3 KB of store per span, twice that on disk to be safe. A busy
developer produces roughly 2,000 to 5,000 spans a day.

| Spans stored | Disk for the store | RAM | CPU |
|---|---|---|---|
| 1 GB (about 350k spans, a small team for a year) | 5 GB | 8 GB | 2 |
| 10 GB (about 3.5M spans) | 30 GB | 16 GB | 4 |
| 100 GB (about 35M spans) | 250 GB | 64 GB, `DAL_DUCKDB_MEMORY=48GB` | 8, `DAL_BUILD_DETERMINISTIC=0` |

Too small looks like: the query layout rebuild in the `dal-sync` log says `Killed`
or takes more than an hour. Give it RAM, or set `DAL_BUILD_CHUNK_DAYS=3`.

## If something is wrong

| You see | Do |
|---|---|
| `exec format error` on start | The images were built for the wrong CPU type. Ask us for an x86_64 bundle. |
| `received` stays 0 after a prompt | The person's machine cannot reach port 4000. `curl http://<store-host>:4000/health/liveliness` from their machine. |
| `litellm` not healthy | `docker compose logs litellm`; the usual cause is a typo in `litellm.yaml`. |
| A query says a table is missing | Run the rebuild in step 6 once. |
| A query says `model_name` not found | Add `-e DAL_SPAN_LAYOUT=typed` to the exec, as in step 6. |
