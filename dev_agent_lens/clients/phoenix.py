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
                timeout=int(self.timeout),  # Phoenix default is 5s, we use instance timeout
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

    def get_span_annotations_dataframe(
        self,
        spans_dataframe: pd.DataFrame | None = None,
        span_ids: list[str] | None = None,
        project_name: str | None = None,
        include_annotation_names: list[str] | None = None,
        exclude_annotation_names: list[str] | None = None,
        limit: int = 10000,
    ) -> pd.DataFrame:
        """
        Fetch annotations for spans from Phoenix.

        Args:
            spans_dataframe: DataFrame of spans to get annotations for.
                Must have 'context.span_id' or 'span_id' column.
            span_ids: List of span IDs to get annotations for.
                Either spans_dataframe or span_ids must be provided.
            project_name: The project to query. Defaults to instance project_name.
            include_annotation_names: Only include these annotation types.
            exclude_annotation_names: Exclude these annotation types.
            limit: Maximum number of annotations to retrieve. Defaults to 10000.

        Returns:
            A pandas DataFrame containing annotation data.
            Columns include: span_id, name, annotator_kind, label, score,
            explanation, metadata, created_at, updated_at, source, user_id.
            Returns empty DataFrame if no annotations found.

        Raises:
            PhoenixConnectionError: If connection to Phoenix fails.
            ValueError: If neither spans_dataframe nor span_ids provided.
        """
        if spans_dataframe is None and span_ids is None:
            raise ValueError("Either spans_dataframe or span_ids must be provided")

        client = self._get_client()
        project = project_name or self.project_name

        try:
            df = client.spans.get_span_annotations_dataframe(
                spans_dataframe=spans_dataframe,
                span_ids=span_ids,
                project_identifier=project,
                include_annotation_names=include_annotation_names,
                exclude_annotation_names=exclude_annotation_names,
                limit=limit,
                timeout=int(self.timeout),  # Phoenix default is 5s, we use instance timeout
            )

            if df is None:
                return pd.DataFrame()

            return df

        except Exception as e:
            error_str = str(e).lower()
            if "connection" in error_str or "timeout" in error_str or "refused" in error_str:
                raise PhoenixConnectionError(
                    f"Failed to fetch annotations from Phoenix at {self.base_url}: {e}"
                ) from e
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

    def get_date_range(
        self,
        project_name: str | None = None,
    ) -> tuple[datetime | None, datetime | None]:
        """
        Detect the date range of available data.

        This fetches a small sample of spans to determine the date range.

        Args:
            project_name: The project to query. Defaults to instance project_name.

        Returns:
            Tuple of (earliest_date, latest_date), or (None, None) if no data.

        Raises:
            PhoenixConnectionError: If connection to Phoenix fails.
        """
        # Fetch a sample to find date range
        # Phoenix API doesn't support querying without limits, so we fetch
        # spans sorted by time to get the range
        try:
            # Get oldest spans
            df = self.get_spans_dataframe(
                project_name=project_name,
                limit=100,  # Small sample
            )

            if df is None or df.empty:
                return None, None

            # Find the timestamp column
            time_columns = [c for c in df.columns if 'start_time' in c.lower()]
            if not time_columns:
                time_columns = [c for c in df.columns if 'time' in c.lower()]

            if not time_columns:
                return None, None

            time_col = time_columns[0]
            timestamps = pd.to_datetime(df[time_col])
            earliest = timestamps.min()
            latest = timestamps.max()

            # Convert to datetime if needed
            if hasattr(earliest, 'to_pydatetime'):
                earliest = earliest.to_pydatetime()
            if hasattr(latest, 'to_pydatetime'):
                latest = latest.to_pydatetime()

            return earliest, latest

        except Exception as e:
            if "connection" in str(e).lower():
                raise PhoenixConnectionError(f"Failed to get date range: {e}") from e
            return None, None

    def __repr__(self) -> str:
        return f"PhoenixClient(base_url='{self.base_url}', project_name='{self.project_name}')"
