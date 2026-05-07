"""
Tests for PhoenixPostgresClient.

These tests mock psycopg connections so they don't need a live Postgres.
The goal is to lock in:
- The query shape (joins, parameters, ordering)
- JSONB → string serialization for backwards-compat with SQLite client
- Repr password masking
- Test_connection / close lifecycle
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dev_agent_lens.clients.phoenix_postgres import (
    PhoenixPostgresClient,
    PhoenixPostgresConnectionError,
    PhoenixPostgresError,
    PhoenixPostgresQueryError,
)


@pytest.fixture
def mock_psycopg(monkeypatch):
    """Patch psycopg.connect so the client thinks it has a live connection."""
    fake_psycopg = MagicMock()
    fake_conn = MagicMock()
    fake_conn.closed = False
    fake_psycopg.connect.return_value = fake_conn

    # Make `import psycopg` succeed inside the client module
    monkeypatch.setitem(__import__("sys").modules, "psycopg", fake_psycopg)
    # And also psycopg.rows used by _execute_query
    fake_rows = MagicMock()
    fake_rows.dict_row = "dict_row_sentinel"
    monkeypatch.setitem(__import__("sys").modules, "psycopg.rows", fake_rows)

    return fake_psycopg, fake_conn


def _make_cursor(rows: list[dict]) -> MagicMock:
    """Build a MagicMock cursor that yields the given rows."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    return cur


class TestPhoenixPostgresClientLifecycle:
    """Connection lifecycle, repr, and close behavior."""

    def test_repr_masks_password(self):
        """The repr must not expose the password from the URL."""
        client = PhoenixPostgresClient(
            connection_url="postgresql://user:supersecret@host:5432/db",
            project="p",
        )
        s = repr(client)
        assert "supersecret" not in s
        assert "user" in s
        assert "host:5432/db" in s

    def test_repr_handles_url_without_creds(self):
        """Repr should not crash if the URL has no creds."""
        client = PhoenixPostgresClient(
            connection_url="postgresql://host:5432/db",
            project="p",
        )
        # Just shouldn't raise
        repr(client)

    def test_init_without_psycopg_raises(self, monkeypatch):
        """Helpful error if psycopg isn't installed."""
        # Force the import inside __init__ to fail
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psycopg":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(PhoenixPostgresError, match="psycopg"):
            PhoenixPostgresClient(connection_url="postgresql://h/d")


class TestPhoenixPostgresClientQueries:
    """Query shape + result handling."""

    def test_get_total_span_count_filters_by_project(self, mock_psycopg):
        """Total count query filters by project name."""
        _, fake_conn = mock_psycopg
        fake_conn.cursor.return_value = _make_cursor([{"count": 42}])

        client = PhoenixPostgresClient(
            connection_url="postgresql://u:p@h:5432/d",
            project="dev-agent-lens",
            schema="phoenix",
        )
        n = client.get_total_span_count()

        assert n == 42
        # Validate the SQL passed in. The first cursor.execute call sets
        # search_path; the second one runs the count query.
        executed_calls = [c for c in fake_conn.cursor().execute.call_args_list]
        # At least one call must be the COUNT(*) query, parametrized on project
        count_calls = [
            c for c in executed_calls
            if c.args and "COUNT(*)" in str(c.args[0])
        ]
        assert count_calls, "expected COUNT(*) query to be executed"
        assert count_calls[0].args[1][0] == "dev-agent-lens"

    def test_get_spans_dataframe_returns_dataframe(self, mock_psycopg):
        """get_spans_dataframe returns a DataFrame matching SQLite shape."""
        _, fake_conn = mock_psycopg
        rows = [
            {
                "context.span_id": "s1",
                "context.trace_id": "t1",
                "parent_id": None,
                "name": "root",
                "span_kind": "LLM",
                "start_time": datetime(2026, 5, 6, 12, 0),
                "end_time": datetime(2026, 5, 6, 12, 0, 5),
                "status_code": "OK",
                "status_message": None,
                "attributes": {"foo": "bar"},
                "events": [],
                "cumulative_error_count": 0,
                "cumulative_llm_token_count_prompt": 100,
                "cumulative_llm_token_count_completion": 50,
                "llm_token_count_prompt": 100,
                "llm_token_count_completion": 50,
            }
        ]
        fake_conn.cursor.return_value = _make_cursor(rows)

        client = PhoenixPostgresClient(
            connection_url="postgresql://u:p@h:5432/d",
            project="dev-agent-lens",
        )
        df = client.get_spans_dataframe(limit=10)

        assert isinstance(df, pd.DataFrame)
        assert df.shape == (1, 16)
        assert "context.span_id" in df.columns
        assert "context.trace_id" in df.columns
        # JSONB columns must be serialized to strings for SQLite-client parity
        assert df["attributes"].iloc[0] == json.dumps({"foo": "bar"})
        assert df["events"].iloc[0] == json.dumps([])

    def test_get_time_range_raises_on_empty_project(self, mock_psycopg):
        """Empty project surfaces a clear ValueError."""
        _, fake_conn = mock_psycopg
        fake_conn.cursor.return_value = _make_cursor(
            [{"min_time": None, "max_time": None}]
        )

        client = PhoenixPostgresClient(
            connection_url="postgresql://u:p@h:5432/d",
            project="missing-project",
        )

        with pytest.raises(ValueError, match="missing-project"):
            client.get_time_range()

    def test_test_connection_returns_false_on_error(self, mock_psycopg):
        """test_connection swallows errors and returns False."""
        _, fake_conn = mock_psycopg

        # Make the cursor raise to simulate a query error
        bad_cursor = MagicMock()
        bad_cursor.__enter__ = MagicMock(return_value=bad_cursor)
        bad_cursor.__exit__ = MagicMock(return_value=False)
        bad_cursor.execute.side_effect = Exception("boom")
        fake_conn.cursor.return_value = bad_cursor

        client = PhoenixPostgresClient(
            connection_url="postgresql://u:p@h:5432/d",
            project="x",
        )
        # Should not raise — test_connection returns False on any failure
        assert client.test_connection() is False


class TestPhoenixPostgresClientContextManager:
    """Context manager support."""

    def test_context_manager_closes_connection(self, mock_psycopg):
        """Exiting the with block closes the cached connection."""
        _, fake_conn = mock_psycopg
        fake_conn.cursor.return_value = _make_cursor([{"id": 1}])

        with PhoenixPostgresClient(
            connection_url="postgresql://u:p@h:5432/d",
            project="x",
        ) as client:
            client.test_connection()

        # close() should have been called on the cached connection
        fake_conn.close.assert_called()
