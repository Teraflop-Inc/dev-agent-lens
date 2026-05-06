"""
Tests for annotation schema and normalization.

Tests cover:
- UnifiedAnnotation schema structure
- Phoenix annotation normalization
- Edge cases and error handling
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from dev_agent_lens.core.schema import (
    ANNOTATION_COLUMNS,
    UnifiedAnnotation,
    normalize_phoenix_annotations,
)


class TestAnnotationSchema:
    """Tests for the UnifiedAnnotation schema."""

    def test_annotation_columns_defined(self):
        """ANNOTATION_COLUMNS should be a non-empty list."""
        assert isinstance(ANNOTATION_COLUMNS, list)
        assert len(ANNOTATION_COLUMNS) > 0

    def test_expected_columns_present(self):
        """Expected columns should be in ANNOTATION_COLUMNS."""
        expected = [
            "annotation_id",
            "span_id",
            "name",
            "annotator_kind",
            "label",
            "score",
            "explanation",
            "metadata",
            "created_at",
            "updated_at",
            "source",
            "user_id",
            "backend",
        ]
        for col in expected:
            assert col in ANNOTATION_COLUMNS, f"Missing column: {col}"

    def test_unified_annotation_is_typeddict(self):
        """UnifiedAnnotation should be a TypedDict."""
        from typing import TypedDict

        assert issubclass(UnifiedAnnotation, dict)


class TestNormalizePhoenixAnnotations:
    """Tests for Phoenix annotation normalization."""

    def test_empty_dataframe_returns_empty(self):
        """Given empty DataFrame, returns empty DataFrame with correct columns."""
        df = pd.DataFrame()
        result = normalize_phoenix_annotations(df)

        assert result.empty
        assert list(result.columns) == ANNOTATION_COLUMNS

    def test_basic_annotation_normalization(self):
        """Given valid Phoenix annotation data, returns normalized DataFrame."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "helpfulness",
                "annotator_kind": "HUMAN",
                "result": {"label": "good", "score": 0.9, "explanation": "Very helpful"},
                "metadata": {"reviewer": "user1"},
                "created_at": "2025-01-15T10:30:00",
                "updated_at": "2025-01-15T10:30:00",
                "source": "APP",
                "user_id": "user123",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["annotation_id"] == "ann-001"
        assert row["span_id"] == "span-001"
        assert row["name"] == "helpfulness"
        assert row["annotator_kind"] == "HUMAN"
        assert row["label"] == "good"
        assert row["score"] == 0.9
        assert row["explanation"] == "Very helpful"
        assert row["source"] == "APP"
        assert row["user_id"] == "user123"
        assert row["backend"] == "phoenix"

    def test_multiple_annotations(self):
        """Given multiple annotations, returns all normalized."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "helpfulness",
                "annotator_kind": "HUMAN",
                "result": {"label": "good", "score": 0.9},
                "created_at": "2025-01-15T10:30:00",
            },
            {
                "id": "ann-002",
                "span_id": "span-001",
                "name": "accuracy",
                "annotator_kind": "LLM",
                "result": {"label": "accurate", "score": 0.95},
                "created_at": "2025-01-15T10:31:00",
            },
            {
                "id": "ann-003",
                "span_id": "span-002",
                "name": "toxicity",
                "annotator_kind": "CODE",
                "result": {"score": 0.05},
                "created_at": "2025-01-15T10:32:00",
            },
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert len(result) == 3
        assert set(result["name"]) == {"helpfulness", "accuracy", "toxicity"}
        assert set(result["annotator_kind"]) == {"HUMAN", "LLM", "CODE"}

    def test_missing_optional_fields(self):
        """Given annotation with missing optional fields, handles gracefully."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "quick_note",
                "annotator_kind": "HUMAN",
                "created_at": "2025-01-15T10:30:00",
                # No result, metadata, updated_at, source, user_id
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["annotation_id"] == "ann-001"
        assert row["label"] is None
        assert row["score"] is None
        assert row["explanation"] is None
        assert row["metadata"] is None

    def test_flat_result_columns(self):
        """Given flat result columns instead of nested dict, handles correctly."""
        # Some Phoenix versions may return flat columns
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "quality",
                "annotator_kind": "HUMAN",
                "label": "excellent",
                "score": 1.0,
                "explanation": "Perfect response",
                "created_at": "2025-01-15T10:30:00",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["label"] == "excellent"
        assert row["score"] == 1.0
        assert row["explanation"] == "Perfect response"

    def test_metadata_dict_converted_to_json(self):
        """Given metadata as dict, converts to JSON string."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "review",
                "annotator_kind": "HUMAN",
                "metadata": {"reviewer": "user1", "timestamp": "2025-01-15"},
                "created_at": "2025-01-15T10:30:00",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        row = result.iloc[0]
        assert row["metadata"] is not None
        parsed = json.loads(row["metadata"])
        assert parsed["reviewer"] == "user1"

    def test_all_annotator_kinds(self):
        """Given different annotator kinds, preserves correctly."""
        phoenix_df = pd.DataFrame([
            {
                "id": f"ann-{i}",
                "span_id": "span-001",
                "name": "test",
                "annotator_kind": kind,
                "created_at": "2025-01-15T10:30:00",
            }
            for i, kind in enumerate(["HUMAN", "LLM", "CODE"])
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert len(result) == 3
        assert set(result["annotator_kind"]) == {"HUMAN", "LLM", "CODE"}

    def test_score_as_float(self):
        """Given score values, converts to float."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "confidence",
                "annotator_kind": "LLM",
                "result": {"score": 0.95},
                "created_at": "2025-01-15T10:30:00",
            },
            {
                "id": "ann-002",
                "span_id": "span-002",
                "name": "binary",
                "annotator_kind": "CODE",
                "result": {"score": 1},  # Integer
                "created_at": "2025-01-15T10:30:00",
            },
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert result.iloc[0]["score"] == 0.95
        assert result.iloc[1]["score"] == 1.0
        assert isinstance(result.iloc[0]["score"], float)

    def test_timestamp_normalization(self):
        """Given various timestamp formats, normalizes to ISO-8601."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "test",
                "annotator_kind": "HUMAN",
                "created_at": "2025-01-15T10:30:00Z",
                "updated_at": pd.Timestamp("2025-01-15 10:30:00"),
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        row = result.iloc[0]
        assert "2025-01-15" in row["created_at"]
        assert "10:30" in row["created_at"]

    def test_column_order_matches_schema(self):
        """Result columns should match ANNOTATION_COLUMNS order."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "test",
                "annotator_kind": "HUMAN",
                "created_at": "2025-01-15T10:30:00",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert list(result.columns) == ANNOTATION_COLUMNS


