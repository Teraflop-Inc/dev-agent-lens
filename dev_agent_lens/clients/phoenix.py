"""
Phoenix Client Module

Provides a client for connecting to and fetching trace data from Phoenix.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

# Import Phoenix client - may fail if not installed
try:
    from phoenix.client import Client as _PhoenixClient
except ImportError:
    _PhoenixClient = None  # type: ignore


class PhoenixConnectionError(Exception):
    """Raised when connection to Phoenix fails."""

    pass


class PhoenixClient:
    """
    Client for interacting with a Phoenix trace server.

    This client provides methods to connect to a Phoenix instance and fetch
    span data as pandas DataFrames.

    Args:
        base_url: The URL of the Phoenix server. Defaults to DAL_PHOENIX_URL
            environment variable or 'http://localhost:6006'.
        project_name: The Phoenix project to query. Defaults to DAL_PHOENIX_PROJECT
            environment variable or 'default'.
        timeout: Connection timeout in seconds. Defaults to 30.

    Example:
        >>> client = PhoenixClient()
        >>> df = client.get_spans_dataframe()
        >>> print(f"Retrieved {len(df)} spans")
    """

    def __init__(
        self,
        base_url: str | None = None,
        project_name: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url or os.getenv("DAL_PHOENIX_URL", "http://localhost:6006")
        self.project_name = project_name or os.getenv("DAL_PHOENIX_PROJECT", "default")
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        """
        Get or create the Phoenix client instance.

        Returns:
            The Phoenix Client instance.

        Raises:
            PhoenixConnectionError: If connection to Phoenix fails.
        """
        if self._client is None:
            if _PhoenixClient is None:
                raise PhoenixConnectionError(
                    "Phoenix client not installed. Install with: pip install arize-phoenix"
                )
            try:
                self._client = _PhoenixClient(base_url=self.base_url)
            except Exception as e:
                raise PhoenixConnectionError(
                    f"Failed to connect to Phoenix at {self.base_url}: {e}"
                ) from e
        return self._client

    def get_spans_dataframe(
        self,
        project_name: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100000,
    ) -> pd.DataFrame:
        """
        Fetch spans from Phoenix as a pandas DataFrame.

        Args:
            project_name: The project to query. Defaults to instance project_name.
            start_time: Filter spans starting from this time.
            end_time: Filter spans up to this time.
            limit: Maximum number of spans to retrieve. Defaults to 100000.

        Returns:
            A pandas DataFrame containing span data with raw Phoenix schema.
            Returns an empty DataFrame if no spans are found.

        Raises:
            PhoenixConnectionError: If connection to Phoenix fails.
        """
        client = self._get_client()
        project = project_name or self.project_name

        try:
            df = client.spans.get_spans_dataframe(
                project_name=project,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )

            if df is None:
                return pd.DataFrame()

            return df

        except Exception as e:
            # Check if it's a connection error vs other issues
            error_str = str(e).lower()
            if "connection" in error_str or "timeout" in error_str or "refused" in error_str:
                raise PhoenixConnectionError(
                    f"Failed to fetch spans from Phoenix at {self.base_url}: {e}"
                ) from e
            # For other errors (like project not found), just re-raise
            raise

    def test_connection(self) -> bool:
        """
        Test if the connection to Phoenix is working.

        Returns:
            True if connection is successful, False otherwise.
        """
        try:
            self._get_client()
            return True
        except PhoenixConnectionError:
            return False

    def __repr__(self) -> str:
        return f"PhoenixClient(base_url='{self.base_url}', project_name='{self.project_name}')"
