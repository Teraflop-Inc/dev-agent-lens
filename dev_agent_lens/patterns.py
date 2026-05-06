"""
Pattern Matching Infrastructure

Provides generic pattern matching dataclasses and extraction functions
that can be extended by downstream packages for domain-specific use cases.

This module provides:
- PatternMatch: A generic dataclass representing a pattern match in trace data
- extract_patterns: A function to extract all matches of a pattern from sessions
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PatternMatch:
    """
    A pattern match found in trace data.

    This is a generic base class that can be extended by downstream packages
    to add domain-specific fields (e.g., parsed ticket numbers, entity types).

    Attributes:
        pattern: The regex pattern that was used to find this match
        match: The actual matched text (e.g., "ENG2-123")
        session_id: ID of the session containing the match (if applicable)
        span_id: ID of the span containing the match (if applicable)
        field: The field where the match was found ("input_value", "output_value", etc.)
        context: Surrounding text for additional context
        start_pos: Character position where the match starts in the field
        end_pos: Character position where the match ends in the field
    """

    # All fields have defaults to allow subclass extension without ordering issues
    pattern: str = ""
    match: str = ""
    session_id: str | None = None
    span_id: str | None = None
    field: str | None = None
    context: str | None = None
    start_pos: int | None = None
    end_pos: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pattern": self.pattern,
            "match": self.match,
            "session_id": self.session_id,
            "span_id": self.span_id,
            "field": self.field,
            "context": self.context,
            "start_pos": self.start_pos,
            "end_pos": self.end_pos,
        }


# Fields to search by default
DEFAULT_PATTERN_FIELDS = [
    "input_value",
    "output_value",
    "input_messages",
    "output_messages",
    "name",
]


def _get_text_value(obj: Any, field_name: str) -> str | None:
    """Extract a string value from an object field."""
    if isinstance(obj, dict):
        value = obj.get(field_name)
    else:
        value = getattr(obj, field_name, None)

    if value is None:
        return None
    if isinstance(value, str):
        return value
    # Convert lists/dicts to string for searching
    return str(value)


def _extract_context(text: str, start: int, end: int, context_chars: int = 50) -> str:
    """Extract surrounding context from text around a match."""
    ctx_start = max(0, start - context_chars)
    ctx_end = min(len(text), end + context_chars)

    prefix = "..." if ctx_start > 0 else ""
    suffix = "..." if ctx_end < len(text) else ""

    return f"{prefix}{text[ctx_start:ctx_end]}{suffix}"


def extract_patterns(
    sessions: list[dict[str, Any]],
    pattern: str,
    fields: list[str] | None = None,
    include_context: bool = True,
    context_chars: int = 50,
    case_insensitive: bool = True,
) -> list[PatternMatch]:
    """
    Extract all matches of a pattern from sessions.

    Args:
        sessions: List of session dictionaries, each with "session_id" and "spans"
        pattern: Regular expression pattern to search for
        fields: Fields to search within spans. If None, searches DEFAULT_PATTERN_FIELDS
        include_context: Whether to include surrounding text context
        context_chars: Number of characters of context to include on each side
        case_insensitive: Whether to perform case-insensitive matching

    Returns:
        List of PatternMatch objects for all matches found

    Example:
        >>> sessions = [{"session_id": "s1", "spans": [{"input_value": "Working on ENG2-123"}]}]
        >>> matches = extract_patterns(sessions, r"ENG2-\\d+")
        >>> matches[0].match
        'ENG2-123'
    """
    if fields is None:
        fields = DEFAULT_PATTERN_FIELDS

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}") from e

    matches: list[PatternMatch] = []

    for session in sessions:
        session_id = session.get("session_id")
        spans = session.get("spans", [])

        for span in spans:
            span_id = span.get("span_id")

            for field_name in fields:
                text = _get_text_value(span, field_name)
                if not text:
                    continue

                for m in compiled.finditer(text):
                    context = None
                    if include_context:
                        context = _extract_context(text, m.start(), m.end(), context_chars)

                    matches.append(
                        PatternMatch(
                            pattern=pattern,
                            match=m.group(),
                            session_id=session_id,
                            span_id=span_id,
                            field=field_name,
                            context=context,
                            start_pos=m.start(),
                            end_pos=m.end(),
                        )
                    )

    return matches


def extract_unique_matches(
    sessions: list[dict[str, Any]],
    pattern: str,
    fields: list[str] | None = None,
    case_insensitive: bool = True,
) -> set[str]:
    """
    Extract unique matched strings from sessions.

    This is a convenience function when you only need the unique matched values
    without location information.

    Args:
        sessions: List of session dictionaries
        pattern: Regular expression pattern to search for
        fields: Fields to search within spans
        case_insensitive: Whether to perform case-insensitive matching

    Returns:
        Set of unique matched strings

    Example:
        >>> sessions = [{"session_id": "s1", "spans": [{"input_value": "ENG2-123 and ENG2-456"}]}]
        >>> extract_unique_matches(sessions, r"ENG2-\\d+")
        {'ENG2-123', 'ENG2-456'}
    """
    matches = extract_patterns(
        sessions, pattern, fields, include_context=False, case_insensitive=case_insensitive
    )
    return {m.match for m in matches}


def group_matches_by_session(
    matches: list[PatternMatch],
) -> dict[str, list[PatternMatch]]:
    """
    Group pattern matches by session ID.

    Args:
        matches: List of PatternMatch objects

    Returns:
        Dictionary mapping session_id to list of matches in that session
    """
    by_session: dict[str, list[PatternMatch]] = {}
    for m in matches:
        if m.session_id:
            by_session.setdefault(m.session_id, []).append(m)
    return by_session


def group_matches_by_value(
    matches: list[PatternMatch],
) -> dict[str, list[PatternMatch]]:
    """
    Group pattern matches by matched value.

    Args:
        matches: List of PatternMatch objects

    Returns:
        Dictionary mapping matched value to list of matches with that value
    """
    by_value: dict[str, list[PatternMatch]] = {}
    for m in matches:
        by_value.setdefault(m.match, []).append(m)
    return by_value
