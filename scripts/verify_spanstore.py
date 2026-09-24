#!/usr/bin/env python3
"""Prove every span-store backend is interchangeable, by measurement.

ENG2-1591: the object-storage backend has to be flexible enough to deploy into a
regulated, on-prem environment. A claim like that is worth nothing until the backends are
run side by side against identical data and shown to agree.

What this checks, per backend:
  1. probe     reachable, readable, writable
  2. ingest    the same Parquet loads, hive-partitioned + zstd
  3. parity    the five pinned recipes return BYTE-IDENTICAL results across backends
  4. traces    trace reconstruction is structurally intact (parent/child edges preserved)
  5. size      bytes on disk, on one common basis

Parity is the point. Any single backend passing proves nothing; two backends disagreeing
by one row is a portability bug, and this is where we want to find it.

    docker compose -f docker/spanstore/all.yml up -d
    uv sync --extra s3
    uv run python scripts/verify_spanstore.py --from-parquet '/path/to/spans/**/*.parquet'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dev_agent_lens.storage.spanstore import open_store  # noqa: E402

log = logging.getLogger("verify")

# Backends verified by default. Two independent S3 servers on purpose: agreement between
# MinIO and SeaweedFS is evidence about the protocol, not about one vendor.
_SCRATCH = tempfile.mkdtemp(prefix="dal-verify-")  # not a fixed /tmp path: spans hold PII
DEFAULT_TARGETS = {
    "local": f"file://{_SCRATCH}/local",
    "minio": "s3://dal-verify/minio?endpoint=127.0.0.1:9100&tls=0",
    "seaweedfs": "s3://dal-verify/seaweed?endpoint=127.0.0.1:8333&tls=0",
}

RECIPES = {
    "R_roster": "SELECT day, count(*) FROM spans GROUP BY 1 ORDER BY 1",
    "R_status": "SELECT status_code, count(*) FROM spans GROUP BY 1 ORDER BY 1",
    "R_names": "SELECT name, count(*) c FROM spans GROUP BY 1 ORDER BY c DESC, name LIMIT 25",
    "R_tokens": (
        "SELECT day, sum(COALESCE(llm_token_count_prompt,0)) p, "
        "sum(COALESCE(llm_token_count_completion,0)) c "
        "FROM spans GROUP BY 1 ORDER BY 1"
    ),
    "R_errors": (
        "SELECT trace_rowid, span_id FROM spans WHERE status_code='ERROR' "
        "ORDER BY trace_rowid, span_id LIMIT 50"
    ),
}

TRACE_SQL = """
SELECT trace_rowid, span_id, COALESCE(parent_id,'') AS parent_id, name
FROM spans
WHERE trace_rowid IN (
    SELECT trace_rowid FROM spans GROUP BY 1 ORDER BY count(*) DESC, trace_rowid LIMIT 25
)
ORDER BY trace_rowid, span_id
"""


def digest(rows) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(("\x1f".join("" if v is None else str(v) for v in r) + "\x1e").encode())
    return h.hexdigest()[:16]


@dataclass
class BackendReport:
    name: str
    uri: str
    ok: bool = False
    skipped: str = ""
    probe_ms: float = 0.0
    probe_detail: str = ""
    capabilities: dict = field(default_factory=dict)
    ingest_rows: int = 0
    ingest_partitions: int = 0
    ingest_ms: float = 0.0
    size_bytes: int = 0
    recipes: dict = field(default_factory=dict)  # id -> {digest, rows, ms}
    trace_digest: str = ""
    trace_rows: int = 0
    trace_edges_intact: bool = False


def connect(store):
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")  # unpinned tz invents a phantom day bucket
    store.attach_duckdb(con)
    return con


def run_backend(name: str, uri: str, source_glob: str, runs: int) -> BackendReport:
    rep = BackendReport(name=name, uri=uri)
    store = open_store(uri)
    rep.capabilities = asdict(store.capabilities())

    h = store.probe()
    rep.probe_ms, rep.probe_detail = h.elapsed_ms, h.detail
    if not h.ok:
        try:
            store.ensure()
            h = store.probe()
            rep.probe_detail = h.detail
        except Exception as e:  # noqa: BLE001
            rep.skipped = f"unreachable: {type(e).__name__}: {e}"
            log.warning("[verify] %s SKIPPED %s", name, rep.skipped)
            return rep
    if not h.ok:
        rep.skipped = f"probe failed: {h.detail}"
        log.warning("[verify] %s SKIPPED %s", name, rep.skipped)
        return rep
    store.ensure()

    con = connect(store)
    w = store.copy_from_glob(con, source_glob, dataset="spans", partition_by="day")
    rep.ingest_rows, rep.ingest_partitions = w.rows, w.partitions
    rep.ingest_ms, rep.size_bytes = w.elapsed_ms, w.bytes_written

    con.execute(f"""CREATE VIEW spans AS SELECT * FROM read_parquet(
        '{store.read_glob("spans")}', hive_partitioning=true, union_by_name=true)""")

    for rid, sql in RECIPES.items():
        times = []
        rows = None
        for _ in range(runs):
            t0 = time.perf_counter()
            rows = con.execute(sql).fetchall()
            times.append(round((time.perf_counter() - t0) * 1000, 2))
        rep.recipes[rid] = {"digest": digest(rows), "rows": len(rows), "ms": min(times)}
        log.info("[verify] %-10s %-10s rows=%-6d %.1fms", name, rid, len(rows), min(times))

    trows = con.execute(TRACE_SQL).fetchall()
    rep.trace_digest, rep.trace_rows = digest(trows), len(trows)
    # every non-root parent_id must resolve to a span_id in the same trace
    ids_by_trace: dict = {}
    for tr, sid, _p, _n in trows:
        ids_by_trace.setdefault(tr, set()).add(sid)
    dangling = [(tr, p) for tr, _s, p, _n in trows if p and p not in ids_by_trace.get(tr, set())]
    rep.trace_edges_intact = not dangling
    if dangling:
        log.warning(
            "[verify] %s %d dangling parent edges (first: %s)", name, len(dangling), dangling[0]
        )
    con.close()
    rep.ok = True
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-parquet", dest="source", required=True, help="Parquet glob to ingest")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", help="comma-separated subset of backends")
    ap.add_argument("--json", dest="json_out", help="write the full report here")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    targets = dict(DEFAULT_TARGETS)
    if a.only:
        keep = {s.strip() for s in a.only.split(",")}
        targets = {k: v for k, v in targets.items() if k in keep}
    if os.getenv("DAL_VERIFY_EXTRA_STORES"):  # name=uri,name=uri
        for pair in os.getenv("DAL_VERIFY_EXTRA_STORES", "").split(","):
            n, _, u = pair.partition("=")
            if n and u:
                targets[n.strip()] = u.strip()

    reports = [run_backend(n, u, a.source, a.runs) for n, u in targets.items()]
    live = [r for r in reports if r.ok]

    print("\n" + "=" * 78)
    print(f"{'backend':<12}{'rows':>9}{'parts':>7}{'size MB':>10}{'ingest s':>10}  capability")
    for r in reports:
        if not r.ok:
            print(f"{r.name:<12}{'SKIPPED':>9}   {r.skipped[:44]}")
            continue
        cap = r.capabilities
        tag = (
            "air-gap ok"
            if cap.get("airgap_ready")
            else f"needs {cap.get('needs_duckdb_extension')}"
        )
        print(
            f"{r.name:<12}{r.ingest_rows:>9,}{r.ingest_partitions:>7}"
            f"{r.size_bytes / 1e6:>10.1f}{r.ingest_ms / 1000:>10.1f}  {tag}"
        )

    print("\n--- cross-backend parity (the actual test) ---")
    failures = 0
    if len(live) < 2:
        # a single backend agreeing with itself is not parity; this used to print PASS
        print(f"  only {len(live)} live backend(s): parity needs at least 2")
        failures += 1
    for rid in RECIPES:
        digs = {r.name: r.recipes[rid]["digest"] for r in live}
        agree = len(set(digs.values())) <= 1
        failures += 0 if agree else 1
        times = "  ".join(f"{r.name}={r.recipes[rid]['ms']:.1f}ms" for r in live)
        print(f"  {rid:<10} {'IDENTICAL' if agree else 'MISMATCH ' + json.dumps(digs)}   {times}")
    tdigs = {r.name: r.trace_digest for r in live}
    tagree = len(set(tdigs.values())) <= 1
    failures += 0 if tagree else 1
    print(
        f"  {'traces':<10} {'IDENTICAL' if tagree else 'MISMATCH ' + json.dumps(tdigs)}"
        f"   rows={live[0].trace_rows if live else 0}"
        f"  edges_intact={all(r.trace_edges_intact for r in live)}"
    )
    if not all(r.trace_edges_intact for r in live):
        failures += 1

    if a.json_out:
        with open(a.json_out, "w") as f:
            json.dump([asdict(r) for r in reports], f, indent=1)
        print(f"\n[verify] report -> {a.json_out}")

    print(
        "\n"
        + ("PASS: every backend agrees" if failures == 0 else f"FAIL: {failures} disagreement(s)")
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
