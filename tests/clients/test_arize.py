"""
Tests for ArizeClient.

These tests verify the Arize client functionality including:
- Connection handling
- DataFrame fetching
- Error handling for various failure scenarios
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dev_agent_lens.clients.arize import ArizeClient, ArizeConnectionError


# Patch targets for Arize client
ARIZE_CLIENT_PATCH = "dev_agent_lens.clients.arize._ArizeExportClient"
ARIZE_ENVIRONMENTS_PATCH = "dev_agent_lens.clients.arize._Environments"


class TestArizeClientInit:
    """Tests for ArizeClient initialization."""

    def test_default_values(self):
        """Given no arguments and no env vars, client uses default model_id."""
        with patch.dict("os.environ", {}, clear=True):
            client = ArizeClient()
            assert client.api_key is None
            assert client.space_key is None
            assert client.model_id == "dev-agent-lens"

    def test_env_var_override(self):
        """Given environment variables, client uses env values."""
        with patch.dict(
            "os.environ",
            {
                "ARIZE_API_KEY": "test-api-key",
                "ARIZE_SPACE_KEY": "test-space-key",
                "ARIZE_MODEL_ID": "my-model",
            },
        ):
            client = ArizeClient()
            assert client.api_key == "test-api-key"
            assert client.space_key == "test-space-key"
            assert client.model_id == "my-model"

    def test_explicit_args_override_env(self):
        """Given explicit arguments, they override environment variables."""
        with patch.dict(
            "os.environ",
            {
                "ARIZE_API_KEY": "env-api-key",
                "ARIZE_SPACE_KEY": "env-space-key",
                "ARIZE_MODEL_ID": "env-model",
            },
        ):
            client = ArizeClient(
                api_key="explicit-api-key",
                space_key="explicit-space-key",
                model_id="explicit-model",
            )
            assert client.api_key == "explicit-api-key"
            assert client.space_key == "explicit-space-key"
            assert client.model_id == "explicit-model"


class TestArizeClientConnection:
    """Tests for connection handling."""

    def test_connection_success(self):
        """Given valid credentials, connection succeeds."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            mock_client_class.return_value = MagicMock()
            client = ArizeClient(api_key="test-key", space_key="test-space")
            assert client.test_connection() is True
            mock_client_class.assert_called_once_with(api_key="test-key")

    def test_connection_failure_no_api_key(self):
        """Given no API key, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            mock_client_class.return_value = MagicMock()
            with patch.dict("os.environ", {}, clear=True):
                client = ArizeClient(space_key="test-space")
                with pytest.raises(ArizeConnectionError) as exc_info:
                    client._get_client()
                assert "ARIZE_API_KEY" in str(exc_info.value)

    def test_connection_failure_no_space_key(self):
        """Given no space key, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            mock_client_class.return_value = MagicMock()
            with patch.dict("os.environ", {}, clear=True):
                client = ArizeClient(api_key="test-key")
                with pytest.raises(ArizeConnectionError) as exc_info:
                    client._get_client()
                assert "ARIZE_SPACE_KEY" in str(exc_info.value)

    def test_connection_failure_invalid_credentials(self):
        """Given invalid credentials, connection fails gracefully."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            mock_client_class.side_effect = Exception("Unauthorized")
            client = ArizeClient(api_key="bad-key", space_key="bad-space")
            assert client.test_connection() is False

    def test_arize_not_installed(self):
        """Given arize not installed, raises ArizeConnectionError with helpful message."""
        with patch(ARIZE_CLIENT_PATCH, None):
            client = ArizeClient(api_key="test-key", space_key="test-space")
            with pytest.raises(ArizeConnectionError) as exc_info:
                client._get_client()
            assert "not installed" in str(exc_info.value)


class TestArizeClientFetch:
    """Tests for span fetching."""

    def test_fetch_returns_dataframe(self):
        """Given valid credentials, returns DataFrame with spans."""
        mock_df = pd.DataFrame(
            {
                "context.span_id": ["span1", "span2"],
                "context.trace_id": ["trace1", "trace1"],
                "name": ["LLM", "Tool"],
                "start_time": [datetime.now(), datetime.now()],
            }
        )

        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = mock_df
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                result = client.get_spans_dataframe()

                assert isinstance(result, pd.DataFrame)
                assert len(result) == 2
                assert "context.span_id" in result.columns

    def test_fetch_empty_project(self):
        """Given project with no spans, returns empty DataFrame."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                result = client.get_spans_dataframe()

                assert isinstance(result, pd.DataFrame)
                assert len(result) == 0

    def test_fetch_with_date_filters(self):
        """Given date filters, passes them to Arize."""
        start = datetime(2025, 1, 1)
        end = datetime(2025, 1, 31)

        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe(start_time=start, end_time=end)

                mock_client.export_model_to_df.assert_called_once_with(
                    space_id="test-space",
                    model_id="dev-agent-lens",
                    environment="tracing",
                    start_time=start,
                    end_time=end,
                )

    def test_fetch_with_custom_model_id(self):
        """Given custom model_id, uses it."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe(model_id="custom-model")

                # Check the call was made with the right model_id
                mock_client.export_model_to_df.assert_called_once()
                call_kwargs = mock_client.export_model_to_df.call_args.kwargs
                assert call_kwargs["space_id"] == "test-space"
                assert call_kwargs["model_id"] == "custom-model"
                assert call_kwargs["environment"] == "tracing"
                # start_time and end_time are now automatically added with defaults
                assert "start_time" in call_kwargs
                assert "end_time" in call_kwargs

    def test_fetch_connection_error(self):
        """Given connection failure during fetch, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.side_effect = Exception("Connection refused")
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                with pytest.raises(ArizeConnectionError):
                    client.get_spans_dataframe()

    def test_fetch_timeout_error(self):
        """Given timeout during fetch, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.side_effect = Exception("timeout exceeded")
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                with pytest.raises(ArizeConnectionError):
                    client.get_spans_dataframe()

    def test_fetch_api_error(self):
        """Given API error during fetch, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.side_effect = Exception("API rate limit")
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                with pytest.raises(ArizeConnectionError):
                    client.get_spans_dataframe()

    def test_fetch_none_returns_empty_df(self):
        """Given None response from Arize, returns empty DataFrame."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = None
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                result = client.get_spans_dataframe()

                assert isinstance(result, pd.DataFrame)
                assert len(result) == 0


class TestArizeClientRepr:
    """Tests for string representation."""

    def test_repr(self):
        """Given client, repr shows space_key and model_id."""
        client = ArizeClient(
            api_key="test-key", space_key="my-space", model_id="my-model"
        )
        result = repr(client)
        assert "my-space" in result
        assert "my-model" in result


class TestArizeClientPerformanceParams:
    """Tests for performance parameters in get_spans_dataframe."""

    def test_fetch_with_columns_selection(self):
        """Given specific columns, passes them to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe(columns=["span_id", "name", "start_time"])

                call_kwargs = mock_client.export_model_to_df.call_args.kwargs
                assert call_kwargs["columns"] == ["span_id", "name", "start_time"]

    def test_fetch_with_stream_chunk_size(self):
        """Given stream_chunk_size, passes it to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe(stream_chunk_size=5000)

                call_kwargs = mock_client.export_model_to_df.call_args.kwargs
                assert call_kwargs["stream_chunk_size"] == 5000

    def test_fetch_with_parallelize_exports(self):
        """Given parallelize_exports=True, passes it to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe(parallelize_exports=True)

                call_kwargs = mock_client.export_model_to_df.call_args.kwargs
                assert call_kwargs["parallelize_exports"] is True

    def test_fetch_with_all_performance_params(self):
        """Given all performance parameters, passes them to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe(
                    columns=["span_id", "name"],
                    stream_chunk_size=10000,
                    parallelize_exports=True,
                )

                call_kwargs = mock_client.export_model_to_df.call_args.kwargs
                assert call_kwargs["columns"] == ["span_id", "name"]
                assert call_kwargs["stream_chunk_size"] == 10000
                assert call_kwargs["parallelize_exports"] is True

    def test_fetch_without_optional_params_omits_them(self):
        """Given no optional params, they are not passed to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_df.return_value = pd.DataFrame()
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_dataframe()

                call_kwargs = mock_client.export_model_to_df.call_args.kwargs
                assert "columns" not in call_kwargs
                assert "stream_chunk_size" not in call_kwargs
                assert "parallelize_exports" not in call_kwargs


