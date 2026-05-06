"""
Subset Detection Module

Identifies when one session/span is fully contained within another.
This is critical for understanding storage efficiency and identifying
redundant data that could be deleted.

Key concepts:
- Complete subset: All content from session A appears in session B
- Partial overlap: Some content from A appears in B
- Coverage metrics: What percentage of sessions are complete vs partial
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubsetRelationship:
    """
    Represents a containment relationship between two sessions.

    A subset relationship exists when the content of one session
    is fully or partially contained in another session.
    """

    child_session_id: str
    parent_session_id: str
    containment_percentage: float  # 0.0 to 100.0
    is_complete_subset: bool
    child_span_count: int
    parent_span_count: int
    matched_chunks: int
    total_chunks: int
    recommendation: str  # "deletable", "keep_both", "review"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "child_session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "containment_percentage": round(self.containment_percentage, 2),
            "is_complete_subset": self.is_complete_subset,
            "child_span_count": self.child_span_count,
            "parent_span_count": self.parent_span_count,
            "matched_chunks": self.matched_chunks,
            "total_chunks": self.total_chunks,
            "recommendation": self.recommendation,
        }


@dataclass
class CoverageReport:
    """
    Coverage analysis for a collection of sessions.

    Tracks how many sessions are complete, partial, or subsets.
    """

    total_sessions: int = 0
    complete_sessions: int = 0  # Sessions that are not subsets of others
    subset_sessions: int = 0  # Sessions fully contained in another
    partial_sessions: int = 0  # Sessions with some overlap
    unique_sessions: int = 0  # Sessions with no overlap at all

    # Storage implications
    deletable_sessions: int = 0
    deletable_span_count: int = 0
    total_span_count: int = 0

    # Subset relationships
    relationships: list[SubsetRelationship] = field(default_factory=list)

    @property
    def coverage_percentage(self) -> float:
        """Percentage of sessions that are complete (not subsets)."""
        if self.total_sessions == 0:
            return 100.0
        return (self.complete_sessions / self.total_sessions) * 100

    @property
    def redundancy_percentage(self) -> float:
        """Percentage of sessions that could potentially be deleted."""
        if self.total_sessions == 0:
            return 0.0
        return (self.deletable_sessions / self.total_sessions) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "summary": {
                "total_sessions": self.total_sessions,
                "complete_sessions": self.complete_sessions,
                "subset_sessions": self.subset_sessions,
                "partial_sessions": self.partial_sessions,
                "unique_sessions": self.unique_sessions,
            },
            "coverage": {
                "coverage_percentage": round(self.coverage_percentage, 2),
                "redundancy_percentage": round(self.redundancy_percentage, 2),
            },
            "storage": {
                "deletable_sessions": self.deletable_sessions,
                "deletable_span_count": self.deletable_span_count,
                "total_span_count": self.total_span_count,
            },
            "relationships": [r.to_dict() for r in self.relationships],
        }


def _extract_content_chunks(span: dict[str, Any]) -> list[str]:
    """
    Extract content chunks from a span for comparison.

    Extracts text from:
    - input_value
    - output_value
    - llm_input_messages
    - llm_output_messages
    """
    chunks = []

    # Input value
    input_val = span.get("input_value") or ""
    if input_val and len(str(input_val).strip()) > 10:
        chunks.append(str(input_val).strip())

    # Output value
    output_val = span.get("output_value") or ""
    if output_val and len(str(output_val).strip()) > 10:
        chunks.append(str(output_val).strip())

    # LLM input messages
    input_msgs = span.get("llm_input_messages") or span.get("attributes.llm.input_messages") or []
    if isinstance(input_msgs, list):
        for msg in input_msgs:
            if isinstance(msg, dict):
                content = msg.get("message.content") or msg.get("content") or ""
                if content and len(str(content).strip()) > 10:
                    chunks.append(str(content).strip())

    # LLM output messages
    output_msgs = span.get("llm_output_messages") or span.get("attributes.llm.output_messages") or ""
    if output_msgs:
        if isinstance(output_msgs, list):
            for msg in output_msgs:
                if isinstance(msg, dict):
                    content = msg.get("message.content") or msg.get("content") or ""
                    if content and len(str(content).strip()) > 10:
                        chunks.append(str(content).strip())
        elif isinstance(output_msgs, str) and len(output_msgs.strip()) > 10:
            chunks.append(output_msgs.strip())

    return chunks


def _get_session_content(session: dict[str, Any]) -> tuple[list[str], set[str]]:
    """
    Get all content chunks and their hashes from a session.

    Returns:
        Tuple of (chunks list, chunk_hashes set)
    """
    spans = session.get("spans", [])
    all_chunks = []

    for span in spans:
        chunks = _extract_content_chunks(span)
        all_chunks.extend(chunks)

    # Create hashes for efficient comparison
    chunk_hashes = set()
    for chunk in all_chunks:
        # Hash normalized content
        normalized = chunk.lower().strip()
        chunk_hash = hashlib.md5(normalized.encode()).hexdigest()
        chunk_hashes.add(chunk_hash)

    return all_chunks, chunk_hashes


def _check_containment(
    child_session: dict[str, Any],
    parent_session: dict[str, Any],
) -> SubsetRelationship | None:
    """
    Check if child session is contained within parent session.

    Returns SubsetRelationship if there's significant overlap, None otherwise.
    """
    child_id = child_session.get("session_id", "unknown")
    parent_id = parent_session.get("session_id", "unknown")

    # Don't compare session to itself
    if child_id == parent_id:
        return None

    # Get content chunks
    child_chunks, child_hashes = _get_session_content(child_session)
    parent_chunks, parent_hashes = _get_session_content(parent_session)

    if not child_hashes:
        return None

    # Check how many child chunks are in parent
    matched_hashes = child_hashes.intersection(parent_hashes)
    matched_count = len(matched_hashes)
    total_count = len(child_hashes)

    containment_pct = (matched_count / total_count * 100) if total_count > 0 else 0

    # Only report significant relationships
    if containment_pct < 50:
        return None

    is_complete = containment_pct >= 95

    # Determine recommendation
    if is_complete:
        recommendation = "deletable"
    elif containment_pct >= 80:
        recommendation = "review"
    else:
        recommendation = "keep_both"

    child_spans = len(child_session.get("spans", []))
    parent_spans = len(parent_session.get("spans", []))

    return SubsetRelationship(
        child_session_id=child_id,
        parent_session_id=parent_id,
        containment_percentage=containment_pct,
        is_complete_subset=is_complete,
        child_span_count=child_spans,
        parent_span_count=parent_spans,
        matched_chunks=matched_count,
        total_chunks=total_count,
        recommendation=recommendation,
    )


def detect_subsets(sessions: list[dict[str, Any]]) -> list[SubsetRelationship]:
    """
    Detect subset relationships between sessions.

    Compares each session against all others to find containment
    relationships. A session is a subset if most of its content
    appears in another session.

    Args:
        sessions: List of session dictionaries with 'session_id' and 'spans'

    Returns:
        List of SubsetRelationship objects describing containment
    """
    relationships = []

    # Sort by span count (smaller first - more likely to be subsets)
    sorted_sessions = sorted(
        sessions,
        key=lambda s: len(s.get("spans", [])),
    )

    # Compare each session to larger sessions
    for i, child in enumerate(sorted_sessions):
        for j, parent in enumerate(sorted_sessions):
            # Only compare to sessions with more spans
            if i >= j:
                continue

            child_spans = len(child.get("spans", []))
            parent_spans = len(parent.get("spans", []))

            # Skip if parent is smaller (can't contain child)
            if parent_spans < child_spans:
                continue

            relationship = _check_containment(child, parent)
            if relationship:
                relationships.append(relationship)

    return relationships


def analyze_coverage(sessions: list[dict[str, Any]]) -> CoverageReport:
    """
    Analyze coverage and redundancy across sessions.

    Identifies:
    - Complete sessions (not subsets of others)
    - Subset sessions (fully contained in another)
    - Partial sessions (some overlap with others)
    - Unique sessions (no overlap at all)

    Args:
        sessions: List of session dictionaries

    Returns:
        CoverageReport with coverage metrics
    """
    report = CoverageReport(
        total_sessions=len(sessions),
        total_span_count=sum(len(s.get("spans", [])) for s in sessions),
    )

    if not sessions:
        return report

    # Detect all subset relationships
    relationships = detect_subsets(sessions)
    report.relationships = relationships

    # Track session status
    session_status: dict[str, str] = {}  # session_id -> status

    # Initialize all as complete
    for session in sessions:
        sid = session.get("session_id", "unknown")
        session_status[sid] = "complete"

    # Mark subsets
    for rel in relationships:
        if rel.is_complete_subset:
            session_status[rel.child_session_id] = "subset"
            report.deletable_span_count += rel.child_span_count
        else:
            # Only mark as partial if not already a subset
            if session_status.get(rel.child_session_id) != "subset":
                session_status[rel.child_session_id] = "partial"

    # Count by status
    for status in session_status.values():
        if status == "complete":
            report.complete_sessions += 1
        elif status == "subset":
            report.subset_sessions += 1
            report.deletable_sessions += 1
        elif status == "partial":
            report.partial_sessions += 1

    # Count unique (complete with no relationships)
    child_ids = {r.child_session_id for r in relationships}
    parent_ids = {r.parent_session_id for r in relationships}
    related_ids = child_ids.union(parent_ids)

    for session in sessions:
        sid = session.get("session_id", "unknown")
        if sid not in related_ids:
            report.unique_sessions += 1

    return report


def get_deletable_sessions(sessions: list[dict[str, Any]]) -> list[str]:
    """
    Get list of session IDs that can be safely deleted.

    These are sessions that are complete subsets of other sessions
    and contain no unique content.

    Args:
        sessions: List of session dictionaries

    Returns:
        List of session IDs that are safe to delete
    """
    relationships = detect_subsets(sessions)

    deletable = []
    for rel in relationships:
        if rel.is_complete_subset and rel.recommendation == "deletable":
            deletable.append(rel.child_session_id)

    return deletable
