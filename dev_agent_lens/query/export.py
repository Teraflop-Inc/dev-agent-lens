"""
Export Formats Module

Provides formatters for exporting query results to various formats:
- JSON (default)
- CSV
- Markdown table
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Literal

from dev_agent_lens.query.query import QueryResult

ExportFormat = Literal["json", "csv", "markdown"]


def export_json(
    result: QueryResult,
    output_file: str | Path | None = None,
    indent: int = 2,
) -> str:
    """
    Export query results to JSON format.

    Args:
        result: The QueryResult to export
        output_file: Optional file path to write to
        indent: JSON indentation level (default: 2)

    Returns:
        JSON string representation of the results
    """
    data = result.to_dict()
    json_str = json.dumps(data, indent=indent, default=str)

    if output_file:
        Path(output_file).write_text(json_str)

    return json_str


def export_csv(
    result: QueryResult,
    output_file: str | Path | None = None,
    include_session_id: bool = True,
) -> str:
    """
    Export query results to CSV format.

    Flattens all spans into rows with common columns.

    Args:
        result: The QueryResult to export
        output_file: Optional file path to write to
        include_session_id: Whether to include session_id column (default: True)

    Returns:
        CSV string representation of the results
    """
    # Get all spans with session info
    rows = []
    for session in result.sessions:
        session_id = session.get("session_id")
        for span in session.get("spans", []):
            row = dict(span)
            if include_session_id:
                row["session_id"] = session_id
            rows.append(row)

    if not rows:
        return ""

    # Get all unique columns
    all_columns = set()
    for row in rows:
        all_columns.update(row.keys())

    # Order columns: session_id first if included, then alphabetically
    columns = sorted(all_columns)
    if include_session_id and "session_id" in columns:
        columns.remove("session_id")
        columns = ["session_id"] + columns

    # Write CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    for row in rows:
        # Convert complex objects to strings
        row_copy = {}
        for col in columns:
            value = row.get(col)
            if isinstance(value, (dict, list)):
                row_copy[col] = json.dumps(value)
            else:
                row_copy[col] = value
        writer.writerow(row_copy)

    csv_str = output.getvalue()

    if output_file:
        Path(output_file).write_text(csv_str)

    return csv_str


def export_markdown(
    result: QueryResult,
    output_file: str | Path | None = None,
    columns: list[str] | None = None,
    max_width: int = 50,
) -> str:
    """
    Export query results to Markdown table format.

    Args:
        result: The QueryResult to export
        output_file: Optional file path to write to
        columns: List of columns to include. If None, uses a default set.
        max_width: Maximum width for cell content (truncates with ...)

    Returns:
        Markdown table string representation of the results
    """
    # Default columns for markdown (keep it readable)
    if columns is None:
        columns = ["session_id", "span_id", "name", "status_code", "start_time"]

    # Get all spans with session info
    rows = []
    for session in result.sessions:
        session_id = session.get("session_id")
        for span in session.get("spans", []):
            row = {"session_id": session_id}
            row.update(span)
            rows.append(row)

    if not rows:
        return "No results found."

    # Filter columns to only those that exist
    available_columns = set()
    for row in rows:
        available_columns.update(row.keys())
    columns = [c for c in columns if c in available_columns]

    if not columns:
        return "No columns to display."

    def truncate(value: Any, width: int) -> str:
        """Truncate value to max width."""
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            s = json.dumps(value)
        else:
            s = str(value)
        if len(s) > width:
            return s[: width - 3] + "..."
        return s

    # Build header
    lines = []
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    lines.append(header)
    lines.append(separator)

    # Build rows
    for row in rows:
        cells = [truncate(row.get(col), max_width) for col in columns]
        lines.append("| " + " | ".join(cells) + " |")

    # Add summary
    lines.append("")
    lines.append(f"*{result.total_spans} spans in {result.total_sessions} sessions*")

    md_str = "\n".join(lines)

    if output_file:
        Path(output_file).write_text(md_str)

    return md_str


def export(
    result: QueryResult,
    format: ExportFormat = "json",
    output_file: str | Path | None = None,
    **kwargs: Any,
) -> str:
    """
    Export query results to the specified format.

    Args:
        result: The QueryResult to export
        format: Output format ("json", "csv", or "markdown")
        output_file: Optional file path to write to
        **kwargs: Additional arguments passed to the format-specific exporter

    Returns:
        String representation of the results in the specified format

    Raises:
        ValueError: If format is not supported
    """
    exporters = {
        "json": export_json,
        "csv": export_csv,
        "markdown": export_markdown,
    }

    if format not in exporters:
        valid_formats = list(exporters.keys())
        raise ValueError(f"Unsupported format '{format}'. Valid formats: {valid_formats}")

    return exporters[format](result, output_file=output_file, **kwargs)
