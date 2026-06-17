"""
Tests for unified schema module.

These tests verify schema normalization including:
- Phoenix DataFrame normalization
- Arize DataFrame normalization
- Timestamp conversion
- Field mapping consistency
- Missing field handling
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from dev_agent_lens.core.schema import (
    UNIFIED_COLUMNS,
    normalize_arize,
    normalize_phoenix,
)

LITELLM_USER_STRING = (
    "user_abc123def_account_11111111-1111-1111-1111-111111111111"
    "_session_22222222-2222-2222-2222-222222222222"
)


class TestNormalizePhoenix:
    """Tests for Phoenix DataFrame normalization."""

    def test_empty_dataframe(self):
        """Given empty DataFrame, returns empty DataFrame with unified columns."""
        df = pd.DataFrame()
        result = normalize_phoenix(df)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == UNIFIED_COLUMNS

    def test_basic_fields(self):
        """Given Phoenix DataFrame with basic fields, maps correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "parent_id": ["parent1"],
                "name": ["test_span"],
                "span_kind": ["LLM"],
            }
        )

        result = normalize_phoenix(df)

        assert len(result) == 1
        assert result.iloc[0]["span_id"] == "span1"
        assert result.iloc[0]["trace_id"] == "trace1"
        assert result.iloc[0]["parent_id"] == "parent1"
        assert result.iloc[0]["name"] == "test_span"
        assert result.iloc[0]["span_kind"] == "LLM"
        assert result.iloc[0]["backend"] == "phoenix"

    def test_user_attribution_columns_present(self):
        """Unified schema exposes user_id and account_id columns."""
        assert "user_id" in UNIFIED_COLUMNS
        assert "account_id" in UNIFIED_COLUMNS

    def test_user_attribution_extracted_from_nested_metadata(self):
        """Given Phoenix span with nested proxy metadata, populates attribution."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "attributes": [
                    {"metadata": {"user_api_key_end_user_id": LITELLM_USER_STRING}}
                ],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["user_id"] == "abc123def"
        assert result.iloc[0]["account_id"] == (
            "11111111-1111-1111-1111-111111111111"
        )

    def test_user_attribution_none_without_metadata(self):
        """Given a span without proxy metadata, attribution fields are None."""
        df = pd.DataFrame({"context.span_id": ["span1"], "name": ["tool_call"]})

        result = normalize_phoenix(df)

        assert result.iloc[0]["user_id"] is None
        assert result.iloc[0]["account_id"] is None

    def test_timestamp_conversion_datetime(self):
        """Given datetime timestamps, converts to ISO-8601."""
        now = datetime(2025, 1, 15, 10, 30, 0)
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "start_time": [now],
                "end_time": [now],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["start_time"] == "2025-01-15T10:30:00"
        assert result.iloc[0]["end_time"] == "2025-01-15T10:30:00"

    def test_timestamp_conversion_pandas(self):
        """Given pandas Timestamp, converts to ISO-8601."""
        ts = pd.Timestamp("2025-01-15 10:30:00")
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "start_time": [ts],
            }
        )

        result = normalize_phoenix(df)

        assert "2025-01-15T10:30:00" in result.iloc[0]["start_time"]

    def test_llm_fields(self):
        """Given LLM-specific fields, maps correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.llm.model_name": ["claude-3-sonnet"],
                "attributes.llm.token_count.prompt": [100],
                "attributes.llm.token_count.completion": [50],
                "attributes.llm.token_count.total": [150],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["llm_model_name"] == "claude-3-sonnet"
        assert result.iloc[0]["llm_token_count_prompt"] == 100
        assert result.iloc[0]["llm_token_count_completion"] == 50
        assert result.iloc[0]["llm_token_count_total"] == 150

    def test_content_fields(self):
        """Given input/output content, maps correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.input.value": ["user input"],
                "attributes.output.value": ["assistant output"],
                "attributes.llm.input_messages": ['[{"role": "user"}]'],
                "attributes.llm.output_messages": ['[{"role": "assistant"}]'],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["input_value"] == "user input"
        assert result.iloc[0]["output_value"] == "assistant output"
        assert result.iloc[0]["input_messages"] == '[{"role": "user"}]'
        assert result.iloc[0]["output_messages"] == '[{"role": "assistant"}]'

    def test_missing_optional_fields(self):
        """Given missing optional fields, returns None not KeyError."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["parent_id"] is None
        assert result.iloc[0]["llm_model_name"] is None
        assert result.iloc[0]["input_value"] is None

    def test_nan_values(self):
        """Given NaN values, returns None."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "parent_id": [float("nan")],
                "attributes.llm.model_name": [None],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["parent_id"] is None
        assert result.iloc[0]["llm_model_name"] is None

    def test_span_kind_fallback(self):
        """Given openinference span kind, uses it as fallback."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.openinference.span.kind": ["TOOL"],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["span_kind"] == "TOOL"


