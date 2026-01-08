"""
Phoenix SQLite Client Module

Provides a client for reading Phoenix data directly from SQLite database,
bypassing the HTTP API for faster and more reliable access.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

logger = logging.getLogger(__name__)


class PhoenixSQLiteError(Exception):
    """Base exception for SQLite client errors."""

    pass


class PhoenixSQLiteConnectionError(PhoenixSQLiteError):
    """Failed to connect to database."""

    pass


class PhoenixSQLiteQueryError(PhoenixSQLiteError):
    """Query execution failed."""

    pass


class PhoenixSQLiteClient:
    """Client for reading Phoenix data directly from SQLite database.

    This bypasses the Phoenix HTTP API for faster, more reliable access.
    Intended for historical sync operations where we need to read large
    amounts of data without HTTP timeouts or server crashes.

    Args:
        db_path: Path to phoenix.db file. Can be:
            - Local path: "/tmp/phoenix.db"
            - Docker path: "docker://container-name:/root/.phoenix/phoenix.db"
        project: Project name to filter by. Defaults to DAL_PHOENIX_PROJECT
            environment variable or 'dev-agent-lens'.
        readonly: Open in read-only mode (safer). Defaults to True.

    Example:
        >>> # Local file access
        >>> client = PhoenixSQLiteClient("/tmp/phoenix.db")
        >>> df = client.get_spans_dataframe(limit=100)

        >>> # Docker container access
        >>> client = PhoenixSQLiteClient(
        ...     "docker://dev-agent-lens-phoenix-1:/root/.phoenix/phoenix.db"
        ... )
        >>> print(f"Total spans: {client.get_total_span_count()}")
    """

    def __init__(
        self,
        db_path: str | Path,
        project: str | None = None,
        readonly: bool = True,
    ) -> None:
        self.db_path = str(db_path)
        self.project = project or os.getenv("DAL_PHOENIX_PROJECT", "dev-agent-lens")
        self.readonly = readonly

        # Parse connection mode
        self._is_docker = self.db_path.startswith("docker://")
        self._container_name: str | None = None
        self._container_db_path: str | None = None

        if self._is_docker:
            self._parse_docker_path()

        # Connection will be created on first use (for local mode)
        self._connection: sqlite3.Connection | None = None

    def _parse_docker_path(self) -> None:
        """Parse Docker connection string into container name and path."""
        if not self.db_path.startswith("docker://"):
            return

        # Format: docker://container-name:/path/to/db
        path_part = self.db_path[len("docker://"):]

        if ":" not in path_part:
            raise PhoenixSQLiteConnectionError(
                f"Invalid Docker path format. Expected 'docker://container:/path', got '{self.db_path}'"
            )

        self._container_name, self._container_db_path = path_part.split(":", 1)

        if not self._container_name or not self._container_db_path:
            raise PhoenixSQLiteConnectionError(
                f"Invalid Docker path format. Expected 'docker://container:/path', got '{self.db_path}'"
            )

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create SQLite connection for local file mode.

        Returns:
            SQLite connection instance.

        Raises:
            PhoenixSQLiteConnectionError: If connection fails or using Docker mode.
        """
        if self._is_docker:
            raise PhoenixSQLiteConnectionError(
                "Cannot use _get_connection() in Docker mode. Use _execute_in_docker() instead."
            )

        if self._connection is None:
            try:
                # Add read-only flag if requested
                uri = f"file:{self.db_path}"
                if self.readonly:
                    uri += "?mode=ro"

                self._connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=30.0,
                )

                # Enable datetime conversion
                self._connection.row_factory = sqlite3.Row

            except sqlite3.Error as e:
                raise PhoenixSQLiteConnectionError(
                    f"Failed to connect to database at {self.db_path}: {e}"
                ) from e

        return self._connection

    def _execute_in_docker(
        self,
        container: str,
        python_code: str,
        timeout: int = 300,
    ) -> str:
        """Execute Python code inside Docker container and return stdout.

        Args:
            container: Docker container name
            python_code: Python code to execute
            timeout: Timeout in seconds (default: 300 = 5 minutes)

        Returns:
            Stdout from the command

        Raises:
            PhoenixSQLiteQueryError: If execution fails
        """
        try:
            result = subprocess.run(
                ["docker", "exec", container, "python3", "-c", python_code],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode != 0:
                raise PhoenixSQLiteQueryError(
                    f"Docker exec failed (exit code {result.returncode}): {result.stderr}"
                )

            return result.stdout

        except subprocess.TimeoutExpired as e:
            raise PhoenixSQLiteQueryError(
                f"Docker exec timed out after {timeout} seconds"
            ) from e
        except FileNotFoundError as e:
            raise PhoenixSQLiteConnectionError(
                "Docker command not found. Is Docker installed and in PATH?"
            ) from e
        except Exception as e:
            raise PhoenixSQLiteQueryError(f"Docker exec failed: {e}") from e

    def _execute_in_docker_streaming(
        self,
        container: str,
        python_code: str,
        timeout: int = 600,
    ) -> Iterator[str]:
        """Execute Python code inside Docker container and stream stdout line by line.

        Args:
            container: Docker container name
            python_code: Python code to execute (should print NDJSON)
            timeout: Timeout in seconds (default: 600 = 10 minutes)

        Yields:
            Each line of stdout as it's produced

        Raises:
            PhoenixSQLiteQueryError: If execution fails
        """
        try:
            process = subprocess.Popen(
                ["docker", "exec", container, "python3", "-u", "-c", python_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            # Stream stdout line by line
            for line in process.stdout:  # type: ignore
                line = line.strip()
                if line:
                    yield line

            # Wait for process to complete
            process.wait(timeout=timeout)

            if process.returncode != 0:
                stderr = process.stderr.read() if process.stderr else ""  # type: ignore
                raise PhoenixSQLiteQueryError(
                    f"Docker exec failed (exit code {process.returncode}): {stderr}"
                )

        except subprocess.TimeoutExpired as e:
            process.kill()
            raise PhoenixSQLiteQueryError(
                f"Docker exec timed out after {timeout} seconds"
            ) from e
        except FileNotFoundError as e:
            raise PhoenixSQLiteConnectionError(
                "Docker command not found. Is Docker installed and in PATH?"
            ) from e

    def _execute_query(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """Execute SQL query and return results as list of dicts.

        Args:
            query: SQL query to execute
            params: Query parameters

        Returns:
            List of rows as dictionaries

        Raises:
            PhoenixSQLiteQueryError: If query execution fails
        """
        if self._is_docker:
            return self._execute_query_docker(query, params)
        else:
            return self._execute_query_local(query, params)

    def _execute_query_local(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """Execute query on local SQLite file."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(query, params)

            # Convert rows to dicts
            columns = [desc[0] for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

            return rows

        except sqlite3.Error as e:
            raise PhoenixSQLiteQueryError(f"Query failed: {e}") from e

    def _execute_query_docker(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        """Execute query via Docker container."""
        # Build Python code to execute query
        python_code = f'''
import sqlite3
import json

conn = sqlite3.connect({json.dumps(self._container_db_path)})
cursor = conn.cursor()
cursor.execute({json.dumps(query)}, {json.dumps(params)})

columns = [desc[0] for desc in cursor.description]
rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

print(json.dumps(rows))
conn.close()
'''

        stdout = self._execute_in_docker(
            self._container_name,  # type: ignore
            python_code,
        )

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise PhoenixSQLiteQueryError(f"Failed to parse Docker output: {e}") from e

    def get_spans_dataframe(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> pd.DataFrame:
        """Fetch spans as a DataFrame, matching PhoenixClient interface.

        Args:
            start_time: Filter spans starting at or after this time
            end_time: Filter spans starting before this time
            limit: Maximum number of rows to return
            offset: Number of rows to skip (for pagination)

        Returns:
            DataFrame with columns matching Phoenix HTTP API output:
            - context.span_id
            - context.trace_id
            - parent_id
            - name
            - span_kind
            - start_time
            - end_time
            - status_code
            - status_message
            - attributes (as dict/JSON)
            - events (as dict/JSON)
            - cumulative_error_count
            - cumulative_llm_token_count_prompt
            - cumulative_llm_token_count_completion
            - llm_token_count_prompt
            - llm_token_count_completion

        Raises:
            PhoenixSQLiteQueryError: If query execution fails
        """
        # Build query with filters
        query = """
            SELECT
                s.span_id as "context.span_id",
                t.trace_id as "context.trace_id",
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
            WHERE p.name = ?
        """

        params: list[Any] = [self.project]

        # Add time filters
        if start_time is not None:
            query += " AND s.start_time >= ?"
            params.append(start_time.isoformat())

        if end_time is not None:
            query += " AND s.start_time < ?"
            params.append(end_time.isoformat())

        # Add ordering
        query += " ORDER BY s.start_time ASC"

        # Add pagination
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        if offset > 0:
            query += " OFFSET ?"
            params.append(offset)

        # Execute query
        rows = self._execute_query(query, tuple(params))

        if not rows:
            return pd.DataFrame()

        # Convert to DataFrame
        df = pd.DataFrame(rows)

        # Parse JSON columns
        for col in ["attributes", "events"]:
            if col in df.columns:
                df[col] = df[col].apply(self._parse_json_column)

        # Convert timestamp strings to datetime
        for col in ["start_time", "end_time"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])

        return df

    def _parse_json_column(self, value: Any) -> Any:
        """Parse JSON string column to dict/list."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def get_spans_dataframe_streaming(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        chunk_size: int = 10000,
    ) -> Iterator[pd.DataFrame]:
        """Fetch spans as DataFrames in chunks, using streaming to avoid OOM.

        This method is designed for fetching large numbers of spans from Docker
        without running out of memory. It streams rows one at a time via NDJSON
        and yields DataFrames in chunks.

        Args:
            start_time: Filter spans starting at or after this time
            end_time: Filter spans starting before this time
            chunk_size: Number of rows per DataFrame chunk (default: 10000)

        Yields:
            DataFrame chunks with the same schema as get_spans_dataframe()

        Raises:
            PhoenixSQLiteQueryError: If query execution fails
        """
        if not self._is_docker:
            # For local mode, just use regular method (no streaming needed)
            df = self.get_spans_dataframe(start_time=start_time, end_time=end_time)
            if not df.empty:
                yield df
            return

        # Build query (no LIMIT/OFFSET - we stream everything)
        query = """
            SELECT
                s.span_id as "context.span_id",
                t.trace_id as "context.trace_id",
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
            WHERE p.name = ?
        """

        params: list[Any] = [self.project]

        if start_time is not None:
            query += " AND s.start_time >= ?"
            params.append(start_time.isoformat())

        if end_time is not None:
            query += " AND s.start_time < ?"
            params.append(end_time.isoformat())

        query += " ORDER BY s.start_time ASC"

        # Build Python code that streams NDJSON (one JSON object per line)
        python_code = f'''
import sqlite3
import json
import sys

conn = sqlite3.connect({json.dumps(self._container_db_path)})
cursor = conn.cursor()
cursor.execute({json.dumps(query)}, {json.dumps(params)})

columns = [desc[0] for desc in cursor.description]

# Stream one row at a time as NDJSON (memory-efficient)
for row in cursor:
    record = dict(zip(columns, row))
    print(json.dumps(record, default=str))
    sys.stdout.flush()

conn.close()
'''

        # Collect rows in chunks and yield DataFrames
        chunk: list[dict[str, Any]] = []

        for line in self._execute_in_docker_streaming(
            self._container_name,  # type: ignore
            python_code,
        ):
            try:
                row = json.loads(line)
                chunk.append(row)

                if len(chunk) >= chunk_size:
                    yield self._rows_to_dataframe(chunk)
                    chunk = []

            except json.JSONDecodeError:
                logger.warning(f"Failed to parse NDJSON line: {line[:100]}...")
                continue

        # Yield remaining rows
        if chunk:
            yield self._rows_to_dataframe(chunk)

    def _rows_to_dataframe(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        """Convert list of row dicts to DataFrame with proper type conversion."""
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # Parse JSON columns
        for col in ["attributes", "events"]:
            if col in df.columns:
                df[col] = df[col].apply(self._parse_json_column)

        # Convert timestamp strings to datetime
        for col in ["start_time", "end_time"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])

        return df

    def get_span_annotations_dataframe(
        self,
        spans_dataframe: pd.DataFrame | None = None,
        span_ids: list[str] | None = None,
    ) -> pd.DataFrame:
        """Fetch annotations for given spans.

        Args:
            spans_dataframe: DataFrame with 'context.span_id' column
            span_ids: Alternative - list of span IDs directly

        Returns:
            DataFrame with annotation data including:
            - span_id
            - name
            - label
            - score
            - explanation
            - metadata
            - annotator_kind
            - created_at
            - updated_at

        Raises:
            ValueError: If neither spans_dataframe nor span_ids provided
            PhoenixSQLiteQueryError: If query execution fails
        """
        if spans_dataframe is None and span_ids is None:
            raise ValueError("Either spans_dataframe or span_ids must be provided")

        # Extract span IDs
        if span_ids is None:
            if "context.span_id" in spans_dataframe.columns:  # type: ignore
                span_ids = spans_dataframe["context.span_id"].tolist()  # type: ignore
            elif "span_id" in spans_dataframe.columns:  # type: ignore
                span_ids = spans_dataframe["span_id"].tolist()  # type: ignore
            else:
                raise ValueError(
                    "spans_dataframe must have 'context.span_id' or 'span_id' column"
                )

        if not span_ids:
            return pd.DataFrame()

        # Build query with IN clause
        placeholders = ",".join("?" * len(span_ids))
        query = f"""
            SELECT
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
            WHERE s.span_id IN ({placeholders})
            ORDER BY sa.created_at DESC
        """

        rows = self._execute_query(query, tuple(span_ids))

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # Parse metadata if present
        if "metadata" in df.columns:
            df["metadata"] = df["metadata"].apply(self._parse_json_column)

        # Convert timestamp strings to datetime
        for col in ["created_at", "updated_at"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col])

        return df

    def get_total_span_count(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int:
        """Get count of spans in time range (for progress estimation).

        Args:
            start_time: Filter spans starting at or after this time
            end_time: Filter spans starting before this time

        Returns:
            Number of spans matching the filters

        Raises:
            PhoenixSQLiteQueryError: If query execution fails
        """
        query = """
            SELECT COUNT(*) as count
            FROM spans s
            JOIN traces t ON s.trace_rowid = t.id
            JOIN projects p ON t.project_rowid = p.id
            WHERE p.name = ?
        """

        params: list[Any] = [self.project]

        if start_time is not None:
            query += " AND s.start_time >= ?"
            params.append(start_time.isoformat())

        if end_time is not None:
            query += " AND s.start_time < ?"
            params.append(end_time.isoformat())

        rows = self._execute_query(query, tuple(params))

        if not rows:
            return 0

        return int(rows[0]["count"])

    def get_time_range(self) -> tuple[datetime, datetime]:
        """Get the min and max start_time of all spans in the project.

        Returns:
            Tuple of (min_time, max_time)

        Raises:
            PhoenixSQLiteQueryError: If query execution fails
            ValueError: If no spans found in project
        """
        query = """
            SELECT
                MIN(s.start_time) as min_time,
                MAX(s.start_time) as max_time
            FROM spans s
            JOIN traces t ON s.trace_rowid = t.id
            JOIN projects p ON t.project_rowid = p.id
            WHERE p.name = ?
        """

        rows = self._execute_query(query, (self.project,))

        if not rows or rows[0]["min_time"] is None:
            raise ValueError(f"No spans found in project '{self.project}'")

        min_time = pd.to_datetime(rows[0]["min_time"])
        max_time = pd.to_datetime(rows[0]["max_time"])

        return (min_time.to_pydatetime(), max_time.to_pydatetime())

    def test_connection(self) -> bool:
        """Verify database is accessible and has expected schema.

        Returns:
            True if connection successful and schema valid, False otherwise
        """
        try:
            # Test basic query - check if projects table exists and has our project
            query = "SELECT COUNT(*) as count FROM projects WHERE name = ?"
            rows = self._execute_query(query, (self.project,))

            if not rows:
                logger.warning(f"Project '{self.project}' not found in database")
                return False

            project_count = rows[0]["count"]
            if project_count == 0:
                logger.warning(f"Project '{self.project}' not found in database")
                return False

            logger.info(f"Successfully connected to Phoenix SQLite database (project: {self.project})")
            return True

        except (PhoenixSQLiteError, Exception) as e:
            logger.error(f"Connection test failed: {e}")
            return False

    def close(self) -> None:
        """Close database connection (for local mode only)."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> PhoenixSQLiteClient:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - close connection."""
        self.close()

    def __repr__(self) -> str:
        return f"PhoenixSQLiteClient(db_path='{self.db_path}', project='{self.project}')"
