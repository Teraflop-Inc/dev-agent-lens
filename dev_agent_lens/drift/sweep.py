"""Day-by-day structural fingerprint of phoenix.spans.attributes, straight from Postgres.

The DSN comes from PHOENIX_SQL_DATABASE_URL or a configured `phoenix-postgres` source and
is never printed. The schema comes from PHOENIX_SQL_DATABASE_SCHEMA or the source's
`schema` field (default `phoenix`), matching PhoenixPostgresClient. Output is one JSON line
per day; the sweep is resumable and skips days
already present, so a timeout costs one day, not the run.

cap is per span-family per day. 5,000 was verified to return the identical path set to an
exhaustive run on two probe days, so an absent path is absence, not undersampling.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)
SQL_TEMPLATE = (Path(__file__).parent / "extract.sql").read_text()
DEFAULT_CAP = 5000


class NoDsn(RuntimeError):
    """No Postgres DSN is configured. A library exception; the CLI turns it into a message."""


def resolve_dsn() -> tuple[str, str]:
    """(dsn, schema). Never log either return value."""
    env = os.getenv("PHOENIX_SQL_DATABASE_URL")
    schema = os.getenv("PHOENIX_SQL_DATABASE_SCHEMA") or "phoenix"
    if env:
        return env, schema
    from dev_agent_lens.core.sources import SourceManager, SourceType

    for s in SourceManager().list_sources():
        if s.source_type == SourceType.PHOENIX_POSTGRES and s.connection_url:
            return s.connection_url, (getattr(s, "schema", None) or schema)
    raise NoDsn("no Postgres DSN: set PHOENIX_SQL_DATABASE_URL or add a phoenix-postgres source")


def sweep(
    out: Path, lo: dt.date, hi: dt.date, *, cap: int = DEFAULT_CAP, statement_timeout: str = "1500s"
) -> int:
    import psycopg
    from psycopg import sql as _sql

    done = set()
    if out.exists():
        for line in out.open():
            if not line.strip():
                continue
            try:
                done.add(json.loads(line)["day"])
            except json.JSONDecodeError:
                log.warning(
                    "[drift:sweep] skipping a malformed line in %s (interrupted write?)", out
                )
        log.info("[drift:sweep] resuming, %d days already in %s", len(done), out)
    dsn, schema = resolve_dsn()
    query = _sql.SQL(SQL_TEMPLATE).format(schema=_sql.Identifier(schema))
    written = 0
    # autocommit: a SET inside an implicit transaction is discarded by the first rollback(),
    # so "a timeout costs one day, not the run" held exactly once. It also keeps a multi-hour
    # read-only sweep from pinning xmin on the production database. TIME ZONE pins the day
    # boundaries extract.sql compares timestamptz against.
    try:
        conn = psycopg.connect(dsn, connect_timeout=30, autocommit=True)
    except psycopg.ProgrammingError:  # libpq echoes the offending DSN token
        raise NoDsn("invalid Postgres DSN (value withheld)") from None
    with conn as c, c.cursor() as cur:
        cur.execute(f"SET statement_timeout='{statement_timeout}'")
        cur.execute("SET TIME ZONE 'UTC'")
        with out.open("a") as f:
            d = lo
            while d < hi:
                ds = d.isoformat()
                if ds not in done:
                    t0 = time.time()
                    try:
                        cur.execute(
                            query,
                            {"lo": ds, "hi": (d + dt.timedelta(days=1)).isoformat(), "cap": cap},
                        )
                        paths = [
                            {"path": p, "jtype": j, "n": n, "rows": r}
                            for p, j, n, r in cur.fetchall()
                        ]
                        f.write(json.dumps({"day": ds, "paths": paths}) + "\n")
                        f.flush()
                        written += 1
                        log.info(
                            "[drift:sweep] %s paths=%d in %.1fs", ds, len(paths), time.time() - t0
                        )
                    except Exception as e:  # noqa: BLE001 - one bad day must not end the run
                        log.error(
                            "[drift:sweep] %s FAILED %s: %s", ds, type(e).__name__, str(e)[:120]
                        )
                d += dt.timedelta(days=1)
    return written
