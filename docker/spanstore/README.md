# Span-store backends

ENG2-1591. The open-source product must not force S3, and it has to deploy into highly
regulated, on-prem environments — so where the trace store lives has to be pluggable.

**Everything in this directory is local development tooling: default credentials,
loopback-bound, never to be deployed.** The store itself is production code; these
compose files are not.

DAL's trace store is hive-partitioned Parquet. Where those files physically live is a
connection string, not an architecture. This directory stands up the backends locally so
that claim gets tested instead of asserted.

## One file per backend

Each starts on its own in seconds and they share no state, so bring up only what you need.

```bash
docker compose -f docker/spanstore/minio.yml     up -d    # S3-compatible
docker compose -f docker/spanstore/seaweedfs.yml up -d    # a second, independent S3
docker compose -f docker/spanstore/all.yml       up -d    # both S3 servers, for parity runs
docker compose -f docker/spanstore/doltgres.yml  up -d    # Forge only: not a DAL store, not in all.yml
```

Pick **one** start method per machine. The single files and `all.yml` are different compose
projects with different volumes, and the fixed container names collide, so switching
between them yields an empty store or a name clash.

**`dal sync` writes to the store once one has been chosen.** After `dal store use`, every
synced batch is appended to `spans_raw` (partitioned by day, one file per batch, never
cleared); `dal store status` shows what landed. Verified 2026-09-04 against the Supabase
Phoenix: one day of a production project into MinIO, matching a direct Postgres count
exactly, and the typed layout built from it with identity on 100% of rows. Direct
(Postgres/SQLite) sources land in the native span shape; REST sources have their flattened
`attributes.*` re-nested best-effort. Every landed row is stamped with the configured
source name in a `source` column, so one store can hold several sources.

**Reading it back.** Three commands read the store; nothing is copied out first. What
each dataset is, and how to rebuild, verify, move or retire it, is in
[docs/span-store-formats.md](../../docs/span-store-formats.md).

```bash
dal store status                                   # every dataset: rows, files, bytes, range
dal store query "SELECT day, count(*) FROM spans GROUP BY 1 ORDER BY 1"
dal query-spans --from-store --stats               # the same filters as the export files
dal query-spans --from-store --layout typed --model claude --status-code ERROR
```

**`dal export-parquet` reads it and lands in it.** `--from-store` builds the per-source
export (the flattened `spans/source=<name>/week=...` layout plus `sessions/`) from the
store's `spans_raw`, one day at a time, through the same unifier a direct Postgres sync
uses, so a machine attached to a shared store never needs raw sync files. Whatever input
the export used, its output is then copied into the store as `export/<source>/spans` and
`export/<source>/sessions` (replaced whole on each run, since the local export is already
cumulative). `dal store status` lists them.

Then point DAL at one. `dal store presets` prints the URI for each so you do not have to
remember them:

```bash
dal store use 's3://dal/spans?endpoint=127.0.0.1:9100&tls=0'
dal store verify --from-parquet '/path/to/spans/**/*.parquet'
```

**Local and remote are the same thing.** There is no container-only path: a store URI that
works against MinIO on this laptop works against Ceph in a customer's data centre with the
endpoint moved and `tls=1`. That is the whole point of the abstraction, and it is why the
parity harness runs two independent S3 servers rather than one twice.

```bash
uv sync --extra s3
uv run python scripts/verify_spanstore.py --from-parquet '/path/to/spans/**/*.parquet'
```

The harness ingests the same Parquet into every backend, runs the same recipes, and
**fails if any two backends disagree by a single row**. A backend passing on its own
proves nothing; agreement across independent implementations is the evidence.

Two S3 servers are here deliberately. MinIO and SeaweedFS are separate codebases with
different storage models, so agreement between them is a statement about the S3 protocol
rather than about one vendor.

| service | port | what it is |
|---|---|---|
| `minio` | 9100 | S3 implementation #1 |
| `seaweedfs` | 8333 | S3 implementation #2, independent |
| `doltgres` | 5434 | Postgres-flavored Dolt, 1.0 since 2026-08-06 (Forge dataset versioning) |

Ports are overridable (`DAL_MINIO_PORT`, `DAL_SEAWEED_PORT`, `DAL_DOLTGRES_PORT`) because
5433 collides with a stock second Postgres on a normal dev machine.

## Store URIs

```
file:///var/lib/dal/spans                        local directory (tier 0)
s3://bucket/prefix                               AWS
s3://bucket/prefix?endpoint=host:9000&tls=0      MinIO, SeaweedFS, Ceph RGW, on-prem gateway
```

