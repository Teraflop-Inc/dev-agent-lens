"""
Regex Search Engine

Provides regex-based search functionality for trace spans stored in JSONL files.
Supports Python regex syntax, case-insensitive matching, and field-specific searches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


class RegexSearchError(Exception):
    """Raised when there's an error with the regex pattern or search operation."""

    pass


# String fields that are searched by default
DEFAULT_SEARCH_FIELDS = [
    "name",
    "input_value",
    "output_value",
    "input_messages",
    "output_messages",
    "llm_model_name",
    "status_code",
    "raw_attributes",
]

# All valid searchable fields (includes defaults plus additional fields)
VALID_SEARCH_FIELDS = set(DEFAULT_SEARCH_FIELDS) | {
    "span_id",
    "trace_id",
    "parent_id",
    "span_kind",
    "start_time",
    "end_time",
    "backend",
}


class InvalidFieldError(Exception):
    """Raised when an invalid field name is provided."""

    pass


def validate_fields(fields: list[str] | None) -> None:
    """
    Validate that all provided field names are valid.

    Args:
        fields: List of field names to validate

    Raises:
        InvalidFieldError: If any field name is not valid
    """
    if fields is None:
        return

    invalid = [f for f in fields if f not in VALID_SEARCH_FIELDS]
    if invalid:
        valid_list = sorted(VALID_SEARCH_FIELDS)
        raise InvalidFieldError(
            f"Invalid field(s): {invalid}. Valid fields are: {valid_list}"
        )


@dataclass
class SearchMatch:
    """
    Represents a single match found in a span.

    Attributes:
        span: The full span dictionary that contains the match
        field: The field name where the match was found
        match_start: Character position where the match starts in the field value
        match_end: Character position where the match ends in the field value
        matched_text: The actual text that matched the pattern
        line_number: Line number in the JSONL file (1-indexed)
    """

    span: dict[str, Any]
    field: str
    match_start: int
    match_end: int
    matched_text: str
    line_number: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "span": self.span,
            "field": self.field,
            "match_start": self.match_start,
            "match_end": self.match_end,
            "matched_text": self.matched_text,
            "line_number": self.line_number,
        }


def _compile_pattern(pattern: str, case_insensitive: bool = False) -> re.Pattern:
    """
    Compile a regex pattern with error handling.

    Args:
        pattern: The regex pattern string
        case_insensitive: Whether to enable case-insensitive matching

    Returns:
        Compiled regex pattern

    Raises:
        RegexSearchError: If the pattern is invalid
    """
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise RegexSearchError(f"Invalid regex pattern '{pattern}': {e}") from e


def _get_searchable_value(span: dict[str, Any], field_name: str) -> str | None:
    """
    Get a searchable string value from a span field.

    Handles nested fields and converts various types to strings.

    Args:
        span: The span dictionary
        field_name: The field to extract

    Returns:
        String value or None if field doesn't exist or is None
    """
    value = span.get(field_name)

    if value is None:
        return None

    # If it's already a string, return it
    if isinstance(value, str):
        return value

    # If it's a dict or list, convert to JSON string for searching
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return str(value)

    # For other types, convert to string
    return str(value)


def _search_span(
    span: dict[str, Any],
    compiled_pattern: re.Pattern,
    fields: list[str] | None = None,
    line_number: int = 0,
) -> list[SearchMatch]:
    """
    Search a single span for matches.

    Args:
        span: The span dictionary to search
        compiled_pattern: Pre-compiled regex pattern
        fields: List of field names to search (None = all default fields)
        line_number: Line number in the source file

    Returns:
        List of SearchMatch objects for all matches found
    """
    matches = []
    search_fields = fields if fields is not None else DEFAULT_SEARCH_FIELDS

    for field_name in search_fields:
        value = _get_searchable_value(span, field_name)
        if value is None:
            continue

        # Find all matches in this field
        for match in compiled_pattern.finditer(value):
            matches.append(
                SearchMatch(
                    span=span,
                    field=field_name,
                    match_start=match.start(),
                    match_end=match.end(),
                    matched_text=match.group(),
                    line_number=line_number,
                )
            )

    return matches


