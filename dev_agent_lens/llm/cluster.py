"""
Session Clustering Module (Story 4.4)

Clusters sessions by behavioral similarity using embeddings.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dev_agent_lens.analysis.sessions import session_metrics
from dev_agent_lens.llm.batch import BatchConfig, format_batch
from dev_agent_lens.llm.prompts import PromptType, load_prompt, render_prompt
from dev_agent_lens.llm.router import (
    AnalysisType,
    NoLLMConfigError,
    get_embeddings,
    get_llm_config,
    route_request,
)


@dataclass
class Cluster:
    """A cluster of similar sessions.

    Attributes:
        cluster_id: Cluster identifier
        label: Human-readable cluster label
        description: Description of cluster characteristics
        session_ids: List of session IDs in this cluster
        centroid: Centroid vector (if available)
        size: Number of sessions in cluster
    """

    cluster_id: int
    label: str = ""
    description: str = ""
    session_ids: list[str] = field(default_factory=list)
    centroid: list[float] | None = None
    size: int = 0

    def __post_init__(self):
        if self.size == 0:
            self.size = len(self.session_ids)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "cluster_id": self.cluster_id,
            "label": self.label,
            "description": self.description,
            "session_ids": self.session_ids,
            "size": self.size,
        }


@dataclass
class ClusterResult:
    """Result of clustering operation.

    Attributes:
        clusters: List of clusters
        n_clusters: Number of clusters
        n_sessions: Total sessions clustered
        silhouette_score: Clustering quality score (-1 to 1)
        outliers: Sessions that don't fit well
        model_used: Model used for embeddings
    """

    clusters: list[Cluster] = field(default_factory=list)
    n_clusters: int = 0
    n_sessions: int = 0
    silhouette_score: float | None = None
    outliers: list[str] = field(default_factory=list)
    model_used: str | None = None

    def __post_init__(self):
        if self.n_clusters == 0:
            self.n_clusters = len(self.clusters)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "clusters": [c.to_dict() for c in self.clusters],
            "n_clusters": self.n_clusters,
            "n_sessions": self.n_sessions,
            "silhouette_score": self.silhouette_score,
            "outliers": self.outliers,
            "model_used": self.model_used,
        }

    def get_cluster(self, cluster_id: int) -> Cluster | None:
        """Get cluster by ID."""
        for cluster in self.clusters:
            if cluster.cluster_id == cluster_id:
                return cluster
        return None


def _session_to_text(session: dict[str, Any]) -> str:
    """Convert a session to text for embedding.

    Args:
        session: Session dictionary

    Returns:
        Text representation for embedding
    """
    session_id = session.get("session_id", "unknown")
    spans = session.get("spans", [])

    if not spans:
        return f"Session {session_id}: Empty session"

    # Compute metrics
    metrics = session_metrics(spans, session_id=session_id)

    # Get tool names
    tools = set()
    for span in spans:
        name = span.get("name", "")
        if "Tool" in name:
            # Extract tool name
            tool = name.replace("Claude_Code_Tool_", "").replace("Claude_Code_", "")
            tools.add(tool)

    # Get models used
    models = set()
    for span in spans:
        model = span.get("llm_model_name")
        if model:
            models.add(model)

    # Get error status
    errors = [s for s in spans if s.get("status_code") == "ERROR"]

    # Build text representation
    parts = [
        f"Session with {len(spans)} spans",
        f"Duration: {metrics.duration_minutes:.1f} minutes",
        f"Turns: {metrics.turn_count}",
        f"Tools used: {', '.join(sorted(tools)) if tools else 'none'}",
        f"Models: {', '.join(sorted(models)) if models else 'unknown'}",
        f"Errors: {len(errors)}",
    ]

    # Add sample operations
    tool_spans = [s for s in spans if s.get("span_kind") == "TOOL"][:5]
    if tool_spans:
        ops = [s.get("name", "unknown") for s in tool_spans]
        parts.append(f"Sample operations: {', '.join(ops)}")

    return ". ".join(parts)


async def cluster_sessions(
    sessions: list[dict[str, Any]],
    n_clusters: int | None = None,
    min_cluster_size: int = 2,
    model: str | None = None,
    generate_labels: bool = True,
) -> ClusterResult:
    """Cluster sessions by behavioral similarity.

    Args:
        sessions: List of session dictionaries
        n_clusters: Number of clusters (auto-detected if None)
        min_cluster_size: Minimum sessions per cluster
        model: Optional embedding model override
        generate_labels: Whether to generate cluster labels with LLM

    Returns:
        ClusterResult with cluster assignments

    Raises:
        NoLLMConfigError: If OpenAI is not configured
        ValueError: If not enough sessions for clustering
    """
    if len(sessions) < 2:
        raise ValueError("Need at least 2 sessions for clustering")

    # Convert sessions to text
    texts = [_session_to_text(s) for s in sessions]
    session_ids = [s.get("session_id", f"session_{i}") for i, s in enumerate(sessions)]

    # Get embeddings
    try:
        embeddings = await get_embeddings(texts)
    except ImportError as e:
        raise NoLLMConfigError(
            "OpenAI is required for clustering. Install with: pip install openai"
        ) from e

    # Perform clustering
    try:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        import numpy as np
    except ImportError:
        raise ImportError(
            "scikit-learn is required for clustering. Install with: pip install scikit-learn"
        )

    embeddings_array = np.array(embeddings)

    # Determine optimal number of clusters if not specified
    if n_clusters is None:
        # Use elbow method or reasonable default
        max_clusters = min(10, len(sessions) // min_cluster_size)
        n_clusters = max(2, min(5, max_clusters))

    # Ensure n_clusters is valid
    n_clusters = min(n_clusters, len(sessions))

    # Fit KMeans
    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    labels = kmeans.fit_predict(embeddings_array)

    # Calculate silhouette score
    sil_score = None
    if n_clusters > 1 and n_clusters < len(sessions):
        sil_score = float(silhouette_score(embeddings_array, labels))

    # Build clusters
    clusters_map: dict[int, list[str]] = {}
    for session_id, label in zip(session_ids, labels):
        if label not in clusters_map:
            clusters_map[label] = []
        clusters_map[label].append(session_id)

    # Identify outliers (clusters below min size)
    outliers = []
    valid_clusters = []
    for cluster_id, session_list in clusters_map.items():
        if len(session_list) < min_cluster_size:
            outliers.extend(session_list)
        else:
            valid_clusters.append(
                Cluster(
                    cluster_id=cluster_id,
                    session_ids=session_list,
                    centroid=kmeans.cluster_centers_[cluster_id].tolist(),
                )
            )

    # Generate labels if requested
    if generate_labels and valid_clusters:
        try:
            valid_clusters = await _generate_cluster_labels(
                valid_clusters, sessions, model
            )
        except NoLLMConfigError:
            # Fall back to generic labels
            for i, cluster in enumerate(valid_clusters):
                cluster.label = f"Cluster {i + 1}"
                cluster.description = f"Contains {cluster.size} sessions"

    return ClusterResult(
        clusters=valid_clusters,
        n_clusters=len(valid_clusters),
        n_sessions=len(sessions),
        silhouette_score=sil_score,
        outliers=outliers,
        model_used=model or "text-embedding-3-small",
    )


async def _generate_cluster_labels(
    clusters: list[Cluster],
    sessions: list[dict[str, Any]],
    model: str | None = None,
) -> list[Cluster]:
    """Generate descriptive labels for clusters using LLM.

    Args:
        clusters: List of clusters to label
        sessions: Original sessions for reference
        model: Optional model override

    Returns:
        Clusters with labels filled in
    """
    # Build session lookup
    session_lookup = {s.get("session_id"): s for s in sessions}

    # Load prompt
    template = load_prompt(PromptType.CLUSTER)

    # Build summaries for each cluster
    cluster_summaries = []
    for cluster in clusters:
        sample_sessions = cluster.session_ids[:3]  # Sample up to 3
        summaries = []
        for sid in sample_sessions:
            session = session_lookup.get(sid)
            if session:
                summaries.append(_session_to_text(session))
        cluster_summaries.append(
            f"Cluster {cluster.cluster_id} ({cluster.size} sessions):\n" +
            "\n".join(f"  - {s}" for s in summaries)
        )

    # Prepare variables
    variables = {
        "session_count": sum(c.size for c in clusters),
        "session_summaries": "\n\n".join(cluster_summaries),
    }

    # Render prompt
    prompt = render_prompt(template, variables)

    # Get LLM config
    llm_config = get_llm_config(
        AnalysisType.SUMMARIZE,
        model=model,
    )

    # Generate labels
    response = await route_request(
        prompt=prompt,
        config=llm_config,
        system_prompt="You are a trace analysis assistant. Analyze session clusters and provide descriptive labels.",
    )

    # Parse response and assign labels
    # Simple parsing - look for cluster IDs and labels
    lines = response.content.split("\n")
    for cluster in clusters:
        cluster.label = f"Cluster {cluster.cluster_id + 1}"
        cluster.description = f"Contains {cluster.size} similar sessions"

        # Try to find matching label in response
        for line in lines:
            if f"cluster {cluster.cluster_id}" in line.lower():
                # Extract label from line
                if ":" in line:
                    cluster.label = line.split(":", 1)[1].strip()[:50]
                    break

    return clusters


def cluster_sessions_sync(
    sessions: list[dict[str, Any]],
    n_clusters: int | None = None,
    min_cluster_size: int = 2,
    model: str | None = None,
    generate_labels: bool = True,
) -> ClusterResult:
    """Synchronous wrapper for cluster_sessions.

    Args:
        sessions: List of session dictionaries
        n_clusters: Number of clusters (auto-detected if None)
        min_cluster_size: Minimum sessions per cluster
        model: Optional embedding model override
        generate_labels: Whether to generate cluster labels

    Returns:
        ClusterResult with cluster assignments
    """
    return asyncio.run(
        cluster_sessions(
            sessions,
            n_clusters=n_clusters,
            min_cluster_size=min_cluster_size,
            model=model,
            generate_labels=generate_labels,
        )
    )


def get_cluster_preview(
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Get a preview of clustering without making API calls.

    Args:
        sessions: List of session dictionaries

    Returns:
        Preview information
    """
    texts = [_session_to_text(s) for s in sessions]

    return {
        "n_sessions": len(sessions),
        "sample_texts": texts[:3],
        "estimated_embedding_tokens": sum(len(t) // 4 for t in texts),
        "recommended_clusters": max(2, min(5, len(sessions) // 2)),
    }
