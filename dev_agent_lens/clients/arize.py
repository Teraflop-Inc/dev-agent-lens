"""
Arize Client Module

Provides a client for connecting to and fetching trace data from Arize.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

# Import Arize client - may fail if not installed
try:
    from arize.exporter import ArizeExportClient as _ArizeExportClient
    from arize.utils.types import Environments as _Environments
except ImportError:
    _ArizeExportClient = None  # type: ignore
    _Environments = None  # type: ignore


class ArizeConnectionError(Exception):
    """Raised when connection to Arize fails."""

    pass


class ArizeClient:
    """
    Client for interacting with Arize AX platform.

    This client provides methods to connect to Arize and fetch
    trace data as pandas DataFrames.

    Args:
        api_key: The Arize API key. Defaults to ARIZE_API_KEY environment variable.
        space_key: The Arize space key. Defaults to ARIZE_SPACE_KEY environment variable.
        model_id: The model ID in Arize. Defaults to ARIZE_MODEL_ID environment variable
            or 'dev-agent-lens'.

    Example:
        >>> client = ArizeClient()
        >>> df = client.get_spans_dataframe()
        >>> print(f"Retrieved {len(df)} spans")
    """

    def __init__(
        self,
        api_key: str | None = None,
        space_key: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ARIZE_API_KEY")
        self.space_key = space_key or os.getenv("ARIZE_SPACE_KEY")
        self.model_id = model_id or os.getenv("ARIZE_MODEL_ID", "dev-agent-lens")
        self._client = None

    def _get_client(self):
        """
        Get or create the Arize client instance.

        Returns:
            The Arize ExportClient instance.

        Raises:
            ArizeConnectionError: If connection to Arize fails.
        """
        if self._client is None:
            if _ArizeExportClient is None:
                raise ArizeConnectionError(
                    "Arize client not installed. Install with: pip install arize"
                )
            if not self.api_key:
                raise ArizeConnectionError(
                    "ARIZE_API_KEY not set. Set environment variable or pass api_key parameter."
                )
            if not self.space_key:
                raise ArizeConnectionError(
                    "ARIZE_SPACE_KEY not set. Set environment variable or pass space_key parameter."
                )
            try:
                self._client = _ArizeExportClient(api_key=self.api_key)
            except Exception as e:
                raise ArizeConnectionError(f"Failed to connect to Arize: {e}") from e
        return self._client

    def get_spans_dataframe(
        self,
        model_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pd.DataFrame:
        """
        Fetch spans from Arize as a pandas DataFrame.

        Args:
            model_id: The model ID to query. Defaults to instance model_id.
            start_time: Filter spans starting from this time.
            end_time: Filter spans up to this time.

        Returns:
            A pandas DataFrame containing span data with raw Arize schema.
            Returns an empty DataFrame if no spans are found.

        Raises:
            ArizeConnectionError: If connection to Arize fails.
        """
        client = self._get_client()
        model = model_id or self.model_id

        # Arize API requires start_time and end_time
        # Default to last 30 days if not specified
        from datetime import timedelta

        if end_time is None:
            end_time = datetime.now()
        if start_time is None:
            start_time = end_time - timedelta(days=30)

        export_params = {
            "space_id": self.space_key,
            "model_id": model,
            "environment": _Environments.TRACING,
            "start_time": start_time,
            "end_time": end_time,
        }

        try:
            df = client.export_model_to_df(**export_params)

            if df is None or df.empty:
                return pd.DataFrame()

            return df

        except Exception as e:
            error_str = str(e).lower()
            if (
                "connection" in error_str
                or "timeout" in error_str
                or "refused" in error_str
                or "unauthorized" in error_str
                or "api" in error_str
            ):
                raise ArizeConnectionError(
                    f"Failed to fetch spans from Arize: {e}"
                ) from e
            raise

    def test_connection(self) -> bool:
        """
        Test if the connection to Arize is working.

        Returns:
            True if connection is successful, False otherwise.
        """
        try:
            self._get_client()
            return True
        except ArizeConnectionError:
            return False

    def __repr__(self) -> str:
        return f"ArizeClient(space_key='{self.space_key}', model_id='{self.model_id}')"
