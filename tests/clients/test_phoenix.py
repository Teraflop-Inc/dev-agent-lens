"""
Tests for PhoenixClient.

These tests verify the Phoenix client functionality including:
- Connection handling
- DataFrame fetching
- Error handling for various failure scenarios
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dev_agent_lens.clients.phoenix import PhoenixClient, PhoenixConnectionError


# Patch target for the Phoenix client
PHOENIX_CLIENT_PATCH = "dev_agent_lens.clients.phoenix._PhoenixClient"


class TestPhoenixClientInit:
    """Tests for PhoenixClient initialization."""

    def test_default_values(self):
        """Given no arguments, client uses default URL and project."""
        with patch.dict("os.environ", {}, clear=True):
            client = PhoenixClient()
            assert client.base_url == "http://localhost:6006"
            assert client.project_name == "default"
            assert client.timeout == 30.0

    def test_env_var_override(self):
        """Given environment variables, client uses env values."""
        with patch.dict(
            "os.environ",
            {"DAL_PHOENIX_URL": "http://phoenix:8080", "DAL_PHOENIX_PROJECT": "my-project"},
        ):
            client = PhoenixClient()
            assert client.base_url == "http://phoenix:8080"
            assert client.project_name == "my-project"

    def test_explicit_args_override_env(self):
        """Given explicit arguments, they override environment variables."""
        with patch.dict(
            "os.environ",
            {"DAL_PHOENIX_URL": "http://env-url:8080", "DAL_PHOENIX_PROJECT": "env-project"},
        ):
            client = PhoenixClient(
                base_url="http://explicit:9090", project_name="explicit-project"
            )
            assert client.base_url == "http://explicit:9090"
            assert client.project_name == "explicit-project"

    def test_custom_timeout(self):
        """Given custom timeout, client uses it."""
        client = PhoenixClient(timeout=60.0)
        assert client.timeout == 60.0


class TestPhoenixClientConnection:
    """Tests for connection handling."""

    def test_connection_success(self):
        """Given valid Phoenix server, connection succeeds."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client_class.return_value = MagicMock()
            client = PhoenixClient()
            assert client.test_connection() is True
            mock_client_class.assert_called_once_with(base_url="http://localhost:6006")

    def test_connection_failure_invalid_url(self):
        """Given invalid URL, raises PhoenixConnectionError."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client_class.side_effect = Exception("Connection refused")
            client = PhoenixClient(base_url="http://invalid:9999")
            assert client.test_connection() is False

    def test_phoenix_not_installed(self):
        """Given phoenix not installed, raises PhoenixConnectionError with helpful message."""
        with patch(PHOENIX_CLIENT_PATCH, None):
            client = PhoenixClient()
            with pytest.raises(PhoenixConnectionError) as exc_info:
                client._get_client()
            assert "not installed" in str(exc_info.value)


class TestPhoenixClientFetch:
    """Tests for span fetching."""

    def test_fetch_returns_dataframe(self):
        """Given valid project, returns DataFrame with spans."""
        mock_df = pd.DataFrame(
            {
                "context.span_id": ["span1", "span2"],
                "context.trace_id": ["trace1", "trace1"],
                "name": ["LLM", "Tool"],
                "start_time": [datetime.now(), datetime.now()],
            }
        )

        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.return_value = mock_df
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_spans_dataframe()

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 2
            assert "context.span_id" in result.columns

    def test_fetch_empty_project(self):
        """Given project with no spans, returns empty DataFrame."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.return_value = pd.DataFrame()
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_spans_dataframe()

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 0

    def test_fetch_with_date_filters(self):
        """Given date filters, passes them to Phoenix."""
        start = datetime(2025, 1, 1)
        end = datetime(2025, 1, 31)

        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.return_value = pd.DataFrame()
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            client.get_spans_dataframe(start_time=start, end_time=end)

            mock_client.spans.get_spans_dataframe.assert_called_once_with(
                project_name="default",
                start_time=start,
                end_time=end,
                limit=100000,
                timeout=30,  # Default timeout from PhoenixClient
            )

    def test_fetch_with_custom_limit(self):
        """Given custom limit, uses it."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.return_value = pd.DataFrame()
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            client.get_spans_dataframe(limit=500)

            mock_client.spans.get_spans_dataframe.assert_called_once_with(
                project_name="default",
                start_time=None,
                end_time=None,
                limit=500,
                timeout=30,  # Default timeout from PhoenixClient
            )

    def test_fetch_connection_error(self):
        """Given connection failure during fetch, raises PhoenixConnectionError."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.side_effect = Exception("Connection refused")
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            with pytest.raises(PhoenixConnectionError):
                client.get_spans_dataframe()

    def test_fetch_timeout_error(self):
        """Given timeout during fetch, raises PhoenixConnectionError."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.side_effect = Exception("timeout exceeded")
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            with pytest.raises(PhoenixConnectionError):
                client.get_spans_dataframe()

    def test_fetch_none_returns_empty_df(self):
        """Given None response from Phoenix, returns empty DataFrame."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_spans_dataframe.return_value = None
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_spans_dataframe()

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 0