class TestAnnotationEdgeCases:
    """Edge case tests for annotation handling."""

    def test_null_values_handled(self):
        """Given null/NaN values, handles gracefully."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "test",
                "annotator_kind": None,
                "result": None,
                "metadata": None,
                "created_at": "2025-01-15T10:30:00",
                "updated_at": None,
                "source": None,
                "user_id": None,
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        assert len(result) == 1
        row = result.iloc[0]
        assert row["annotator_kind"] is None
        assert row["label"] is None
        assert row["score"] is None

    def test_empty_result_dict(self):
        """Given empty result dict, returns None for result fields."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "placeholder",
                "annotator_kind": "HUMAN",
                "result": {},
                "created_at": "2025-01-15T10:30:00",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        row = result.iloc[0]
        assert row["label"] is None
        assert row["score"] is None
        assert row["explanation"] is None

    def test_special_characters_in_text_fields(self):
        """Given special characters, preserves them correctly."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "review",
                "annotator_kind": "HUMAN",
                "result": {
                    "label": "good",
                    "explanation": "Great response! 🎉 Contains: \"quotes\" & <tags>",
                },
                "created_at": "2025-01-15T10:30:00",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        row = result.iloc[0]
        assert "🎉" in row["explanation"]
        assert "\"quotes\"" in row["explanation"]
        assert "<tags>" in row["explanation"]

    def test_score_zero_preserved(self):
        """Given score of 0, preserves it (not treated as None)."""
        phoenix_df = pd.DataFrame([
            {
                "id": "ann-001",
                "span_id": "span-001",
                "name": "toxicity",
                "annotator_kind": "CODE",
                "result": {"score": 0.0},
                "created_at": "2025-01-15T10:30:00",
            }
        ])

        result = normalize_phoenix_annotations(phoenix_df)

        row = result.iloc[0]
        assert row["score"] == 0.0
        assert row["score"] is not None
