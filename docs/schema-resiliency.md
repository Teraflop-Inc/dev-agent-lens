# Schema resiliency: what a surprise can and cannot reach

The store types a schema so queries are fast. Alex's worry (ENG2-1591, 09-04; ENG2-1602,
09-08) is the trade that implies: *"we're accepting chaos at ingestion time and we're gonna
be trying to be really flexible. Does putting us in a schema hurt us there?"* This page is
the answer in one place. It covers two different risks and names the mechanism that
confines each, because they are not the same problem:

1. **Drift in a producer we already read** (Phoenix, the OTel path, LiteLLM). Handled.
2. **A producer we have not met** (a new coding agent, a new gateway, a new OTel partner).
   Handled by construction; proven by the two runs in ENG2-1610.

Everything below was measured on this corpus, not reasoned about. Numbers are dated.

## The three things that confine every surprise

| Mechanism | What it guarantees | Where |
|---|---|---|
| `spans_raw` keeps `attributes` as verbatim JSON text | nothing is lost at ingest; a path we never typed is still there the day a recipe wants it | `storage/spanstore/base.py` `append_frame` |
| `attributes_rest` in the typed layout | everything the typed select did not lift out is carried along, whole, on every typed row | `storage/layouts/typed.py` |
| `dal drift` fingerprints the producer per day | a change is a fact you read the next morning, not a NULL you notice a month later | `dev_agent_lens/drift/` |

The typed columns are a **view over raw that we rebuild**, not a second copy of the truth.
`dal store layout typed && dal store verify --from-parquet '<spans_raw glob>'` is the whole
recovery from any typed-layout mistake, and it took 1.7 s on 36,052 spans (2026-09-11).

## Risk 1: drift in producers we already read

Measured 2026-09-04 to 09-08 (ENG2-1591 Answer 6): **122 days, 187 attribute paths, zero
jsonb type changes.** Every structural change was a key arriving or leaving. The one real
event was invisible to structure: `llm.invocation_parameters` went from ~90% unparseable to
~96% parseable JSON in August, inside a double-encoded string, swapping `temperature` for
`thinking` in its key set. That is why the detector has two levels (structure, then the
shape inside string-valued typed columns).

Blast radius per event kind, and what the operator does:

| Event | Raw | Typed | Operator |
|---|---|---|---|
| new path | untouched | in `attributes_rest` | add a column when a recipe wants it, rebuild |
| path vanishes or gaps | NULL on read | column NULL for the window | `dal drift classify` says whether it is a quiet day or drift; check the producer before believing loss |
| type flips | untouched, it is text | `TRY_CAST` returns NULL, **no error** | this is the silent case; the contract's coverage floor is what sees it. Adjust the cast, rebuild |
| values go empty with the key present | untouched | column present, empty | a proxy or producer regression (this is the ENG2-1510 shape); `classify`'s coverage classes surface it before the contract fails |
| change inside a double-encoded string | untouched | only the columns that parse that string | `dal drift` deep-shape reports it; the typed build must hand the parser NULL for non-objects (fixed 2026-09-11, two spans in 36,052 were enough to fail a build) |

Per producer, what we actually depend on:

- **Phoenix (Postgres)**: table shape (`span_id`, `trace_rowid`, `attributes` jsonb, token
  counts) plus the `attributes` paths the typed select lifts. The oracle
  (`scripts/migration_oracle.py`) runs fifteen cookbook questions through Phoenix and the
  store daily; a producer change that alters an answer fails it the next morning.
- **LiteLLM → OTel → Phoenix**: the attribute paths are LiteLLM's (`llm.*`,
  `metadata.usage_object.*`, `metadata.user_api_key_end_user_id`). A LiteLLM upgrade is the
  most likely drift source. 0003 keeps LiteLLM unchanged indefinitely and names OTel as the
  boundary; the drift contract is pinned from a clean window so an upgrade that renames a
  path fails `dal drift check` rather than going NULL quietly.
- **Claude Code hooks (F3 capture)**: writes through the same OTel path, different span
  names and a different `attributes` vocabulary. Same mechanisms apply; it is also the
  "real producer" in the ENG2-1610 proof because it is what we run day to day.