class TestNormalizeArize:
    """Tests for Arize DataFrame normalization."""

    def test_empty_dataframe(self):
        """Given empty DataFrame, returns empty DataFrame with unified columns."""
        df = pd.DataFrame()
        result = normalize_arize(df)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == UNIFIED_COLUMNS

    def test_basic_fields(self):
        """Given Arize DataFrame with basic fields, maps correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "parent_id": ["parent1"],
                "name": ["test_span"],
                "attributes.openinference.span.kind": ["LLM"],
            }
        )

        result = normalize_arize(df)

        assert len(result) == 1
        assert result.iloc[0]["span_id"] == "span1"
        assert result.iloc[0]["trace_id"] == "trace1"
        assert result.iloc[0]["parent_id"] == "parent1"
        assert result.iloc[0]["name"] == "test_span"
        assert result.iloc[0]["span_kind"] == "LLM"
        assert result.iloc[0]["backend"] == "arize"

    def test_timestamp_conversion_milliseconds(self):
        """Given millisecond timestamps, converts to ISO-8601."""
        # Jan 15, 2025 10:30:00 in milliseconds
        ms_timestamp = 1736936400000
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "start_time": [ms_timestamp],
            }
        )

        result = normalize_arize(df)

        # Should contain the date
        assert "2025-01-15" in result.iloc[0]["start_time"]

    def test_llm_fields(self):
        """Given LLM-specific fields, maps correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.llm.model_name": ["claude-3-sonnet"],
                "attributes.llm.token_count.prompt": [100],
                "attributes.llm.token_count.completion": [50],
                "attributes.llm.token_count.total": [150],
            }
        )

        result = normalize_arize(df)

        assert result.iloc[0]["llm_model_name"] == "claude-3-sonnet"
        assert result.iloc[0]["llm_token_count_prompt"] == 100
        assert result.iloc[0]["llm_token_count_completion"] == 50
        assert result.iloc[0]["llm_token_count_total"] == 150

    def test_content_fields(self):
        """Given input/output content, maps correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.input.value": ["user input"],
                "attributes.output.value": ["assistant output"],
            }
        )

        result = normalize_arize(df)

        assert result.iloc[0]["input_value"] == "user input"
        assert result.iloc[0]["output_value"] == "assistant output"

    def test_missing_optional_fields(self):
        """Given missing optional fields, returns None not KeyError."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
            }
        )

        result = normalize_arize(df)

        assert result.iloc[0]["parent_id"] is None
        assert result.iloc[0]["llm_model_name"] is None
        assert result.iloc[0]["input_value"] is None


class TestSchemaParity:
    """Tests ensuring Phoenix and Arize produce identical output structure."""

    def test_same_columns(self):
        """Given data from both backends, output has same columns."""
        phoenix_df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
            }
        )
        arize_df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
            }
        )

        phoenix_result = normalize_phoenix(phoenix_df)
        arize_result = normalize_arize(arize_df)

        assert list(phoenix_result.columns) == list(arize_result.columns)
        assert list(phoenix_result.columns) == UNIFIED_COLUMNS

    def test_same_span_identical_output(self):
        """Given same span from both backends, output matches (except backend field)."""
        now = datetime(2025, 1, 15, 10, 30, 0)
        common_data = {
            "context.span_id": ["span1"],
            "context.trace_id": ["trace1"],
            "parent_id": ["parent1"],
            "name": ["test_span"],
            "start_time": [now],
            "end_time": [now],
            "attributes.input.value": ["test input"],
            "attributes.output.value": ["test output"],
            "attributes.llm.model_name": ["claude-3-sonnet"],
            "attributes.llm.token_count.total": [100],
        }

        # Phoenix uses span_kind, Arize uses attributes.openinference.span.kind
        phoenix_df = pd.DataFrame({**common_data, "span_kind": ["LLM"]})
        arize_df = pd.DataFrame(
            {**common_data, "attributes.openinference.span.kind": ["LLM"]}
        )

        phoenix_result = normalize_phoenix(phoenix_df)
        arize_result = normalize_arize(arize_df)

        # Compare all fields except 'backend' and 'raw_attributes'
        # (raw_attributes contains backend-specific column names)
        for col in UNIFIED_COLUMNS:
            if col == "backend":
                assert phoenix_result.iloc[0][col] == "phoenix"
                assert arize_result.iloc[0][col] == "arize"
            elif col == "raw_attributes":
                # Skip raw_attributes - it contains backend-specific column names
                continue
            else:
                assert phoenix_result.iloc[0][col] == arize_result.iloc[0][col], (
                    f"Mismatch in {col}: "
                    f"phoenix={phoenix_result.iloc[0][col]}, "
                    f"arize={arize_result.iloc[0][col]}"
                )

    def test_type_consistency(self):
        """Given same field, type is consistent regardless of backend."""
        import numbers

        phoenix_df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.llm.token_count.total": [100],
            }
        )
        arize_df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "attributes.llm.token_count.total": [100],
            }
        )

        phoenix_result = normalize_phoenix(phoenix_df)
        arize_result = normalize_arize(arize_df)

        # Both should have integer type for token count (including numpy int types)
        assert isinstance(phoenix_result.iloc[0]["llm_token_count_total"], numbers.Integral)
        assert isinstance(arize_result.iloc[0]["llm_token_count_total"], numbers.Integral)


