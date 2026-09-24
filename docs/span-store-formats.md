# Span store: the formats, and how to run them

One page for the question "how do we manage all of these formats?". It says what each
shape of trace data is, who writes it, who reads it, whether it is the truth or something
derived, and the one command that rebuilds it. If a shape is not on this page, DAL does
not write it.

The store itself is a URI (`dal store show`). Everything below is plain hive-partitioned
Parquet under that URI, so any Parquet reader and any S3 tool works on it; there is no
DAL-specific container format. See [docker/spanstore/README.md](../docker/spanstore/README.md)
for the local backends and the parity harness.

## The shapes at a glance

| Dataset (under the store URI) | What a row is | Written by | Read by | Truth or derived |
|---|---|---|---|---|
| `spans_raw/day=YYYY-MM-DD/` | one span as the producer emitted it: ids, name, kind, times, status, token counts, `attributes` and `events` as verbatim JSON text, plus `source` (which configured source landed it) and `day` | `dal sync` appends one file per batch, never clears; `dal store verify --from-parquet <glob>` with the raw layout replaces it whole | `dal store query` (raw layout), `dal query-spans --from-store`, `dal export-parquet --from-store`, the typed build | **truth** |
| `spans_typed/day=…/` + `blobs_typed/` | ~30 typed columns (model, tokens, thinking settings, identity, tool name, `source`, …), an `attributes_rest` JSON overflow for the tail, and the large payloads moved to content-addressed blobs | the typed layout build (`dal store layout typed`, then `dal store verify --from-parquet <glob>`); replaced whole | `dal store query --layout typed`, `dal query-spans --from-store --layout typed` | derived from `spans_raw` |
| `export/<source>/spans/week=…/` + `export/<source>/sessions/` | the per-source analysis shape: one flattened row per span with `session_id`, `llm_model_name`, token counts and `raw_attributes_json`; one row per session with totals | `dal export-parquet` (any input), replaced whole after the local files are written | `dal store query` over the files; `dal store status` | derived |

Outside the store, on the machine that ran `dal sync`, the older local files still exist
and are still written: `~/.dal/data/raw/<source>/sync_*.jsonl` (unified rows),
`unified/<source>_sessions.jsonl`, `parquet/spans/source=<name>/…` (the local export that
`dal query-spans --source <name>` reads), and the Oxen repo. They predate the store. Whether
they become a view over it is an open design choice; today both are written.

## Which one answers which question

| Question | Use |
|---|---|
| Did the traces land? How many, which days, how big? | `dal store status` |
| Anything ad hoc, in SQL | `dal store query "…"` (`--layout typed` for typed columns) |
| Spans by session, status, model or name; quick stats | `dal query-spans --from-store` |
| The legacy per-source files for notebooks, Oxen, or `dal query-spans --source` | `dal export-parquet --source <name> --from-store` |
| A typed table for an engine that cannot parse JSON per row | the typed layout |

## The order things happen

```
producer (Phoenix Postgres / SQLite / REST)
   │  dal sync                           append, one file per batch, stamped with source
   ▼
spans_raw  ──── truth ─────────────────────────────────────────────────────────────┐
   │  dal store verify --from-parquet    typed layout, replaced whole               │
   ▼                                                                                │
spans_typed + blobs_typed                                                           │
   │                                                                                │
   │  dal export-parquet --from-store    one day at a time, same unifier as a sync  │
   ▼                                                                                │
local export files  ──► landed as export/<source>/…  (replaced whole)  ◄────────────┘
```

Reads never copy anything out: `dal store query` and `dal query-spans --from-store` read the
Parquet where it lives.

## Rules that keep this manageable

1. **`spans_raw` is the only truth in the store.** Every other dataset can be deleted and
   rebuilt from it with one command (table below). If two of them disagree, the derived one
   is stale; rebuild it.
2. **Derived datasets are replaced whole.** Nothing in DAL appends to `spans_typed`,
   `blobs_typed` or `export/…`. Only `spans_raw` is appended to, and only by `dal sync`.
3. **Writes pin their settings.** Compression is zstd at the level `dal store level` shows
   (default 9). A size figure you intend to quote comes from a deterministic (single
   writer thread) build; `dal store verify --from-parquet` does that.
4. **Readers use `hive_partitioning=true, union_by_name=true`.** A column that appears in
   later files is NULL in earlier ones, and the reverse. So adding a column costs nothing;
   renaming one is a rebuild; changing a column's type makes the reader fail on the
   conflict, which is the signal to rebuild (or cast at write time).
5. **`source` tells sources apart.** One store can hold several. Rows landed before the
   stamp existed have NULL there; `dal export-parquet --from-store` includes them and says
   how many.
