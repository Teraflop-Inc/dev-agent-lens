# DAL on Fly with Cloudflare R2

This is a private, receiver-only first stage. It does not enable history rebuilds,
publish a query API, or redirect existing capture. The 2 GB Machine is a starting
point for ingest validation, not a measured size for full-history queries.

## Account setup

Use the team's Fly organization and Cloudflare account. Enable R2 in Cloudflare,
create a dedicated private Standard bucket, then create an R2 S3 credential with
object read/write access scoped to that bucket. Wrangler's OAuth login is not an
S3 credential. Do not enable the bucket's public development URL or custom domain.

From the repository root, select a globally unique application name:

```sh
export DAL_APP=your-dal-store
export DAL_FLY_ORG=your-team
flyctl apps create "$DAL_APP" --org "$DAL_FLY_ORG"
flyctl volumes create dal_data --app "$DAL_APP" --region iad --size 10
flyctl ips allocate-v6 --private --app "$DAL_APP"
```

Keep compute near the existing producer. `iad` matches the example proxy's region;
adjust both the volume region and `primary_region` together for other deployments.

Import secrets from a mode-600 file outside the repository using stdin:

```text
DAL_SPAN_STORE=s3://YOUR_BUCKET/live?endpoint=YOUR_ACCOUNT.r2.cloudflarestorage.com&region=auto
AWS_ACCESS_KEY_ID=YOUR_BUCKET_SCOPED_ACCESS_KEY
AWS_SECRET_ACCESS_KEY=YOUR_BUCKET_SCOPED_SECRET
DAL_EVENTS_SECRET=YOUR_RANDOM_WEBHOOK_SECRET
```

```sh
flyctl secrets import --app "$DAL_APP" < /secure/path/dal-fly.env
flyctl config validate --strict --config fly/store.fly.toml --app "$DAL_APP"
flyctl deploy . --config fly/store.fly.toml --app "$DAL_APP" --ha=false
flyctl machines list --app "$DAL_APP"
flyctl ips list --app "$DAL_APP"
```

Verify exactly **one** receiver Machine and **only private** IP addresses. Never
scale the receiver horizontally or deploy with spare Machines: raw writes require
one writer. The immediate deployment strategy stops the old Machine before
starting its replacement. Keep imports stopped while the receiver writes.

## Private connectivity and validation

The receiver listens on IPv4; use `http://APP.flycast/v1/traces` through Flycast
from the same Fly private network. Do not use `APP.internal`, which resolves to
direct IPv6 addresses. Flycast is private even though the config uses
`http_service`; it must have no public IP allocation. OTLP has no application-level
authentication, so only trusted workloads should share this network. Laptop
access needs an explicit private-network route or an authenticated gateway.

Check `/healthz`, then send a uniquely identified synthetic span and verify it
exists in R2 after the receiver's 30-second flush interval. A successful HTTP
response alone does not prove persistence; the receiver buffers data in memory.
The Fly volume does not make that buffer crash-durable. Inspect flush failures and
object contents before connecting real producers.

Validate the S3 driver and DuckDB reads against R2 using the synthetic prefix
before copying private history. Use the inventories and comparison procedure in
`deploy/HOSTED.md`; a bucket listing alone is not a migration validation.

## Before production cutover

* Implement and measure incremental typed builds and atomic snapshot publication.
  The existing full rebuild clears derived data and can need tens of GB of RAM.
  Do not run `sync-loop` or full typed builds on this ingest Machine.
* Establish the supported private query path and measure its memory/concurrency.
* Add durable ingest/retry guarantees appropriate to the required loss tolerance;
  today's in-memory buffer may lose accepted spans on a crash.
* Backfill and reconcile history with one writer; preserve the original stores.
* Dual-write through an exporter/collector, verify new Claude and Codex sessions,
  and compare against Phoenix for a week before cutover.

R2 holds the durable objects. The volume is configuration and scratch space, not
the only copy of history. Size compute and scratch capacity from measured workload
before enabling query traffic.
