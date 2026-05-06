"""
Tests for the cluster module (Story 4.4).

Test Cases:
1. Cluster result structure
2. Preview functionality
3. Session text conversion
4. Edge cases
"""

from __future__ import annotations

import pytest

from dev_agent_lens.llm.cluster import (
    Cluster,
    ClusterResult,
    get_cluster_preview,
)


class TestClusterResult:
    """Test Case 1: Cluster result structure."""

    def test_cluster_to_dict(self):
        """Cluster converts to dictionary."""
        cluster = Cluster(
            cluster_id=0,
            label="Development Tasks",
            description="Tasks involving code development",
            session_ids=["sess1", "sess2", "sess3"],
        )

        result = cluster.to_dict()

        assert result["cluster_id"] == 0
        assert result["label"] == "Development Tasks"
        assert result["size"] == 3
        assert "sess1" in result["session_ids"]

    def test_cluster_result_to_dict(self):
        """ClusterResult converts to dictionary."""
        clusters = [
            Cluster(cluster_id=0, session_ids=["s1", "s2"]),
            Cluster(cluster_id=1, session_ids=["s3"]),
        ]
        result = ClusterResult(
            clusters=clusters,
            n_sessions=3,
            silhouette_score=0.75,
            outliers=["s4"],
        )

        data = result.to_dict()

        assert data["n_clusters"] == 2
        assert data["n_sessions"] == 3
        assert data["silhouette_score"] == 0.75
        assert len(data["clusters"]) == 2
        assert "s4" in data["outliers"]

    def test_get_cluster_by_id(self):
        """Gets cluster by ID."""
        clusters = [
            Cluster(cluster_id=0, label="A"),
            Cluster(cluster_id=1, label="B"),
        ]
        result = ClusterResult(clusters=clusters)

        found = result.get_cluster(1)

        assert found is not None
        assert found.label == "B"

    def test_returns_none_for_invalid_id(self):
        """Returns None for invalid cluster ID."""
        result = ClusterResult(clusters=[])

        found = result.get_cluster(999)

        assert found is None


class TestClusterPreview:
    """Test Case 2: Preview functionality."""

    def test_preview_structure(self):
        """Preview returns expected structure."""
        sessions = [
            {"session_id": "s1", "spans": [{"name": "test"}]},
            {"session_id": "s2", "spans": [{"name": "test"}]},
        ]

        preview = get_cluster_preview(sessions)

        assert "n_sessions" in preview
        assert "sample_texts" in preview
        assert "estimated_embedding_tokens" in preview
        assert "recommended_clusters" in preview

    def test_preview_counts_sessions(self):
        """Preview counts sessions correctly."""
        sessions = [
            {"session_id": f"s{i}", "spans": []} for i in range(5)
        ]

        preview = get_cluster_preview(sessions)

        assert preview["n_sessions"] == 5

    def test_preview_estimates_tokens(self):
        """Preview estimates embedding tokens."""
        sessions = [
            {"session_id": "s1", "spans": [{"name": "test"}]},
        ]

        preview = get_cluster_preview(sessions)

        assert preview["estimated_embedding_tokens"] > 0

    def test_preview_recommends_clusters(self):
        """Preview recommends cluster count."""
        sessions = [
            {"session_id": f"s{i}", "spans": []} for i in range(10)
        ]

        preview = get_cluster_preview(sessions)

        assert 2 <= preview["recommended_clusters"] <= 10


class TestSessionTextConversion:
    """Test Case 3: Session text conversion."""

    def test_sample_texts_generated(self):
        """Sample texts are generated for sessions."""
        sessions = [
            {
                "session_id": "s1",
                "spans": [
                    {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
                    {"name": "llm", "span_kind": "LLM", "llm_model_name": "claude-sonnet"},
                ],
            },
        ]

        preview = get_cluster_preview(sessions)

        assert len(preview["sample_texts"]) > 0
        assert "spans" in preview["sample_texts"][0].lower()

    def test_text_includes_tools(self):
        """Session text includes tool information."""
        sessions = [
            {
                "session_id": "s1",
                "spans": [
                    {"name": "Claude_Code_Tool_Read", "span_kind": "TOOL"},
                ],
            },
        ]

        preview = get_cluster_preview(sessions)

        # Text should mention tools
        assert "tool" in preview["sample_texts"][0].lower() or "read" in preview["sample_texts"][0].lower()

    def test_limits_sample_texts(self):
        """Limits sample texts to 3."""
        sessions = [
            {"session_id": f"s{i}", "spans": [{"name": "test"}]}
            for i in range(10)
        ]

        preview = get_cluster_preview(sessions)

        assert len(preview["sample_texts"]) <= 3


class TestClusterEdgeCases:
    """Test Case 4: Edge cases."""

    def test_single_session_preview(self):
        """Preview works with single session."""
        sessions = [
            {"session_id": "s1", "spans": [{"name": "test"}]},
        ]

        preview = get_cluster_preview(sessions)

        assert preview["n_sessions"] == 1
        assert preview["recommended_clusters"] >= 2  # Minimum

    def test_empty_sessions_preview(self):
        """Preview works with empty spans."""
        sessions = [
            {"session_id": "s1", "spans": []},
            {"session_id": "s2", "spans": []},
        ]

        preview = get_cluster_preview(sessions)

        assert preview["n_sessions"] == 2

    def test_cluster_size_calculation(self):
        """Cluster size calculated from session_ids."""
        cluster = Cluster(
            cluster_id=0,
            session_ids=["a", "b", "c", "d", "e"],
        )

        assert cluster.size == 5


class TestClusterMetadata:
    """Tests for cluster metadata."""

    def test_cluster_without_label(self):
        """Cluster can have empty label."""
        cluster = Cluster(
            cluster_id=0,
            session_ids=["s1"],
        )

        assert cluster.label == ""

    def test_cluster_with_centroid(self):
        """Cluster can store centroid vector."""
        cluster = Cluster(
            cluster_id=0,
            session_ids=["s1"],
            centroid=[0.1, 0.2, 0.3],
        )

        assert cluster.centroid == [0.1, 0.2, 0.3]

    def test_result_auto_counts_clusters(self):
        """Result auto-counts clusters."""
        clusters = [
            Cluster(cluster_id=i, session_ids=[f"s{i}"])
            for i in range(3)
        ]
        result = ClusterResult(clusters=clusters)

        assert result.n_clusters == 3