class TestPhoenixClientAnnotations:
    """Tests for annotation fetching."""

    def test_fetch_annotations_with_spans_df(self):
        """Given spans DataFrame, fetches annotations for those spans."""
        mock_spans = pd.DataFrame({
            "context.span_id": ["span-001", "span-002"],
            "name": ["LLM", "Tool"],
        })
        mock_annotations = pd.DataFrame({
            "id": ["ann-001"],
            "span_id": ["span-001"],
            "name": ["helpfulness"],
            "annotator_kind": ["HUMAN"],
            "result": [{"label": "good", "score": 0.9}],
        })

        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_span_annotations_dataframe.return_value = mock_annotations
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_span_annotations_dataframe(spans_dataframe=mock_spans)

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 1

    def test_fetch_annotations_with_span_ids(self):
        """Given list of span IDs, fetches annotations."""
        mock_annotations = pd.DataFrame({
            "id": ["ann-001", "ann-002"],
            "span_id": ["span-001", "span-002"],
            "name": ["quality", "accuracy"],
        })

        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_span_annotations_dataframe.return_value = mock_annotations
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_span_annotations_dataframe(
                span_ids=["span-001", "span-002"]
            )

            assert len(result) == 2

    def test_fetch_annotations_requires_spans_or_ids(self):
        """Given neither spans_dataframe nor span_ids, raises ValueError."""
        client = PhoenixClient()
        with pytest.raises(ValueError, match="Either spans_dataframe or span_ids"):
            client.get_span_annotations_dataframe()

    def test_fetch_annotations_empty_returns_empty_df(self):
        """Given no annotations exist, returns empty DataFrame."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_span_annotations_dataframe.return_value = pd.DataFrame()
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_span_annotations_dataframe(span_ids=["span-001"])

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 0

    def test_fetch_annotations_none_returns_empty_df(self):
        """Given None response, returns empty DataFrame."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_span_annotations_dataframe.return_value = None
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            result = client.get_span_annotations_dataframe(span_ids=["span-001"])

            assert isinstance(result, pd.DataFrame)
            assert len(result) == 0

    def test_fetch_annotations_with_filters(self):
        """Given annotation name filters, passes them correctly."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_span_annotations_dataframe.return_value = pd.DataFrame()
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            client.get_span_annotations_dataframe(
                span_ids=["span-001"],
                include_annotation_names=["quality"],
                exclude_annotation_names=["note"],
            )

            mock_client.spans.get_span_annotations_dataframe.assert_called_once_with(
                spans_dataframe=None,
                span_ids=["span-001"],
                project_identifier="default",
                include_annotation_names=["quality"],
                exclude_annotation_names=["note"],
                limit=10000,
                timeout=30,  # Default timeout from PhoenixClient
            )

    def test_fetch_annotations_connection_error(self):
        """Given connection failure, raises PhoenixConnectionError."""
        with patch(PHOENIX_CLIENT_PATCH) as mock_client_class:
            mock_client = MagicMock()
            mock_client.spans.get_span_annotations_dataframe.side_effect = Exception(
                "Connection refused"
            )
            mock_client_class.return_value = mock_client

            client = PhoenixClient()
            with pytest.raises(PhoenixConnectionError):
                client.get_span_annotations_dataframe(span_ids=["span-001"])


class TestPhoenixClientRepr:
    """Tests for string representation."""

    def test_repr(self):
        """Given client, repr shows URL and project."""
        client = PhoenixClient(base_url="http://test:8080", project_name="my-project")
        result = repr(client)
        assert "http://test:8080" in result
        assert "my-project" in result