- **Arize (OTel partner) and Portkey (gateway)**: not in the pipeline today. They would be
  Risk 2 until their first day of data, then Risk 1.

The open weakness, stated in `detector.py`: the coverage floor is the minimum over the
baseline window, so a collapse that begins inside the baseline is learned as normal. Build
the baseline from a window `classify` has already called clean. Enforcing that in code is
the next drift change; today it is documented.

## Risk 2: a producer we have not met

Nothing about the typed schema has to be known in advance for a new integration to land,
and that is by construction rather than by care:

1. Its spans land in `spans_raw` **as-is**, `attributes` verbatim, stamped with a `source`
   that names it. Nothing rejects an unknown shape; `append_frame` requires only `span_id`
   and `start_time`.
2. It gets **its own layout** if its vocabulary does not fit `_TYPED_SELECT`. A layout is a
   SELECT over raw plus a name (`storage/layouts/`), registered in `LAYOUTS`. The existing
   typed layout is not touched, so the old producer's queries cannot regress because a new
   producer arrived.
3. Where its vocabulary *does* overlap (tokens, model, timing are near-universal), the
   columns it shares are the ones cookbook recipes already read, and the oracle's
   per-source scoping (`source` is a typed column since 2026-09-11) keeps its answers from
   blending into another producer's.

What that costs an open-source adopter, which is Alex's real concern: **a breaking change is
a change to a typed column's name, type, or meaning.** Adding a column is free
(`union_by_name`), adding a layout is free, adding a producer is free. Renaming or retyping
a column is a rebuild for everyone who reads it, so it is versioned: the layout name is the
contract, and a change that breaks readers ships as a new layout name next to the old one,
not as an edit to `typed`.

### The proof, run 2026-09-11 (ENG2-1610)

Two producers the schema had never seen, into a fresh local store, one typed build:

| Producer | Rows | Landed | Typed columns | In `attributes_rest` |
|---|---:|---:|---|---|
| Claude Code hooks (`claude-code-surfaces`, real) | 3,941 | 3,941 | timing, status, names; **identity was NULL on every row** | `claude.*` whole, session id included |
| `scripts/fake_producer.py` (arbitrary: nested objects, arrays, a field whose JSON type changes per row, a double-encoded string, a 200 KB attribute, a unicode key, rows with no events, a few reusing `llm.model_name`) | 2,000 | 2,000 | the 154 overlapping rows typed their model and tokens; nothing else, correctly | `widget.*` whole, 200 KB row intact |

Per-source scoping held: neither producer's rows appear in the other's recipes.

What the real producer found: **identity is producer-specific.** The typed layout read a
session only from LiteLLM's `metadata.user_api_key_end_user_id`; the hooks producer keeps
it at `claude.session_id`. Every hooks row landed, every one carried its session in the
overflow, and the typed `session_id` column was NULL on all 3,941. Fixed the same day: the
identity derivation now tries the known producer paths in order (LiteLLM, then
`claude.session_id`, then OpenInference `session.id` / `user.id`), additively, with a test.
After the fix: 3,941 of 3,941 with a session, 94 sessions, and the oracle's fifteen
recipes agree with Phoenix on the hooks project.

What the fake producer found: nothing broke, which is the point of the design, and it is
now a repeatable stress test rather than an argument.

What neither could test: `dal drift` fingerprints the **producer** (Postgres), so a producer
that lands only in the store has no drift coverage today. A store-side sweep over
`spans_raw` per `source` is the follow-up, and until it exists the oracle's per-source
count and `sample_content` recipes are the drift signal for store-only producers.

## What an operator does on a surprise, in order

1. `dal drift check <fingerprints> --contract <contract>` names the path and the day.
2. `dal store query --layout raw "SELECT ... FROM spans WHERE day = ..."` shows the raw
   rows; nothing was lost, so the question is only what the typed column should now read.
3. Fix the cast or add the column in `typed.py`, `dal store verify --from-parquet` rebuilds
   typed from raw, and the oracle confirms the answers still match Phoenix.
4. If the producer changed rather than us, pin a new contract from the first clean window
   after the change and record the date here.

Dated facts on this page: 2026-09-11.
