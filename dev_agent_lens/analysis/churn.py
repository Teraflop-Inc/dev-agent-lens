"""
Code Churn Detector Module

Detects code churn patterns in trace spans - repeated edits to the same files.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from dev_agent_lens.analysis.classify import SpanCategory, classify_span


@dataclass
class FileChurn:
    """Churn metrics for a single file."""

    file_path: str
    edit_count: int = 0
    write_count: int = 0
    total_operations: int = 0
    write_then_edit: bool = False  # True if Write followed by Edit

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "file_path": self.file_path,
            "edit_count": self.edit_count,
            "write_count": self.write_count,
            "total_operations": self.total_operations,
            "write_then_edit": self.write_then_edit,
        }


@dataclass
class ChurnMetrics:
    """Overall churn metrics for a session."""

    files: dict[str, FileChurn] = field(default_factory=dict)
    multi_edit_files: list[str] = field(default_factory=list)  # Files edited 3+ times
    write_edit_files: list[str] = field(default_factory=list)  # Write then edit
    total_edits: int = 0
    total_writes: int = 0
    unique_files: int = 0

    @property
    def churn_ratio(self) -> float:
        """
        Ratio of total edits to unique files.

        Higher ratio indicates more churn (revisiting files).
        A ratio of 1.0 means each file was edited exactly once.
        """
        if self.unique_files == 0:
            return 0.0
        return self.total_edits / self.unique_files

    @property
    def has_churn(self) -> bool:
        """Check if any churn was detected."""
        return len(self.multi_edit_files) > 0 or len(self.write_edit_files) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "churn_ratio": round(self.churn_ratio, 2),
            "has_churn": self.has_churn,
            "total_edits": self.total_edits,
            "total_writes": self.total_writes,
            "unique_files": self.unique_files,
            "multi_edit_files": self.multi_edit_files,
            "write_edit_files": self.write_edit_files,
            "files": {path: f.to_dict() for path, f in self.files.items()},
        }


def _extract_file_path(span: dict[str, Any]) -> str | None:
    """
    Extract the file path from a span.

    Looks in various attributes for file path information.
    """
    name = span.get("name", "") or ""
    raw_attrs = span.get("raw_attributes", {}) or {}

    # Check for file_path in attributes
    if isinstance(raw_attrs, dict):
        # Direct file_path attribute
        if "file_path" in raw_attrs:
            return raw_attrs["file_path"]

        # Check nested attributes
        for key, value in raw_attrs.items():
            if "file" in key.lower() and "path" in key.lower():
                if isinstance(value, str):
                    return value

    # Try to extract from input_value
    input_value = span.get("input_value", "") or ""
    if isinstance(input_value, str):
        # Look for file path patterns
        # Match paths like /Users/... or ./... or relative paths with extensions
        path_match = re.search(r'["\']?((?:/[\w.-]+)+(?:\.\w+)?)["\']?', input_value)
        if path_match:
            return path_match.group(1)

    return None


def _is_edit_operation(span: dict[str, Any]) -> bool:
    """Check if span represents an edit operation."""
    name = (span.get("name") or "").lower()
    return "edit" in name or "modify" in name or "update" in name


def _is_write_operation(span: dict[str, Any]) -> bool:
    """Check if span represents a write operation."""
    name = (span.get("name") or "").lower()
    return "write" in name or "create" in name


def detect_churn(spans: list[dict[str, Any]]) -> ChurnMetrics:
    """
    Detect code churn in a list of spans.

    Args:
        spans: List of span dictionaries (should be in chronological order)

    Returns:
        ChurnMetrics with churn analysis
    """
    metrics = ChurnMetrics()

    # Track operations per file in order
    file_operations: dict[str, list[str]] = defaultdict(list)  # path -> [op_type, ...]

    for span in spans:
        # Only analyze tool spans
        classification = classify_span(span)
        if classification.category != SpanCategory.TOOLS:
            continue

        file_path = _extract_file_path(span)
        if not file_path:
            continue

        # Initialize file churn if needed
        if file_path not in metrics.files:
            metrics.files[file_path] = FileChurn(file_path=file_path)

        file_churn = metrics.files[file_path]

        # Track operation type
        if _is_edit_operation(span):
            file_churn.edit_count += 1
            metrics.total_edits += 1
            file_operations[file_path].append("edit")
        elif _is_write_operation(span):
            file_churn.write_count += 1
            metrics.total_writes += 1
            file_operations[file_path].append("write")

        file_churn.total_operations += 1

    # Analyze patterns
    metrics.unique_files = len(metrics.files)

    for file_path, operations in file_operations.items():
        file_churn = metrics.files[file_path]

        # Check for multi-edit (3+ edits to same file)
        if file_churn.edit_count >= 3:
            metrics.multi_edit_files.append(file_path)

        # Check for write-then-edit pattern
        for i in range(len(operations) - 1):
            if operations[i] == "write" and operations[i + 1] == "edit":
                file_churn.write_then_edit = True
                if file_path not in metrics.write_edit_files:
                    metrics.write_edit_files.append(file_path)
                break

    return metrics


def get_churn_summary(metrics: ChurnMetrics) -> dict[str, Any]:
    """
    Get a summary of churn metrics.

    Args:
        metrics: ChurnMetrics object

    Returns:
        Dictionary with churn summary
    """
    return {
        "has_churn": metrics.has_churn,
        "churn_ratio": round(metrics.churn_ratio, 2),
        "multi_edit_file_count": len(metrics.multi_edit_files),
        "write_edit_file_count": len(metrics.write_edit_files),
        "total_edits": metrics.total_edits,
        "total_writes": metrics.total_writes,
        "unique_files": metrics.unique_files,
    }
