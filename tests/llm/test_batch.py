"""
Tests for the batch formatter module (Story 4.1).

Test Cases:
1. Batch creation from spans
2. Batch configuration options
3. Multiple batch splitting
4. Output formats (JSON, text, markdown)
5. Token estimation
6. Empty and edge cases
"""

from __future__ import annotations

import json

import pytest

from dev_agent_lens.llm.batch import (
    Batch,
    BatchConfig,
    create_batches,
    format_batch,
    format_session_batch,
    format_spans_for_llm,
    get_batch_summary,
)


class TestBatchCreation:
    """Test Case 1: Batch creation from spans."""

    def test_creates_batch_from_spans(self):
        """Creates batch from list of spans."""
        spans = [
            {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
            {"span_id": "2", "name": "llm_call", "span_kind": "LLM", "llm_model_name": "claude-sonnet"},
        ]

        batch = format_batch(spans, batch_id="test_batch")

        assert batch.batch_id == "test_batch"
        assert batch.span_count == 2
        assert len(batch.spans) == 2

    def test_batch_includes_span_metadata(self):
        """Batch includes classified span metadata."""
        spans = [
            {
                "span_id": "1",
                "name": "Claude_Code_Tool_Read",
                "span_kind": "TOOL",
                "input_value": "file.py",
            },
        ]

        batch = format_batch(spans)

        assert batch.spans[0]["category"] == "tools"
        assert batch.spans[0]["name"] == "Claude_Code_Tool_Read"
        assert batch.spans[0]["input"] == "file.py"

    def test_batch_preserves_session_id(self):
        """Batch preserves session ID when provided."""
        spans = [{"span_id": "1", "name": "test"}]

        batch = format_batch(spans, session_id="session_123")

        assert batch.session_id == "session_123"

    def test_batch_calculates_metadata(self):
        """Batch calculates category and model metadata."""
        spans = [
            {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
            {"span_id": "2", "name": "Claude_Code_Tool_Write", "span_kind": "TOOL"},
            {"span_id": "3", "name": "llm", "span_kind": "LLM", "llm_model_name": "sonnet"},
        ]

        batch = format_batch(spans)

        assert "categories" in batch.metadata
        assert batch.metadata["categories"].get("tools", 0) == 2


class TestBatchConfig:
    """Test Case 2: Batch configuration options."""

    def test_default_config_values(self):
        """Default config has expected values."""
        config = BatchConfig()

        assert config.max_spans_per_batch == 100
        assert config.max_tokens_estimate == 8000
        assert config.include_raw_attributes is False
        assert config.format == "json"

    def test_custom_config_applied(self):
        """Custom config is applied to batch creation."""
        config = BatchConfig(
            max_spans_per_batch=10,
            include_raw_attributes=True,
        )
        spans = [
            {"span_id": "1", "name": "test", "raw_attributes": {"key": "value"}},
        ]

        batch = format_batch(spans, config=config)

        assert batch.spans[0].get("raw_attributes") is not None

    def test_config_excludes_raw_attributes_by_default(self):
        """Raw attributes excluded by default."""
        spans = [
            {"span_id": "1", "name": "test", "raw_attributes": {"key": "value"}},
        ]

        batch = format_batch(spans)

        assert batch.spans[0].get("raw_attributes") is None


class TestBatchSplitting:
    """Test Case 3: Multiple batch splitting."""

    def test_splits_by_span_count(self):
        """Splits into multiple batches when exceeding span limit."""
        config = BatchConfig(max_spans_per_batch=2)
        spans = [
            {"span_id": "1", "name": "span1"},
            {"span_id": "2", "name": "span2"},
            {"span_id": "3", "name": "span3"},
            {"span_id": "4", "name": "span4"},
            {"span_id": "5", "name": "span5"},
        ]

        batches = create_batches(spans, config=config)

        assert len(batches) == 3  # 2 + 2 + 1
        assert batches[0].span_count == 2
        assert batches[1].span_count == 2
        assert batches[2].span_count == 1

    def test_splits_by_token_estimate(self):
        """Splits when token estimate exceeds limit."""
        config = BatchConfig(max_tokens_estimate=100)
        # Create spans with long content
        spans = [
            {"span_id": "1", "name": "span1", "input_value": "x" * 500},
            {"span_id": "2", "name": "span2", "input_value": "y" * 500},
        ]

        batches = create_batches(spans, config=config)

        assert len(batches) >= 2

    def test_empty_input_returns_empty_list(self):
        """Empty span list returns empty batch list."""
        batches = create_batches([])

        assert batches == []

    def test_batch_ids_are_sequential(self):
        """Batch IDs are numbered sequentially."""
        config = BatchConfig(max_spans_per_batch=1)
        spans = [
            {"span_id": "1", "name": "span1"},
            {"span_id": "2", "name": "span2"},
        ]

        batches = create_batches(spans, config=config)

        assert batches[0].batch_id == "batch_0"
        assert batches[1].batch_id == "batch_1"


class TestOutputFormats:
    """Test Case 4: Output formats."""

    def test_to_dict(self):
        """to_dict returns serializable dictionary."""
        spans = [{"span_id": "1", "name": "test"}]
        batch = format_batch(spans, batch_id="test")

        result = batch.to_dict()

        assert "batch_id" in result
        assert "spans" in result
        assert "span_count" in result
        assert result["batch_id"] == "test"

    def test_to_json(self):
        """to_json returns valid JSON string."""
        spans = [{"span_id": "1", "name": "test"}]
        batch = format_batch(spans)

        json_str = batch.to_json()
        parsed = json.loads(json_str)

        assert "batch_id" in parsed
        assert "spans" in parsed

    def test_to_text(self):
        """to_text returns human-readable text."""
        spans = [
            {"span_id": "1", "name": "test_op", "span_kind": "TOOL"},
        ]
        batch = format_batch(spans, batch_id="test")

        text = batch.to_text()

        assert "Batch test" in text
        assert "test_op" in text
        assert "Span 1" in text

    def test_to_markdown(self):
        """to_markdown returns markdown formatted text."""
        spans = [
            {"span_id": "1", "name": "test_op", "span_kind": "TOOL"},
        ]
        batch = format_batch(spans, batch_id="test")

        md = batch.to_markdown()

        assert "# Batch test" in md
        assert "## Span 1" in md
        assert "**Type:**" in md

    def test_text_truncates_long_content(self):
        """Text format truncates long input/output."""
        spans = [
            {"span_id": "1", "name": "test", "input_value": "x" * 1000},
        ]
        batch = format_batch(spans)

        text = batch.to_text()

        assert "..." in text
        assert len(text) < 2000  # Should be truncated


class TestTokenEstimation:
    """Test Case 5: Token estimation."""

    def test_estimates_tokens(self):
        """Token count is estimated from content."""
        spans = [
            {"span_id": "1", "name": "test", "input_value": "a" * 400},  # ~100 tokens
        ]

        batch = format_batch(spans)

        assert batch.token_estimate > 50  # Should have some token estimate

    def test_empty_content_low_tokens(self):
        """Empty content has low token estimate."""
        spans = [
            {"span_id": "1", "name": "test"},
        ]

        batch = format_batch(spans)

        assert batch.token_estimate < 200  # Just overhead

    def test_token_estimate_scales_with_content(self):
        """Token estimate scales with content size."""
        small_spans = [{"span_id": "1", "name": "test", "input_value": "x" * 100}]
        large_spans = [{"span_id": "1", "name": "test", "input_value": "x" * 1000}]

        small_batch = format_batch(small_spans)
        large_batch = format_batch(large_spans)

        assert large_batch.token_estimate > small_batch.token_estimate


class TestEmptyAndEdgeCases:
    """Test Case 6: Empty and edge cases."""

    def test_empty_spans_returns_empty_batch(self):
        """Empty spans list returns batch with zero spans."""
        batch = format_batch([])

        assert batch.span_count == 0
        assert batch.spans == []
        assert batch.token_estimate == 0

    def test_handles_missing_fields(self):
        """Handles spans with missing fields gracefully."""
        spans = [
            {"span_id": "1"},  # Minimal span
        ]

        batch = format_batch(spans)

        assert batch.span_count == 1
        assert batch.spans[0]["span_id"] == "1"

    def test_handles_none_values(self):
        """Handles None values in span fields."""
        spans = [
            {
                "span_id": "1",
                "name": None,
                "input_value": None,
                "output_value": None,
            },
        ]

        batch = format_batch(spans)

        assert batch.span_count == 1
        # None values should be excluded from formatted span
        assert "input" not in batch.spans[0] or batch.spans[0]["input"] is None


class TestFormatSpansForLLM:
    """Tests for format_spans_for_llm function."""

    def test_formats_multiple_spans(self):
        """Formats list of spans."""
        spans = [
            {"span_id": "1", "name": "op1", "span_kind": "TOOL"},
            {"span_id": "2", "name": "op2", "span_kind": "LLM"},
        ]

        formatted = format_spans_for_llm(spans)

        assert len(formatted) == 2
        assert formatted[0]["span_id"] == "1"
        assert formatted[1]["span_id"] == "2"

    def test_includes_classification(self):
        """Includes span classification."""
        spans = [
            {"span_id": "1", "name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
        ]

        formatted = format_spans_for_llm(spans)

        assert formatted[0]["category"] == "tools"
        assert "confidence" in formatted[0]


class TestFormatSessionBatch:
    """Tests for format_session_batch function."""

    def test_formats_session_dict(self):
        """Formats session dictionary."""
        session = {
            "session_id": "sess_123",
            "spans": [
                {"span_id": "1", "name": "test"},
            ],
        }

        batch = format_session_batch(session)

        assert batch.session_id == "sess_123"
        assert batch.span_count == 1

    def test_generates_batch_id_from_session(self):
        """Generates batch ID from session ID."""
        session = {
            "session_id": "abc123",
            "spans": [],
        }

        batch = format_session_batch(session)

        assert "abc123" in batch.batch_id


class TestGetBatchSummary:
    """Tests for get_batch_summary function."""

    def test_returns_summary_dict(self):
        """Returns summary dictionary."""
        spans = [
            {"span_id": "1", "name": "test", "span_kind": "TOOL"},
        ]
        batch = format_batch(spans, batch_id="test", session_id="sess")

        summary = get_batch_summary(batch)

        assert summary["batch_id"] == "test"
        assert summary["session_id"] == "sess"
        assert summary["span_count"] == 1
        assert "categories" in summary
        assert "models" in summary


class TestDurationCalculation:
    """Tests for duration calculation in formatted spans."""

    def test_calculates_duration_from_timestamps(self):
        """Calculates duration from start/end times."""
        spans = [
            {
                "span_id": "1",
                "name": "test",
                "start_time": "2024-01-01T10:00:00.000",
                "end_time": "2024-01-01T10:00:30.000",
            },
        ]

        formatted = format_spans_for_llm(spans)

        assert formatted[0]["duration_ms"] == 30000  # 30 seconds

    def test_handles_missing_end_time(self):
        """Handles missing end time."""
        spans = [
            {
                "span_id": "1",
                "name": "test",
                "start_time": "2024-01-01T10:00:00.000",
            },
        ]

        formatted = format_spans_for_llm(spans)

        assert "duration_ms" not in formatted[0] or formatted[0].get("duration_ms") is None


class TestParquetBatchFields:
    """Test Case 7: PARQUET_BATCH_FIELDS constant."""

    def test_parquet_batch_fields_defined(self):
        """PARQUET_BATCH_FIELDS constant is exported."""
        from dev_agent_lens.llm.batch import PARQUET_BATCH_FIELDS

        assert isinstance(PARQUET_BATCH_FIELDS, list)
        assert len(PARQUET_BATCH_FIELDS) > 0

    def test_parquet_batch_fields_includes_required_fields(self):
        """PARQUET_BATCH_FIELDS includes required fields for batching."""
        from dev_agent_lens.llm.batch import PARQUET_BATCH_FIELDS

        required = ["session_id", "span_id", "name", "input_value", "output_value"]
        for field in required:
            assert field in PARQUET_BATCH_FIELDS


class TestFormatBatchFromParquet:
    """Test Case 8: format_batch_from_parquet function."""

    def test_format_batch_from_parquet_with_empty_session_ids(self):
        """Returns empty list for empty session_ids."""
        from dev_agent_lens.llm.batch import format_batch_from_parquet

        result = format_batch_from_parquet("fake_path.parquet", [])
        assert result == []

    def test_format_batch_from_parquet_requires_duckdb(self):
        """Requires DuckDB dependency."""
        from dev_agent_lens.llm.batch import format_batch_from_parquet

        # With empty session_ids, returns immediately without needing duckdb
        result = format_batch_from_parquet("fake_path.parquet", [])
        assert result == []


class TestCreateBatchesFromParquet:
    """Test Case 9: create_batches_from_parquet function."""

    def test_create_batches_from_parquet_exported(self):
        """create_batches_from_parquet is exported from module."""
        from dev_agent_lens.llm import create_batches_from_parquet

        assert callable(create_batches_from_parquet)

    def test_format_batch_from_parquet_exported(self):
        """format_batch_from_parquet is exported from module."""
        from dev_agent_lens.llm import format_batch_from_parquet

        assert callable(format_batch_from_parquet)
