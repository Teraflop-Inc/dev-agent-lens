"""
Phoenix Postgres client.

Reads span data directly from a Phoenix-on-Postgres backend (e.g. Supabase),
mirroring the surface of `PhoenixSQLiteClient` so `dal sync` and downstream
tooling work without code changes when switching from SQLite to Postgres.

Use when Phoenix is configured with `PHOENIX_SQL_DATABASE_URL=postgres://...`
(see ENG2-817). Tables live in a configurable schema (default `phoenix`).

Example:
    client = PhoenixPostgresClient(
        connection_url="postgres://...@aws-1-us-east-1.pooler.supabase.com:5432/postgres",
        project="dev-agent-lens",
        schema="phoenix",
    )
    df = client.get_spans_dataframe(start_time=..., end_time=..., limit=1000)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class PhoenixPostgresError(Exception):
    """Base exception for Phoenix Postgres client errors."""


class PhoenixPostgresConnectionError(PhoenixPostgresError):
    """Raised when the client can't reach the Postgres server."""


class PhoenixPostgresQueryError(PhoenixPostgresError):
    """Raised when a query fails to execute."""


class PhoenixPostgresClient:
    """Direct Postgres reader for a Phoenix span store.

    Mirrors the surface of `PhoenixSQLiteClient` so the rest of DAL doesn't
    care which backend Phoenix is on.

    Notes:
        - Connections use `psycopg` (v3). Add `psycopg[binary]` to deps.
        - Phoenix's tables live in a configurable schema (default `phoenix`).
          The schema is set via `PHOENIX_SQL_DATABASE_SCHEMA` on the Phoenix
          container; this client must match.
        - `attributes` and `events` are JSONB in Postgres — returned as dicts,
          unlike the JSON-string of the SQLite path. Callers should treat
          both as dicts for forward-compat.
        - Use Supabase's Session pooler (port 5432 on `*.pooler.supabase.com`)
          for IPv4 + prepared-statement compatibility.
    """

    def __init__(
        self,
        connection_url: str,
        project: str = "dev-agent-lens",
        schema: str = "phoenix",
        timeout: int = 30,
    ) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError as e:
            raise PhoenixPostgresError(
                "psycopg (v3) is required. Install with: uv add 'psycopg[binary]'"
            ) from e

        self.connection_url = connection_url
        self.project = project
        self.schema = schema
        self.timeout = timeout
        self._conn: Any = None

    def _get_connection(self) -> Any:
        """Open and cache a connection. Reconnects if the cached one is dead."""
        import psycopg

        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(
                    self.connection_url,
                    connect_timeout=self.timeout,
                    autocommit=True,
                )
                with self._conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {self.schema}, public")
            except Exception as e:
                raise PhoenixPostgresConnectionError(
                    f"Failed to connect to Postgres: {e}"
                ) from e
        return self._conn

    def _execute_query(
        self, query: str, params: tuple[Any, ...] | None = None
    ) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        try:
            conn = self._get_connection()
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params or ())
                return list(cur.fetchall())
        except PhoenixPostgresConnectionError:
            raise
        except Exception as e:
            raise PhoenixPostgresQueryError(f"Query failed: {e}") from e

    def get_spans_dataframe(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> pd.DataFrame:
        """Fetch spans for the configured project as a DataFrame.

        Returns the same columns as `PhoenixSQLiteClient.get_spans_dataframe` so
        downstream code is backend-agnostic.
        """
        query = """
            SELECT
                s.span_id AS "context.span_id",
                t.trace_id AS "context.trace_id",
                s.parent_id,
                s.name,
                s.span_kind,
                s.start_time,
                s.end_time,
                s.status_code,
                s.status_message,
                s.attributes,
                s.events,
                s.cumulative_error_count,
                s.cumulative_llm_token_count_prompt,
                s.cumulative_llm_token_count_completion,
                s.llm_token_count_prompt,
                s.llm_token_count_completion
            FROM spans s
            JOIN traces t ON s.trace_rowid = t.id
            JOIN projects p ON t.project_rowid = p.id
            WHERE p.name = %s
        """
        params: list[Any] = [self.project]

        if start_time is not None:
            query += " AND s.start_time >= %s"
            params.append(start_time)
        if end_time is not None:
            query += " AND s.start_time < %s"
            params.append(end_time)

        query += " ORDER BY s.start_time ASC"

        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        if offset:
            query += " OFFSET %s"
            params.append(offset)

        rows = self._execute_query(query, tuple(params))
        df = pd.DataFrame(rows)

        # Match SQLite client output: serialize JSONB to strings so callers
        # that parse with json.loads keep working. Future cleanup: have all
        # backends return dicts and update callers.
        for col in ("attributes", "events"):
            if col in df.columns:
                df[col] = df[col].apply(
                    lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v
                )

        return df

    def get_total_span_count(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int:
        query = """
            SELECT COUNT(*) AS count
            FROM spans s
            JOIN traces t ON s.trace_rowid = t.id
            JOIN projects p ON t.project_rowid = p.id
            WHERE p.name = %s
        """
        params: list[Any] = [self.project]

        if start_time is not None:
            query += " AND s.start_time >= %s"
            params.append(start_time)
        if end_time is not None:
            query += " AND s.start_time < %s"
            params.append(end_time)

        rows = self._execute_query(query, tuple(params))
        return int(rows[0]["count"]) if rows else 0

    def get_time_range(self) -> tuple[datetime, datetime]:
        """Return (min, max) of all span start_times for the project."""
        query = """
            SELECT MIN(s.start_time) AS min_time, MAX(s.start_time) AS max_time
            FROM spans s
            JOIN traces t ON s.trace_rowid = t.id
            JOIN projects p ON t.project_rowid = p.id
            WHERE p.name = %s
        """
        rows = self._execute_query(query, (self.project,))
        if not rows or rows[0]["min_time"] is None:
            raise ValueError(
                f"No spans found in project '{self.project}' under schema '{self.schema}'"
            )
        return rows[0]["min_time"], rows[0]["max_time"]

    def get_span_annotations_dataframe(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pd.DataFrame:
        """Fetch span annotations for the project."""
        query = """
            SELECT
                sa.span_rowid,
                s.span_id,
                sa.name,
                sa.label,
                sa.score,
                sa.explanation,
                sa.metadata,
                sa.annotator_kind,
                sa.created_at,
                sa.updated_at
            FROM span_annotations sa
            JOIN spans s ON sa.span_rowid = s.id
            JOIN traces t ON s.trace_rowid = t.id
            JOIN projects p ON t.project_rowid = p.id
            WHERE p.name = %s
        """
        params: list[Any] = [self.project]

        if start_time is not None:
            query += " AND sa.created_at >= %s"
            params.append(start_time)
        if end_time is not None:
            query += " AND sa.created_at < %s"
            params.append(end_time)

        rows = self._execute_query(query, tuple(params))
        return pd.DataFrame(rows)

    def test_connection(self) -> bool:
        """Confirm we can reach Postgres and the project exists."""
        try:
            rows = self._execute_query(
                "SELECT id FROM projects WHERE name = %s LIMIT 1", (self.project,)
            )
            return len(rows) > 0
        except PhoenixPostgresError:
            return False

    def close(self) -> None:
        """Close the cached connection, if any."""
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def __enter__(self) -> PhoenixPostgresClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __repr__(self) -> str:
        # Mask password in the URL for safe logging
        safe_url = self.connection_url
        if "@" in safe_url and "://" in safe_url:
            scheme, rest = safe_url.split("://", 1)
            if "@" in rest:
                creds, host = rest.split("@", 1)
                if ":" in creds:
                    user, _ = creds.split(":", 1)
                    safe_url = f"{scheme}://{user}:***@{host}"
        return (
            f"PhoenixPostgresClient(url={safe_url!r}, "
            f"project={self.project!r}, schema={self.schema!r})"
        )