class TestSchemaSnapshots:
    """Snapshot tests comparing Phoenix vs Arize output structure."""

    # Expected unified output for a complete span (excluding raw_attributes which varies)
    EXPECTED_LLM_SPAN = {
        "span_id": "span-001",
        "trace_id": "trace-001",
        "parent_id": "parent-001",
        "name": "LiteLLM Call",
        "span_kind": "LLM",
        "start_time": "2025-01-15T10:30:00",
        "end_time": "2025-01-15T10:30:05",
        "status_code": "OK",
        "input_value": "Hello, how are you?",
        "output_value": "I'm doing well, thank you!",
        "input_messages": '[{"role": "user", "content": "Hello"}]',
        "output_messages": '[{"role": "assistant", "content": "Hi"}]',
        "llm_model_name": "claude-3-sonnet-20240229",
        "llm_token_count_prompt": 10,
        "llm_token_count_completion": 15,
        "llm_token_count_total": 25,
        # raw_attributes now contains original row data for metadata extraction
    }

    def test_phoenix_snapshot_llm_span(self):
        """Given Phoenix LLM span, output matches expected snapshot."""
        phoenix_df = pd.DataFrame(
            {
                "context.span_id": ["span-001"],
                "context.trace_id": ["trace-001"],
                "parent_id": ["parent-001"],
                "name": ["LiteLLM Call"],
                "span_kind": ["LLM"],
                "start_time": [datetime(2025, 1, 15, 10, 30, 0)],
                "end_time": [datetime(2025, 1, 15, 10, 30, 5)],
                "status_code": ["OK"],
                "attributes.input.value": ["Hello, how are you?"],
                "attributes.output.value": ["I'm doing well, thank you!"],
                "attributes.llm.input_messages": ['[{"role": "user", "content": "Hello"}]'],
                "attributes.llm.output_messages": ['[{"role": "assistant", "content": "Hi"}]'],
                "attributes.llm.model_name": ["claude-3-sonnet-20240229"],
                "attributes.llm.token_count.prompt": [10],
                "attributes.llm.token_count.completion": [15],
                "attributes.llm.token_count.total": [25],
            }
        )

        result = normalize_phoenix(phoenix_df)
        row = result.iloc[0].to_dict()

        # Verify against snapshot (excluding backend which differs)
        for key, expected_value in self.EXPECTED_LLM_SPAN.items():
            assert row[key] == expected_value, f"Mismatch in {key}: {row[key]} != {expected_value}"
        assert row["backend"] == "phoenix"

    def test_arize_snapshot_llm_span(self):
        """Given Arize LLM span, output matches expected snapshot."""
        arize_df = pd.DataFrame(
            {
                "context.span_id": ["span-001"],
                "context.trace_id": ["trace-001"],
                "parent_id": ["parent-001"],
                "name": ["LiteLLM Call"],
                "attributes.openinference.span.kind": ["LLM"],
                "start_time": [datetime(2025, 1, 15, 10, 30, 0)],
                "end_time": [datetime(2025, 1, 15, 10, 30, 5)],
                "status_code": ["OK"],
                "attributes.input.value": ["Hello, how are you?"],
                "attributes.output.value": ["I'm doing well, thank you!"],
                "attributes.llm.input_messages": ['[{"role": "user", "content": "Hello"}]'],
                "attributes.llm.output_messages": ['[{"role": "assistant", "content": "Hi"}]'],
                "attributes.llm.model_name": ["claude-3-sonnet-20240229"],
                "attributes.llm.token_count.prompt": [10],
                "attributes.llm.token_count.completion": [15],
                "attributes.llm.token_count.total": [25],
            }
        )

        result = normalize_arize(arize_df)
        row = result.iloc[0].to_dict()

        # Verify against snapshot (excluding backend which differs)
        for key, expected_value in self.EXPECTED_LLM_SPAN.items():
            assert row[key] == expected_value, f"Mismatch in {key}: {row[key]} != {expected_value}"
        assert row["backend"] == "arize"

    def test_phoenix_arize_produce_identical_output(self):
        """Given same span data, Phoenix and Arize produce identical unified output."""
        common_data = {
            "context.span_id": ["span-001"],
            "context.trace_id": ["trace-001"],
            "parent_id": ["parent-001"],
            "name": ["Tool Call"],
            "start_time": [datetime(2025, 1, 15, 10, 30, 0)],
            "end_time": [datetime(2025, 1, 15, 10, 30, 5)],
            "status_code": ["OK"],
            "attributes.input.value": ["Read file /path/to/file"],
            "attributes.output.value": ["File contents here"],
        }

        phoenix_df = pd.DataFrame({**common_data, "span_kind": ["TOOL"]})
        arize_df = pd.DataFrame({**common_data, "attributes.openinference.span.kind": ["TOOL"]})

        phoenix_result = normalize_phoenix(phoenix_df).iloc[0].to_dict()
        arize_result = normalize_arize(arize_df).iloc[0].to_dict()

        # All fields should match except backend and raw_attributes
        for key in UNIFIED_COLUMNS:
            if key in ("backend", "raw_attributes"):
                continue
            assert phoenix_result[key] == arize_result[key], (
                f"Snapshot mismatch in {key}: "
                f"phoenix={phoenix_result[key]}, arize={arize_result[key]}"
            )

    def test_snapshot_minimal_span(self):
        """Given minimal span data, both backends produce consistent minimal output."""
        minimal_phoenix = pd.DataFrame(
            {
                "context.span_id": ["span-minimal"],
                "context.trace_id": ["trace-minimal"],
            }
        )
        minimal_arize = pd.DataFrame(
            {
                "context.span_id": ["span-minimal"],
                "context.trace_id": ["trace-minimal"],
            }
        )

        phoenix_result = normalize_phoenix(minimal_phoenix).iloc[0].to_dict()
        arize_result = normalize_arize(minimal_arize).iloc[0].to_dict()

        # Both should have same None fields (raw_attributes is now populated with original row)
        expected_none_fields = [
            "parent_id", "span_kind", "end_time", "status_code",
            "input_value", "output_value", "input_messages", "output_messages",
            "llm_model_name", "llm_token_count_prompt",
            "llm_token_count_completion", "llm_token_count_total",
        ]

        for field in expected_none_fields:
            assert phoenix_result[field] is None, f"Phoenix {field} should be None"
            assert arize_result[field] is None, f"Arize {field} should be None"

        # raw_attributes should contain the original row data
        assert isinstance(phoenix_result["raw_attributes"], dict)
        assert isinstance(arize_result["raw_attributes"], dict)


class TestTimestampFormats:
    """Tests for various timestamp format handling."""

    def test_iso8601_string_input(self):
        """Given ISO-8601 string, preserves format."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "start_time": ["2025-01-15T10:30:00"],
            }
        )

        result = normalize_phoenix(df)

        assert "2025-01-15T10:30:00" in result.iloc[0]["start_time"]

    def test_iso8601_with_timezone(self):
        """Given ISO-8601 with timezone, handles correctly."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "start_time": ["2025-01-15T10:30:00Z"],
            }
        )

        result = normalize_phoenix(df)

        assert "2025-01-15" in result.iloc[0]["start_time"]
        assert "10:30:00" in result.iloc[0]["start_time"]

    def test_null_timestamp(self):
        """Given null timestamp, returns None."""
        df = pd.DataFrame(
            {
                "context.span_id": ["span1"],
                "context.trace_id": ["trace1"],
                "start_time": [None],
            }
        )

        result = normalize_phoenix(df)

        assert result.iloc[0]["start_time"] is None