class TestArizeClientParquetExport:
    """Tests for parquet export functionality."""

    def test_parquet_export_returns_path(self):
        """Given valid credentials, get_spans_parquet returns Path to file."""
        from pathlib import Path

        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.return_value = "/tmp/export.parquet"
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                result = client.get_spans_parquet()

                assert isinstance(result, Path)
                assert str(result) == "/tmp/export.parquet"

    def test_parquet_export_with_custom_path(self):
        """Given custom output_path, passes it to Arize API."""
        from pathlib import Path

        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.return_value = "/custom/path.parquet"
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_parquet(output_path="/custom/path.parquet")

                call_kwargs = mock_client.export_model_to_parquet.call_args.kwargs
                assert call_kwargs["path"] == "/custom/path.parquet"

    def test_parquet_export_with_columns(self):
        """Given specific columns, passes them to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.return_value = "/tmp/export.parquet"
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_parquet(columns=["span_id", "name"])

                call_kwargs = mock_client.export_model_to_parquet.call_args.kwargs
                assert call_kwargs["columns"] == ["span_id", "name"]

    def test_parquet_export_with_performance_params(self):
        """Given performance params, passes them to Arize API."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.return_value = "/tmp/export.parquet"
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_parquet(
                    stream_chunk_size=5000,
                    parallelize_exports=True,
                )

                call_kwargs = mock_client.export_model_to_parquet.call_args.kwargs
                assert call_kwargs["stream_chunk_size"] == 5000
                assert call_kwargs["parallelize_exports"] is True

    def test_parquet_export_none_raises_error(self):
        """Given None response from Arize, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.return_value = None
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                with pytest.raises(ArizeConnectionError) as exc_info:
                    client.get_spans_parquet()
                assert "returned None" in str(exc_info.value)

    def test_parquet_export_connection_error(self):
        """Given connection failure, raises ArizeConnectionError."""
        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.side_effect = Exception(
                    "Connection refused"
                )
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                with pytest.raises(ArizeConnectionError):
                    client.get_spans_parquet()

    def test_parquet_export_with_date_filters(self):
        """Given date filters, passes them to Arize API."""
        start = datetime(2025, 1, 1)
        end = datetime(2025, 1, 31)

        with patch(ARIZE_CLIENT_PATCH) as mock_client_class:
            with patch(ARIZE_ENVIRONMENTS_PATCH) as mock_envs:
                mock_envs.TRACING = "tracing"
                mock_client = MagicMock()
                mock_client.export_model_to_parquet.return_value = "/tmp/export.parquet"
                mock_client_class.return_value = mock_client

                client = ArizeClient(api_key="test-key", space_key="test-space")
                client.get_spans_parquet(start_time=start, end_time=end)

                call_kwargs = mock_client.export_model_to_parquet.call_args.kwargs
                assert call_kwargs["start_time"] == start
                assert call_kwargs["end_time"] == end
