# Self-hosting Dev Agent Lens

One command. Four values in an env file. The step-by-step with expected output is `RUNBOOK.md`. No Phoenix, no S3 account, nothing else installed.

```bash
cp deploy/.env.example deploy/.env      # DAL_STORE_USER, DAL_STORE_PASSWORD, PHOENIX_SQL_DATABASE_URL
docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml exec dal-sync dal store status
```

## What runs

| Service | Job | Port |
|---|---|---|
| `minio` | S3-compatible object store holding the span store (Parquet, day partitions) | 127.0.0.1:9100, console 9101 |
| `dal-sync` | first pull from `SYNC_DAYS` ago, then every `SYNC_INTERVAL` s re-pulls the last `SYNC_OVERLAP_DAYS`; each span lands once; rebuilds the typed layout | none |
| `dal-otlp` | OTLP/HTTP receiver on :4318; a producer posts traces, they land in the store with no Phoenix in the path | none |
| `dal-oracle` | daily: asks the producer and the store the same fifteen questions over what the store holds (first pull's start date; `ORACLE_DAYS` overrides), then runs the content sentinel; exits non-zero and pages Slack on a mismatch, a breach, or a check that could not run. With no producer (`PHOENIX_SQL_DATABASE_URL` empty) only the sentinel runs | none |

For an external S3-compatible store, use the separate [hosted configuration](HOSTED.md). The local Compose file intentionally targets its MinIO service; setting `DAL_SPAN_STORE` in the shell does not override that local configuration.

## Recommended specs

Start with 4 CPUs and 16 GB RAM for a small team, then measure against your
actual corpus and retention window. Keep enough disk for the raw and derived
layouts, plus DuckDB spill. Set `DAL_DUCKDB_MEMORY` and `DAL_BUILD_CHUNK_DAYS` to
bound rebuild memory. A large historical corpus can require substantially more
memory and rebuild time; the hosted runbook describes the migration gates.

## Shipping the image instead of the source

An install needs three images, one compose file, one proxy config, an identity file and an env example (the five files `RUNBOOK.md` names), not this repository:

```
docker build --platform linux/amd64 -f deploy/Dockerfile -t dal:<tag> .
docker tag aowen14/litellm-oauth-fix:latest dal-proxy:0.1     # our LiteLLM fork, neutral name for customers
docker save dal:<tag> quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z dal-proxy:0.1 | gzip > dal-images.tgz
# on the target: gunzip -c dal-images.tgz | docker load; DAL_IMAGE=dal:<tag> in deploy/.env
```

Build for the target's architecture. An image built on an Apple laptop is arm64
and fails on an x86 server with `exec format error`; ship images for the target architecture. Ship MinIO too, and pull it from quay.io: Docker Hub no longer serves `minio/minio`
(denied from every network tried on 2026-09-11).

## Pointing a producer at the store (no Phoenix)

Any OpenTelemetry exporter that speaks OTLP over HTTP can write here. The compose
file ships one: `docker compose --profile capture up -d` adds a LiteLLM proxy on
port 4000 already wired to the receiver; Claude Code points `ANTHROPIC_BASE_URL` at
it and logins pass through (see `RUNBOOK.md`, step 5). For a LiteLLM you run
elsewhere, keep its `arize_phoenix` callback and set four env vars:

```
PHOENIX_COLLECTOR_HTTP_ENDPOINT=http://<store host>:4318/v1/traces
OTEL_EXPORTER_OTLP_ENDPOINT=http://<store host>:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=<source name>
```

The first one matters: the callback picks HTTP from that name. `PHOENIX_COLLECTOR_ENDPOINT`
(no `HTTP`) means gRPC and the receiver refuses it; neither set means Arize's hosted
service and a demand for an API key. Found on the 2026-09-15 rehearsal.

Spans buffer for up to 500 rows or 30 s and land in `spans_raw` under that source;
the sync loop's typed rebuild picks them up on its next pass. `GET /health` prints
`ok received=N written=N flushes=N failed=N`. The receiver is idempotent per span, so exporter
retries are safe. Leave `PHOENIX_SQL_DATABASE_URL` empty and `DAL_SOURCES` empty
when this is the only producer; the sync loop then only rebuilds the typed layout.

## Bringing your own Claude folder (no producer at all)

```bash
docker compose -f deploy/compose.yml run --rm -v ~/.claude/projects:/claude:ro dal-sync \
  ingest-sessions --sessions-dir /claude --include '*your-repo*' --project my-claude --to-store --yes
docker compose -f deploy/compose.yml exec -e DAL_SPAN_LAYOUT=typed dal-sync dal store query \
  "SELECT model_name, count(*), sum(tokens_completion) FROM spans GROUP BY 1 ORDER BY 2 DESC"
```

Measured on a laptop, 23,653 spans from three weeks of sessions: ingest 4 s, typed build 0.7 s, first answer 9 ms. Twelve seconds end to end.

## Operating it

- **Producer credentials** live only in `deploy/.env` (mode 600) and reach the `dal-*` containers as env. The store URI carries none.
- **Sources** are declared as `DAL_SOURCES=name=project,name2=project2`; each becomes a `source` stamp so one store holds several projects without blending them.
- **Accuracy**: `/data/oracle/<stamp>.log` and `.json` in the `dal-data` volume; `docker compose logs dal-oracle` for the last verdict. A mismatch is a real disagreement between producer and store on a cookbook question; investigate before trusting either.
- **Schema drift**: see `docs/schema-resiliency.md`. A new producer lands as-is; typed columns it does not fill are NULL, its vocabulary stays whole in `attributes_rest`.
- **Backups**: the store is a bucket. Back it up like one. If lost, re-sync from the producer; if the producer is gone, the store is the copy.

## What this does not include

- The capture side (LiteLLM proxy, OTel exporter, hooks). This is the storage and query half; capture keeps running wherever it runs today.
- Multi-writer coordination. One `dal-sync` per store.
- TLS in front of MinIO. Loopback-bound by default; put your proxy or VPN in front to expose it.
