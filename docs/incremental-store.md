# Incremental typed snapshots

`dal store rebuild` turns the configured store's `spans_raw/day=YYYY-MM-DD/`
Parquet files into a published typed snapshot. It works with local directories and
S3-compatible storage. Use `dal store query --layout typed ...` to read it.

```sh
dal store rebuild          # bootstrap, then update changed inputs
dal store rebuild --full   # explicitly rebuild every day
dal store status           # reports the published typed snapshot
```

The first run does a full build. Changes to the typed build code, incremental
build code, identity mapping file, or compression level also force a full build.
Run large initial builds and schema upgrades on an appropriately sized host.
Existing legacy `spans_typed` files stay readable until the first snapshot is
published. After that, use `store rebuild --full`, rather than the legacy
`store verify --from-parquet` destructive typed rebuild.

## What changes on an update

The updater lists raw object revisions (S3 ETags, or local modification time and
size). New, replaced, and deleted files identify the changed days. Raw partitions
must contain exactly one valid `day=` path component.

Only changed days' JSON payloads are parsed to refresh cached identity facts.
Cached facts preserve the same producer parsing as the full typed builder. Trace
and session links identify any other days affected by late parents, changed
attribution, or deleted identity records. Those days are rebuilt too. A day that
happens to contain an unrelated session does not pull that session's other days
into the rebuild.

The builder reads global cached identity facts to give all rebuilt rows their
complete attribution context. This is incremental payload processing, not zero
work proportional to history: listing objects, checking row counts through
Parquet metadata, and scanning cached identity facts still scale with the store.
Long or malformed sessions can legitimately connect many days. Memory limits
bound the work; failures preserve the current snapshot instead of dropping context.

## Publication and readers

Every update writes new files under `_typed/generations/<id>/`. Unchanged days
continue to reference their existing generation. Hot partitions and payload blobs
are verified for row coverage before publication; shared blobs are exposed once
by reference in the `blobs` view.

The entire manifest is published in one conditional write to `_typed/current.json`.
Local publication uses a lock and atomic rename. S3 publication requires conditional
PUT support (`If-Match`/`If-None-Match`); unsupported implementations fail rather
than publishing unsafely. Each generation also retains its manifest.

Readers resolve the manifest once when attaching their views and keep those exact
files. Existing readers finish on the previous snapshot, and newly attached readers
see the complete new snapshot. Generation files are never cleared by an update.
There is deliberately no automatic garbage collection yet: retain generations
while readers might still reference them. Deleted raw rows disappear from current
query results but are not erased from retained generations.

If a competing updater publishes first, the stale publication fails. Run one updater
to avoid wasted builds. The raw receiver can keep appending unique files during a
build; new files are picked up on the next pass. Replacing or deleting a pinned input
during the build aborts publication. Raw imports still require a single raw writer.

## Hosted operation

The deployment image supports `typed-loop`, which invokes `dal store rebuild` every
`TYPED_INTERVAL` seconds (default 60) and retries failed updates without replacing the
last good snapshot. It does not import raw data. In the external-store Compose setup:

```sh
docker compose --env-file deploy/.env.hosted -f deploy/compose.hosted.yml \
  --profile typed up -d dal-typed
```

Set `DAL_DUCKDB_MEMORY` and `DAL_DUCKDB_TMP` for the updater. Budget additional RAM
for Python, network buffers, and the receiver; the DuckDB limit is not a process RSS
limit. Measure the largest changed-day/session workload before sizing a Fly updater.
The receiver-only Fly configuration does not automatically start an updater.

On migration, finish and reconcile the historical raw copy first, run the initial
typed build on the large migration host, then point the updater and queries at the
same canonical store. A new capture prefix and an isolated migration prefix do not
become one history merely by running this command against one of them.

## Verification

`uv run pytest tests/storage/test_incremental.py` exercises local publication,
cross-day identity, removal, reader stability, and concurrent updates. The optional
`test_object_store_snapshot_publication` also exercises a real S3-compatible store:
set `DAL_INCREMENTAL_TEST_STORE` to an existing validation bucket/prefix and
`DAL_INCREMENTAL_TEST_ENV` to a private env file containing its AWS credentials.
It writes only to a fresh validation generation below that prefix, retained for
inspection. No LLM provider is needed for this storage test.
