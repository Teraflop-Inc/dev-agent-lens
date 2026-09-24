"""Producer-drift measurement and detection for the span store (ENG2-1591).

The question behind it: what is the blast radius when a producer we do not control
(Phoenix, the OTel emitter, LiteLLM) changes the shape of what it writes?

ENG2-1589's schema research admitted: "Nothing in this design detects producer drift."
Under a typed schema an upstream change does not raise -- it returns NULL and the query
keeps answering. Typing is only a safe trade if drift is loud. This package makes it loud.

Measured on four months of phoenix.spans (122 days, 187 paths): zero jsonb type changes;
every structural change was a key arriving or leaving, and every gap on a typed column had
a dated non-drift cause. The one real producer change -- `llm.invocation_parameters` going
from ~90% unparseable to ~96% parseable JSON in August -- was invisible to structural
inspection because it happened INSIDE a double-encoded string. Hence two levels:

    fingerprint   per-day (path, jsonb type, rows) from Postgres      extract.sql, sweep
    classify      APPEAR / GAP / VANISH / TYPEFLIP / POLYMORPH        classify.py
    detector      contract of path + type + minimum coverage          detector.py
    deep_shape    shape INSIDE string-valued typed columns            deep_shape.py

One loader (`fingerprints.load`) is the only way fingerprints are read. It sums rows across
jsonb types per (day, path). Two ad-hoc scripts that did not sum reported a coverage
collapse on the heaviest days in the corpus that the database does not have -- exactly the
signature the detector is meant to catch, manufactured by the tooling meant to catch it.
"""
