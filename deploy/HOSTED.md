# External object storage and hosted deployment

`compose.hosted.yml` uses an existing S3-compatible bucket without starting MinIO.
It can run on a Linux host with Docker Compose. It does **not** provision an AWS
account, Cloudflare subscription, public query API, or authentication gateway.

For a private Fly Machine with Cloudflare R2, see [the Fly setup guide](../fly/STORE.md)
and `fly/store.fly.toml`. That configuration starts only the receiver; typed builds
and production query serving remain separate rollout steps.

## Storage configuration

Copy `.env.hosted.example` to `.env.hosted`, fill in the destination and credentials,
and set mode 600. For Cloudflare R2, enable R2 in the intended account and create a
bucket-scoped object read/write credential. R2 exposes an S3-compatible endpoint;
use its account endpoint and region `auto`. For AWS S3, omit `endpoint` in the URI
and set the bucket's actual region. Prefer a dedicated DAL bucket or prefix.

```sh
docker compose --env-file deploy/.env.hosted -f deploy/compose.hosted.yml config --quiet
docker compose --env-file deploy/.env.hosted -f deploy/compose.hosted.yml build dal-otlp
docker compose --env-file deploy/.env.hosted -f deploy/compose.hosted.yml up -d
```

Only the receiver starts by default. It binds to loopback. Put an authenticated
TLS gateway in front before accepting remote clients; the existing OTLP endpoint
has no built-in authentication. Cloudflare Containers additionally requires an
eligible paid Workers plan; this Compose file is for a Docker host and is not a
Cloudflare Containers deployment manifest.

## One writer and a complete migration

Do not run imports against the same raw store while the receiver writes. Maintenance
services have an explicit profile so `up` cannot accidentally start a second writer.
Use `run --rm dal-sync dal ...` for bounded maintenance commands with the receiver
stopped. Never copy active derived partitions while a rebuild is clearing them.

Before cutover:

1. Inventory every source's span count, earliest/latest timestamp, and distinct
   session count. Keep source identifiers and import provenance.
2. Copy the historical raw and blob objects to a fresh destination prefix; retain
   the old store. Verify object sizes/checksums and row coverage before attaching
   readers. Use a manifest, not a bare `sync` exit status, as the completion gate.
3. Import missing recent source windows. Account for overlap by stable span ID
   and source. Raw imports are idempotent within a source, not across renamed sources.
   Session-file and proxy capture can describe the same work: do not add their
   token totals together without an explicit reconciliation rule.
4. Compare representative queries and sessions across the history transition and
   the newest data. Reuse `scripts/migration_oracle.py` where the producer remains
   available. Count excluded test sources separately.
5. Verify one new Claude session and one new Codex session have persisted, including
   person, session ID, tool kinds, and usage. Receiver acceptance alone is insufficient.
6. Change producer destinations only after validation. Preserve the old destination
   and capture configuration so rollback does not lose in-flight data.

## Typed layout limits

Use `dal store rebuild` for [incremental typed snapshots](../docs/incremental-store.md).
It rebuilds changed days and their affected trace/session context, then conditionally
publishes a complete manifest. Existing readers retain their previous files. The
legacy `store verify --from-parquet` typed builder still clears its legacy output;
do not serve that directory during a rebuild.

Bootstrap and schema upgrades still need a full build. A large history cannot be
sized from a small fresh-store test. Measure updater memory and provide the supported
private query path before cloud cutover.

Do not put private migration inventories or production settings in this public repo.