def search(
    pattern: str,
    spans: list[dict[str, Any]] | pd.DataFrame,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
) -> list[SearchMatch]:
    """
    Search spans for a regex pattern.

    Args:
        pattern: Python regex pattern to search for
        spans: List of span dictionaries or DataFrame with spans
        fields: List of field names to search. If None, searches all default
                string fields (name, input_value, output_value, input_messages,
                output_messages, llm_model_name, status_code, raw_attributes)
        case_insensitive: Whether to enable case-insensitive matching

    Returns:
        List of SearchMatch objects containing matching spans and match locations

    Raises:
        RegexSearchError: If the pattern is invalid
        InvalidFieldError: If any field name is not valid

    Example:
        >>> matches = search(r"ENG2-\\d+", spans)
        >>> for m in matches:
        ...     print(f"Found '{m.matched_text}' in {m.field}")
    """
    # Validate fields before searching
    validate_fields(fields)

    compiled = _compile_pattern(pattern, case_insensitive)

    # Convert DataFrame to list of dicts if needed
    if isinstance(spans, pd.DataFrame):
        spans = spans.to_dict("records")

    all_matches = []
    for span in spans:
        matches = _search_span(span, compiled, fields)
        all_matches.extend(matches)

    return all_matches


def search_file(
    pattern: str,
    file_path: str | Path,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
) -> list[SearchMatch]:
    """
    Search a JSONL file for a regex pattern.

    Efficiently processes the file line by line to handle large files
    without loading everything into memory.

    Args:
        pattern: Python regex pattern to search for
        file_path: Path to the JSONL file
        fields: List of field names to search. If None, searches all default
                string fields
        case_insensitive: Whether to enable case-insensitive matching

    Returns:
        List of SearchMatch objects containing matching spans and match locations

    Raises:
        RegexSearchError: If the pattern is invalid
        InvalidFieldError: If any field name is not valid
        FileNotFoundError: If the file doesn't exist

    Example:
        >>> matches = search_file(r"error", "sessions/sessions_current.jsonl", case_insensitive=True)
        >>> print(f"Found {len(matches)} matches")
    """
    # Validate fields before searching
    validate_fields(fields)

    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    compiled = _compile_pattern(pattern, case_insensitive)

    all_matches = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                # Skip invalid JSON lines
                continue

            matches = _search_span(span, compiled, fields, line_number=line_num)
            all_matches.extend(matches)

    return all_matches


def search_dataframe(
    pattern: str,
    df: pd.DataFrame,
    fields: list[str] | None = None,
    case_insensitive: bool = False,
) -> pd.DataFrame:
    """
    Search a DataFrame of spans and return matching rows.

    This is a convenience function that returns a filtered DataFrame
    instead of SearchMatch objects.

    Args:
        pattern: Python regex pattern to search for
        df: DataFrame with span data
        fields: List of field names to search. If None, searches all default
                string fields
        case_insensitive: Whether to enable case-insensitive matching

    Returns:
        DataFrame containing only rows that have at least one match

    Raises:
        RegexSearchError: If the pattern is invalid
    """
    if df.empty:
        return df

    matches = search(pattern, df, fields, case_insensitive)

    if not matches:
        return df.iloc[0:0]  # Return empty DataFrame with same columns

    # Get unique span_ids that matched
    matched_span_ids = set()
    for match in matches:
        span_id = match.span.get("span_id")
        if span_id:
            matched_span_ids.add(span_id)

    # Filter DataFrame to matching rows
    if "span_id" in df.columns and matched_span_ids:
        return df[df["span_id"].isin(matched_span_ids)]

    # Fallback: return all rows that had matches (by index)
    matched_indices = set()
    spans_list = df.to_dict("records")
    for i, span in enumerate(spans_list):
        if _search_span(span, _compile_pattern(pattern, case_insensitive), fields):
            matched_indices.add(i)

    return df.iloc[list(matched_indices)]