Credentials come from boto3's default chain (environment including `AWS_SESSION_TOKEN`,
profile, SSO, instance role), never the URI. A URI with userinfo, a quote, a statement
character, or an endpoint/bucket outside its legal alphabet is rejected at construction, so
what reaches the engine is data. `tls` defaults **on**; the local presets say `tls=0`
explicitly.

| variable | effect |
|---|---|
| `DAL_SPAN_STORE` | store URI (overrides config) |
| `DAL_SPAN_LAYOUT` | `raw` or `typed` |
| `DAL_ZSTD_LEVEL` | 1–22 |
| `DAL_S3_ENDPOINT` | endpoint when the URI has none (also `AWS_ENDPOINT_URL_S3`, `AWS_ENDPOINT_URL`) |
| `DAL_DUCKDB_HTTPFS_PATH` | vendored `httpfs.duckdb_extension` for air-gapped installs |

## Air-gapped installs: two traps, both measured

`INSTALL httpfs` fetches from `extensions.duckdb.org` and fails with no outbound network.
Vendoring the extension works, and `LOAD` succeeds **with signature enforcement left on**
(no `allow_unsigned_extensions`), but two things will bite:

1. **Version lock.** An extension is built for one DuckDB *patch* version and refuses any
   other. Our pin is a range (`duckdb>=1.4.3,<1.6`), so a routine dependency bump can
   invalidate a vendored file and only an air-gapped install would notice.
2. **Filename lock.** DuckDB derives the entrypoint symbol from the basename, so
   `httpfs-v1.4.3.duckdb_extension` makes it look for `httpfs-v1_duckdb_cpp_init` and
   fail. Keep the canonical name and put the version in the directory:

```
vendor/v1.4.3/httpfs.duckdb_extension        # correct
vendor/httpfs-v1.4.3.duckdb_extension        # silently unloadable
```

Point `DAL_DUCKDB_HTTPFS_PATH` at the file. `capabilities().airgap_ready` reports whether
it *actually loads*, not whether the variable is set, because the first version of this
happily reported "air-gap ok" for a file DuckDB was rejecting on every query.

## Size figures: pin the compression level or the number is noise

Measured on one day of the ENG2-1589 fixture. Same rows, same ZSTD codec, only write
settings varying:

| writer | level | row groups | size |
|---|---:|---:|---:|
| duckdb | 1 | default | 110.17 MB |
| pyarrow | 1 | one | 102.85 MB |
| pyarrow | 1 | 2000-row | **55.47 MB** (how the fixture was written) |
| duckdb | 3 | default | 15.82 MB |
| pyarrow | 3 | one | 14.93 MB |
| pyarrow | 3 | 2000-row | 10.76 MB |
| pyarrow | 9 | one | 6.34 MB |

**A 17x spread with nothing changed but writer settings**, and the two writers agree
within ~6% at any fixed level. pyarrow's default zstd level is 1, which is why our fixture
looked 3x larger than the DuckLake and Iceberg candidates it was compared against: that
comparison was measuring writer defaults, not formats.

### Which level

Whole fixture, 101,655 spans, `threads=1`, four-recipe suite where one recipe scans the
large payload column:

| level | size | write | suite |
|---:|---:|---:|---:|
| 1 | 2,112.6 MB | 15.6 s | 1,877 ms |
| 3 | 443.7 MB | 11.1 s | 1,247 ms |
| **9** | **204.8 MB** | **11.5 s** | **1,072 ms** |
| 15 | 190.7 MB | 352.4 s | 1,060 ms |

`DEFAULT_ZSTD_LEVEL = 9`. It is smaller *and* faster to query than level 3, because
reading 2.2x fewer bytes more than pays for the extra decompression, and it costs 4% more
write time. Level 15 buys 7% more size for 32x the write time and no read gain.

Note what this does to the headline: the fixture the deck quoted at **1,291 MB** is
**204.8 MB** written properly. **6.3x**, from write settings, on the same rows in the same
format. Both candidate figures the deck could not reconcile (DuckLake 429 MB, Iceberg
574 MB) sit in the same family as level 3, so the apparent 3x gap was our own export.

Pass `deterministic=True` to `copy_from_glob` for any size figure that will be quoted:
multi-threaded writes are not byte-reproducible (three runs of identical data gave
450.800 / 451.999 / 450.973 MB), while single-threaded writes are byte-identical and 1.6%
smaller.