6. **Credentials never go in the URI.** They come from the environment (boto3's chain).
   A store URI is safe to paste into a ticket.

## Rebuild, verify, move, retire

| Need | Command |
|---|---|
| Rebuild `spans_raw` from the producer | `dal sync --source <name> --full` (with a store chosen) |
| Rebuild `spans_raw` from a Parquet dump | `dal store layout raw && dal store verify --from-parquet '<glob>'` |
| Rebuild the typed layout | `dal store layout typed && dal store verify --from-parquet '<spans_raw glob>'` |
| Rebuild a source's export | `dal export-parquet --source <name> --from-store` |
| Check the store is reachable and writable | `dal store verify` |
| Check counts against the producer | `dal query-spans --from-store --stats` next to a direct count on the producer for the same time bounds; they must match exactly |
| Check *answers* against the producer | `uv run python scripts/migration_oracle.py --project <phoenix project> --source <name> [--layout raw\|typed]`. Fourteen cookbook-shaped questions run through both schemas; every one must agree, and a recipe the store cannot express is a finding, not a skipped row. This is the cross-schema oracle 0003 §2 and §9 ask for. Rehearsed 2026-09-11 on the dead `dev-agent-lens` project: 36,052 spans, 14/14 on both layouts, store 70x (raw) and 134x (typed) faster than Postgres |
| Move a store | copy the objects with any S3 tool (`aws s3 sync`, `rclone`, `mc mirror`, or `cp -r` for a directory), `dal store use <new uri>`, then `dal store status` on both and compare rows per dataset |
| Retire a derived dataset | delete its prefix with the same tool; nothing else references it. There is no `dal store drop` yet |
| Retention on raw data | delete `spans_raw/day=…` prefixes older than the cutoff; the day partition makes that a prefix delete. Rebuild typed and export afterwards so they do not outlive their source rows |

If the store is lost, the producer is still upstream: re-sync. If the producer is gone, the
store is the copy, so back it up like any bucket.

## Ongoing accuracy check (the oracle on a clock)

0003 §8 keeps Phoenix running as the oracle during migration. A check that only runs when a
human remembers is not a check (see agent-forge's `capture-health.yml` for the nine-day
outage that rule comes from), so the oracle has a clock:

```
scripts/oracle_daily.sh                 # every phoenix-postgres source, cut at the previous hour,
                                        # verdict under ~/.dal/oracle/, Slack on any mismatch
scripts/launchd/com.teraflop.dal-oracle.plist   # 06:23 local; install steps in the file header
```

It runs on the host that holds the store because the store is local MinIO until the net-new
deployment lands somewhere a CI runner can reach; the script reads everything from env
(`~/.dal/oracle.env`, mode 600: the Phoenix DSN, the store credentials, an optional
`SLACK_WEBHOOK_URL`) so it moves into a workflow unchanged when that day comes.

`--until` matters: a live project keeps growing while the import runs, so both sides are cut
at the same instant or every count mismatches for a true reason. A mismatch on a source
whose import is still in flight is expected and is exactly what the check is for; it turns
green when the import catches up and stays green until something drifts.

`--since` is the other cut. A rolling deployment (`deploy/compose.yml`, `SYNC_DAYS`) holds a
window, not history, so the daily check compares that window on both sides; the producer's
older spans are not a disagreement about anything the store claims to hold.

## When the producer's schema changes

`attributes` is kept as verbatim JSON text in `spans_raw`, so a new or renamed attribute
path is never lost at ingest; it just is not typed until a recipe needs it. The typed layout
keeps everything it did not type in `attributes_rest` for the same reason.

The drift tools watch the producer so a change is a fact you read, not a surprise in a
recipe:

```bash
dal drift sweep --start-date … --end-date …   # one fingerprint per day per attribute path
dal drift classify                            # APPEAR / GAP / VANISH / TYPEFLIP / POLYMORPH / NULLED
dal drift contract                            # pin the shape from a clean window
dal drift check                               # exit 1 on a contract violation
```

What to do per kind of event:

| Event | Raw layout | Typed layout | Action |
|---|---|---|---|
| APPEAR (new path) | nothing to do | in `attributes_rest` until a column is added | add the column when a recipe wants it, rebuild typed |
| VANISH / GAP | readers get NULL | column goes NULL | check the producer before assuming data loss; a gap in one day is usually a quiet day |
| TYPEFLIP / POLYMORPH | unaffected (text) | `TRY_CAST` yields NULL for the new type | fix or accept at the producer, adjust the cast, rebuild typed |
| NULLED (path present, values empty) | unaffected | column present, empty | a producer or proxy regression; the coverage classes in `classify` show it before the contract fails |
| a change inside a double-encoded string (invocation parameters, tool inputs) | unaffected | unaffected unless a column parses that string | only the recipes that parse it care; `drift` reports these as deep-shape events |

Take the contract from a window you know is clean. Today that is documented, not enforced.

## Things that bite

- `holds no spans_raw data yet`: no store was chosen (`dal store use …`) or nothing has
  been synced since it was.
- S3-protocol stores need `uv sync --extra s3` (boto3) and DuckDB's httpfs extension;
  air-gapped installs vendor the extension and point `DAL_DUCKDB_HTTPFS_PATH` at it.
- The local compose files are for development only: default credentials, loopback bound.
- `DAL_CONFIG_PATH` may point at a file or a directory; both are accepted everywhere.
- A session called `id` in an older export is a known extractor bug, fixed; re-export.

## Open

- Two writers on one store at the same time is untested. DuckDB is not the arbiter of that;
  a coordination layer would be.
- The local per-source files as a view over the store, instead of a second copy.
- `dal store drop <dataset>` and a retention command, so the S3 tool is not the only way.
