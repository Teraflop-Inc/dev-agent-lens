"""
Arize Client Module

Provides a client for connecting to and fetching trace data from Arize.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

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
        limit: int | None = None,
        columns: list[str] | None = None,
        stream_chunk_size: int | None = None,
        parallelize_exports: bool | None = None,
    ) -> pd.DataFrame:
        """
        Fetch spans from Arize as a pandas DataFrame.

        Args:
            model_id: The model ID to query. Defaults to instance model_id.
            start_time: Filter spans starting from this time.
            end_time: Filter spans up to this time.
            limit: Maximum number of spans to return. Note: Arize API fetches all
                spans in the time range, then truncates to limit if specified.
            columns: List of specific columns to export. If None, exports all columns.
                Use this to reduce data transfer for large exports.
            stream_chunk_size: Number of rows per streaming chunk. Larger values
                may improve performance for large datasets. Default is SDK default.
            parallelize_exports: Enable parallel fetching for faster exports.
                Useful for large datasets.

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

        export_params: dict = {
            "space_id": self.space_key,
            "model_id": model,
            "environment": _Environments.TRACING,
            "start_time": start_time,
            "end_time": end_time,
        }

        # Add optional performance parameters
        if columns is not None:
            export_params["columns"] = columns
        if stream_chunk_size is not None:
            export_params["stream_chunk_size"] = stream_chunk_size
        if parallelize_exports is not None:
            export_params["parallelize_exports"] = parallelize_exports

        try:
            df = client.export_model_to_df(**export_params)

            if df is None or df.empty:
                return pd.DataFrame()

            # Apply limit if specified (Arize API doesn't support limit natively)
            if limit is not None and len(df) > limit:
                df = df.head(limit)

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

    def get_spans_parquet(
        self,
        model_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        output_path: Path | str | None = None,
        columns: list[str] | None = None,
        stream_chunk_size: int | None = None,
        parallelize_exports: bool | None = None,
    ) -> Path:
        """
        Export spans from Arize directly to a parquet file.

        This method is more efficient than get_spans_dataframe() for large datasets
        as it streams data directly to disk without loading everything into memory.

        Args:
            model_id: The model ID to query. Defaults to instance model_id.
            start_time: Filter spans starting from this time.
            end_time: Filter spans up to this time.
            output_path: Custom output path for the parquet file. If None, generates
                a default path under ~/.dal/data/parquet/.
            columns: List of specific columns to export. If None, exports all columns.
            stream_chunk_size: Number of rows per streaming chunk for large exports.
            parallelize_exports: Enable parallel fetching for faster exports.

        Returns:
            Path to the generated parquet file.

        Raises:
            ArizeConnectionError: If connection to Arize fails or export fails.
        """
        client = self._get_client()
        model = model_id or self.model_id

        from datetime import timedelta

        if end_time is None:
            end_time = datetime.now()
        if start_time is None:
            start_time = end_time - timedelta(days=30)

        # Generate default output path if not provided (path is required by Arize SDK)
        if output_path is None:
            parquet_dir = Path.home() / ".dal" / "data" / "parquet"
            parquet_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = parquet_dir / f"arize_{model}_{timestamp}.parquet"

        # Build export params - path is required
        export_params: dict = {
            "path": str(output_path),
            "space_id": self.space_key,
            "model_id": model,
            "environment": _Environments.TRACING,
            "start_time": start_time,
            "end_time": end_time,
        }

        # Add optional parameters only if specified
        if columns is not None:
            export_params["columns"] = columns
        if stream_chunk_size is not None:
            export_params["stream_chunk_size"] = stream_chunk_size
        if parallelize_exports is not None:
            export_params["parallelize_exports"] = parallelize_exports

        try:
            result_path = client.export_model_to_parquet(**export_params)

            if result_path is None:
                raise ArizeConnectionError("Arize export_model_to_parquet returned None")

            return Path(result_path)

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
                    f"Failed to export spans to parquet from Arize: {e}"
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

    # NOTE: get_date_range() was removed because:
    # - Arize SDK has no API to query date ranges without downloading data
    # - Probing requires downloading all data in a time window (expensive)
    # - Users should specify --start-date based on their knowledge of the data

    def __repr__(self) -> str:
        return f"ArizeClient(space_key='{self.space_key}', model_id='{self.model_id}')"
