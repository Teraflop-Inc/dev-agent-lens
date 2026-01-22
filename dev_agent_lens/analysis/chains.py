"""
Conversation Chain Module

Links sessions across compactions to reconstruct unified conversations.

Primary linking method: Claude session UUID embedded in span metadata.
Each Claude Code conversation has a unique session UUID that persists
across all compactions, providing definitive chain linking.

Fallback method: Temporal proximity combined with compaction markers
(for spans that don't have the UUID metadata).

A conversation chain represents a single logical conversation that may
have been split across multiple sessions due to context window limits.
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from dev_agent_lens.analysis.threads import (
    COMPACTION_CONTINUATION_MARKER,
    COMPACTION_TASK_MARKER,
    classify_span_thread,
    ThreadType,
)


# Maximum time gap between sessions to consider them linked (10 seconds)
# Only used as fallback when Claude session UUID is not available
MAX_SESSION_GAP_SECONDS = 60  # Increased from 10 to 60 for fallback cases


@dataclass
class ConversationChain:
    """A chain of linked sessions forming a unified conversation."""

    chain_id: str
    session_ids: list[str] = field(default_factory=list)
    start_time: datetime | None = None
    end_time: datetime | None = None
    compaction_count: int = 0
    total_spans: int = 0
    total_tokens: int = 0
    # Claude session UUID - definitive identifier for the conversation
    claude_session_id: str | None = None
    # User hash from metadata (for multi-user filtering)
    user_hash: str | None = None

    @property
    def session_count(self) -> int:
        """Number of sessions in the chain."""
        return len(self.session_ids)

    @property
    def duration_seconds(self) -> float:
        """Total duration in seconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0

    @property
    def duration_minutes(self) -> float:
        """Total duration in minutes."""
        return self.duration_seconds / 60.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "chain_id": self.chain_id,
            "session_ids": self.session_ids,
            "session_count": self.session_count,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_minutes": round(self.duration_minutes, 2),
            "compaction_count": self.compaction_count,
            "total_spans": self.total_spans,
            "total_tokens": self.total_tokens,
            "claude_session_id": self.claude_session_id,
            "user_hash": self.user_hash,
        }


def _parse_timestamp(ts: Any) -> datetime | None:
    """Parse a timestamp to datetime, handling various formats."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if not isinstance(ts, str):
        return None
    try:
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                return datetime.strptime(ts[:26], fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _safe_str(value: Any) -> str:
    """Convert value to string, handling NaN and None gracefully."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value) if value else ""


def extract_claude_session_info(span: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Extract Claude session UUID and user hash from span metadata.

    The metadata can be in multiple locations depending on the source:
    1. attributes.metadata.requester_metadata.user_id (preferred - lambda2/local-alex)
    2. attributes.metadata.user_api_key_end_user_id (alternative location)
    3. attributes.llm.*.metadata.user_id (legacy format)

    Formats also vary:
    - Dotted keys: {"attributes.metadata": {...}} (lambda2)
    - Nested dicts: {"attributes": {"metadata": {...}}} (local-alex)

    The user_id format is:
    'user_{user_hash}_account_{account_uuid}_session_{session_uuid}'

    Returns:
        Tuple of (claude_session_uuid, user_hash), either may be None if not found.
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        # Also check raw_attributes directly (unified sessions)
        raw_attrs = span.get("raw_attributes")
    if not raw_attrs:
        return None, None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Helper to extract session info from a user_id string
        def extract_from_user_id(user_id: str) -> tuple[str | None, str | None]:
            if not user_id:
                return None, None
            # Extract session UUID (36-char UUID after 'session_')
            session_match = re.search(r"session_([a-f0-9\-]{36})", user_id)
            session_uuid = session_match.group(1) if session_match else None
            # Extract user hash (between 'user_' and '_account')
            user_match = re.search(r"user_([a-zA-Z0-9]+)_account", user_id)
            user_hash = user_match.group(1) if user_match else None
            return session_uuid, user_hash

        # =============================================================
        # PATH 1: Dotted key format (lambda2-dal)
        # {"attributes.metadata": "{...json string...}"}
        # =============================================================
        dotted_metadata = attrs.get("attributes.metadata")
        if dotted_metadata:
            try:
                if isinstance(dotted_metadata, str):
                    metadata_dict = json.loads(dotted_metadata)
                else:
                    metadata_dict = dotted_metadata

                # Try requester_metadata.user_id first
                req_meta = metadata_dict.get("requester_metadata", {})
                if isinstance(req_meta, dict):
                    user_id = req_meta.get("user_id", "")
                    result = extract_from_user_id(user_id)
                    if result[0]:
                        return result

                # Try user_api_key_end_user_id
                user_id = metadata_dict.get("user_api_key_end_user_id", "")
                result = extract_from_user_id(user_id)
                if result[0]:
                    return result
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # =============================================================
        # PATH 2: Nested dict format (local-alex)
        # {"attributes": {"metadata": {...}}}
        # =============================================================
        attributes = attrs.get("attributes", {})
        if isinstance(attributes, dict):
            metadata = attributes.get("metadata", {})
            if isinstance(metadata, dict):
                # Try requester_metadata.user_id
                req_meta = metadata.get("requester_metadata", {})
                if isinstance(req_meta, dict):
                    user_id = req_meta.get("user_id", "")
                    result = extract_from_user_id(user_id)
                    if result[0]:
                        return result

                # Try user_api_key_end_user_id
                user_id = metadata.get("user_api_key_end_user_id", "")
                result = extract_from_user_id(user_id)
                if result[0]:
                    return result

        # =============================================================
        # PATH 3: Legacy llm.*.metadata format
        # {"attributes": {"llm": {"model_name": {"metadata": "..."}}}}
        # =============================================================
        llm_section = attributes.get("llm", {}) if isinstance(attributes, dict) else {}
        for key, llm_data in llm_section.items():
            if isinstance(llm_data, dict) and "metadata" in llm_data:
                metadata_str = llm_data["metadata"]
                try:
                    # Metadata is a Python literal string, not JSON
                    metadata_dict = ast.literal_eval(metadata_str)
                    user_id = metadata_dict.get("user_id", "")
                    result = extract_from_user_id(user_id)
                    if result[0]:
                        return result
                except (ValueError, SyntaxError):
                    pass

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return None, None


def extract_session_claude_id(session: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Extract Claude session UUID and user hash from any span in a session.

    Args:
        session: Session dictionary with 'spans' list.

    Returns:
        Tuple of (claude_session_uuid, user_hash), either may be None if not found.
    """
    spans = session.get("spans", [])
    for span in spans:
        session_uuid, user_hash = extract_claude_session_info(span)
        if session_uuid:
            return session_uuid, user_hash
    return None, None


def _extract_input_value(span: dict[str, Any]) -> str:
    """Extract input value from span, checking multiple locations."""
    # First try direct input_value field
    input_val = _safe_str(span.get("input_value"))
    if input_val:
        return input_val

    # Try raw_attributes_json
    raw_attrs = span.get("raw_attributes_json")
    if raw_attrs:
        try:
            if isinstance(raw_attrs, str):
                attrs = json.loads(raw_attrs)
            else:
                attrs = raw_attrs
            return _safe_str(attrs.get("attributes", {}).get("input", {}).get("value", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return ""


def has_compaction_marker(span: dict[str, Any]) -> bool:
    """Check if a span has a compaction continuation marker."""
    input_val = _extract_input_value(span)
    return COMPACTION_CONTINUATION_MARKER in input_val


def is_compaction_task(span: dict[str, Any]) -> bool:
    """Check if a span is a compaction task (generating summary)."""
    input_val = _extract_input_value(span)
    return COMPACTION_TASK_MARKER in input_val


def get_session_time_range(
    spans: list[dict[str, Any]],
) -> tuple[datetime | None, datetime | None]:
    """Get the start and end time for a session's spans."""
    timestamps: list[datetime] = []

    for span in spans:
        start_ts = _parse_timestamp(span.get("start_time"))
        end_ts = _parse_timestamp(span.get("end_time"))
        if start_ts:
            timestamps.append(start_ts)
        if end_ts:
            timestamps.append(end_ts)

    if not timestamps:
        return None, None

    return min(timestamps), max(timestamps)


def identify_compaction_sessions(
    sessions: list[dict[str, Any]],
) -> set[str]:
    """
    Identify sessions that are continuations from compaction.

    Args:
        sessions: List of session dictionaries with 'session_id' and 'spans' keys.

    Returns:
        Set of session IDs that have compaction continuation markers.
    """
    compaction_sessions: set[str] = set()

    for session in sessions:
        session_id = session.get("session_id", "")
        spans = session.get("spans", [])

        for span in spans:
            if has_compaction_marker(span):
                compaction_sessions.add(session_id)
                break

    return compaction_sessions


def count_compaction_events(spans: list[dict[str, Any]]) -> int:
    """
    Count the number of compaction events (boundaries) within a list of spans.

    This counts actual compaction markers in the spans, useful for unified sessions
    where all spans from multiple compaction cycles are already grouped under
    one session_id.

    For compaction TASKS (generating summary), use is_compaction_task().
    For compaction CONTINUATIONS (resuming after compaction), use has_compaction_marker().

    We count compaction tasks as they mark distinct compaction boundaries.

    Args:
        spans: List of span dictionaries.

    Returns:
        Number of compaction events found in the spans.
    """
    count = 0
    for span in spans:
        if is_compaction_task(span):
            count += 1
    return count


def build_session_links(
    sessions: list[dict[str, Any]],
    max_gap_seconds: float = MAX_SESSION_GAP_SECONDS,
) -> dict[str, str]:
    """
    Build links between sessions based on temporal proximity.

    A session is linked to a previous session if:
    1. It has a compaction continuation marker
    2. It starts within max_gap_seconds of the previous session ending

    Args:
        sessions: List of session dictionaries with 'session_id' and 'spans' keys.
        max_gap_seconds: Maximum time gap to consider sessions linked.

    Returns:
        Dictionary mapping session_id -> previous_session_id for linked sessions.
    """
    # Get time ranges for all sessions
    session_times: list[tuple[str, datetime | None, datetime | None]] = []
    for session in sessions:
        session_id = session.get("session_id", "")
        spans = session.get("spans", [])
        start_time, end_time = get_session_time_range(spans)
        if start_time:
            session_times.append((session_id, start_time, end_time))

    # Sort by start time
    session_times.sort(key=lambda x: x[1])  # type: ignore

    # Identify compaction sessions
    compaction_sessions = identify_compaction_sessions(sessions)

    # Build links
    links: dict[str, str] = {}
    max_gap = timedelta(seconds=max_gap_seconds)

    for i in range(1, len(session_times)):
        curr_id, curr_start, _ = session_times[i]
        prev_id, _, prev_end = session_times[i - 1]

        # Only link if current session has compaction marker
        if curr_id not in compaction_sessions:
            continue

        # Check time gap
        if prev_end and curr_start:
            gap = curr_start - prev_end
            if timedelta(0) <= gap <= max_gap:
                links[curr_id] = prev_id

    return links


def build_conversation_chains(
    sessions: list[dict[str, Any]],
    max_gap_seconds: float = MAX_SESSION_GAP_SECONDS,
) -> list[ConversationChain]:
    """
    Build conversation chains from sessions.

    Primary method: Group sessions by Claude session UUID (definitive linking).
    Fallback method: Temporal proximity with compaction markers.

    Args:
        sessions: List of session dictionaries with 'session_id' and 'spans' keys.
        max_gap_seconds: Maximum time gap for fallback temporal linking.

    Returns:
        List of ConversationChain objects, each representing a unified conversation.
    """
    # Create session lookup
    session_lookup: dict[str, dict[str, Any]] = {
        s.get("session_id", ""): s for s in sessions
    }

    # === PRIMARY METHOD: Group by Claude session UUID ===
    # Extract Claude session UUIDs for all sessions
    uuid_to_sessions: dict[str, list[str]] = defaultdict(list)
    session_to_uuid: dict[str, str] = {}
    session_to_user_hash: dict[str, str] = {}
    sessions_without_uuid: list[str] = []

    for session in sessions:
        session_id = session.get("session_id", "")
        claude_uuid, user_hash = extract_session_claude_id(session)

        if claude_uuid:
            uuid_to_sessions[claude_uuid].append(session_id)
            session_to_uuid[session_id] = claude_uuid
            if user_hash:
                session_to_user_hash[session_id] = user_hash
        else:
            sessions_without_uuid.append(session_id)

    chains: list[ConversationChain] = []
    processed: set[str] = set()

    # Build chains from UUID groups
    for claude_uuid, session_ids in uuid_to_sessions.items():
        # Sort sessions by start time
        session_times: list[tuple[str, datetime | None]] = []
        for sid in session_ids:
            session = session_lookup.get(sid, {})
            spans = session.get("spans", [])
            start, _ = get_session_time_range(spans)
            session_times.append((sid, start))

        session_times.sort(key=lambda x: x[1] or datetime.min)
        sorted_session_ids = [sid for sid, _ in session_times]

        # Calculate chain metadata
        chain_start: datetime | None = None
        chain_end: datetime | None = None
        total_spans = 0
        total_tokens = 0
        compaction_count = 0

        # Get user hash from first session that has it
        user_hash = None
        for sid in sorted_session_ids:
            if sid in session_to_user_hash:
                user_hash = session_to_user_hash[sid]
                break

        for sid in sorted_session_ids:
            session = session_lookup.get(sid, {})
            spans = session.get("spans", [])
            total_spans += len(spans)
            processed.add(sid)

            start, end = get_session_time_range(spans)
            if start and (chain_start is None or start < chain_start):
                chain_start = start
            if end and (chain_end is None or end > chain_end):
                chain_end = end

            # Count tokens
            for span in spans:
                try:
                    tokens = int(float(span.get("llm_token_count_total", 0) or 0))
                    total_tokens += tokens
                except (ValueError, TypeError):
                    pass

            # Count compaction events WITHIN each session
            # This handles unified sessions where spans from multiple
            # compaction cycles are already grouped under one session_id
            compaction_count += count_compaction_events(spans)

        chain = ConversationChain(
            chain_id=sorted_session_ids[0],  # Use first session ID as chain ID
            session_ids=sorted_session_ids,
            start_time=chain_start,
            end_time=chain_end,
            compaction_count=compaction_count,
            total_spans=total_spans,
            total_tokens=total_tokens,
            claude_session_id=claude_uuid,
            user_hash=user_hash,
        )
        chains.append(chain)

    # === FALLBACK METHOD: Temporal linking for sessions without UUID ===
    if sessions_without_uuid:
        # Build session links using temporal proximity
        fallback_sessions = [
            session_lookup.get(sid, {}) for sid in sessions_without_uuid
        ]
        links = build_session_links(fallback_sessions, max_gap_seconds)

        # Find chain roots and follow chains forward
        linked_to: set[str] = set(links.values())
        has_link: set[str] = set(links.keys())
        in_chain: set[str] = linked_to | has_link

        for session_id in sorted(in_chain):
            if session_id in processed:
                continue

            # Follow links backwards to find root
            current = session_id
            while current in links:
                current = links[current]

            # Now follow forward from root
            chain_ids: list[str] = [current]
            processed.add(current)

            # Build reverse lookup: prev_id -> curr_id
            reverse_links: dict[str, str] = {v: k for k, v in links.items()}

            while current in reverse_links:
                next_id = reverse_links[current]
                chain_ids.append(next_id)
                processed.add(next_id)
                current = next_id

            # Calculate chain metadata
            chain_start = None
            chain_end = None
            total_spans = 0
            total_tokens = 0
            compaction_count = 0

            for sid in chain_ids:
                session = session_lookup.get(sid, {})
                spans = session.get("spans", [])
                total_spans += len(spans)

                start, end = get_session_time_range(spans)
                if start and (chain_start is None or start < chain_start):
                    chain_start = start
                if end and (chain_end is None or end > chain_end):
                    chain_end = end

                # Count tokens
                for span in spans:
                    try:
                        tokens = int(float(span.get("llm_token_count_total", 0) or 0))
                        total_tokens += tokens
                    except (ValueError, TypeError):
                        pass

                # Count compactions
                if sid in has_link:
                    compaction_count += 1

            chain = ConversationChain(
                chain_id=chain_ids[0],
                session_ids=chain_ids,
                start_time=chain_start,
                end_time=chain_end,
                compaction_count=compaction_count,
                total_spans=total_spans,
                total_tokens=total_tokens,
                claude_session_id=None,  # No UUID for fallback chains
                user_hash=None,
            )
            chains.append(chain)

    # Sort chains by start time
    chains.sort(key=lambda c: c.start_time or datetime.min)

    return chains


def get_chain_for_session(
    session_id: str,
    sessions: list[dict[str, Any]],
    max_gap_seconds: float = MAX_SESSION_GAP_SECONDS,
) -> ConversationChain | None:
    """
    Get the conversation chain that contains a specific session.

    Args:
        session_id: The session ID to find.
        sessions: List of all sessions.
        max_gap_seconds: Maximum time gap for linking.

    Returns:
        ConversationChain containing the session, or None if not in a chain.
    """
    chains = build_conversation_chains(sessions, max_gap_seconds)
    for chain in chains:
        if session_id in chain.session_ids:
            return chain
    return None


def get_chain_summary_stats(chains: list[ConversationChain]) -> dict[str, Any]:
    """
    Get summary statistics for conversation chains.

    Args:
        chains: List of ConversationChain objects.

    Returns:
        Dictionary with summary statistics.
    """
    if not chains:
        return {
            "total_chains": 0,
            "total_sessions_in_chains": 0,
            "avg_chain_length": 0.0,
            "max_chain_length": 0,
            "total_compactions": 0,
            "chain_length_distribution": {},
        }

    total_sessions = sum(c.session_count for c in chains)
    total_compactions = sum(c.compaction_count for c in chains)
    lengths = [c.session_count for c in chains]

    # Build length distribution
    length_dist: dict[str, int] = {}
    for length in lengths:
        if length <= 3:
            key = str(length)
        elif length <= 5:
            key = "4-5"
        elif length <= 10:
            key = "6-10"
        else:
            key = "11+"
        length_dist[key] = length_dist.get(key, 0) + 1

    return {
        "total_chains": len(chains),
        "total_sessions_in_chains": total_sessions,
        "avg_chain_length": round(total_sessions / len(chains), 2),
        "max_chain_length": max(lengths),
        "total_compactions": total_compactions,
        "chain_length_distribution": length_dist,
    }


# =============================================================================
# MARKDOWN EXPORT
# =============================================================================


def _extract_output_value(span: dict[str, Any]) -> str:
    """Extract output value from span."""
    # First try direct output_value field
    output_val = _safe_str(span.get("output_value"))
    if output_val:
        return output_val

    # Try raw_attributes_json
    raw_attrs = span.get("raw_attributes_json")
    if raw_attrs:
        try:
            if isinstance(raw_attrs, str):
                attrs = json.loads(raw_attrs)
            else:
                attrs = raw_attrs
            return _safe_str(attrs.get("attributes", {}).get("output", {}).get("value", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return ""


def _extract_model(span: dict[str, Any]) -> str:
    """Extract model name from span."""
    # Try direct field
    model = span.get("llm_model_name")
    if model:
        return _safe_str(model)

    # Try raw_attributes_json
    raw_attrs = span.get("raw_attributes_json")
    if raw_attrs:
        try:
            if isinstance(raw_attrs, str):
                attrs = json.loads(raw_attrs)
            else:
                attrs = raw_attrs
            llm = attrs.get("attributes", {}).get("llm", {})
            if isinstance(llm, dict):
                return _safe_str(llm.get("model_name", ""))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    return ""


def _is_main_thread_span(span: dict[str, Any]) -> bool:
    """
    Check if span is a main conversation thread span for markdown export.

    For markdown export, we want to include:
    - User messages and assistant responses (the actual conversation)
    - Tool call results that are part of the conversation flow

    We want to EXCLUDE:
    - Topic detection responses (e.g., "isNewTopic": false)
    - Status line checks
    - Other internal routing/classification that isn't user-visible

    Note: This is different from the thread classification in threads.py,
    which is designed for analytics (counting turns by type). Here we need
    a simpler check that keeps the conversation flow intact.
    """
    name = _safe_str(span.get("name"))

    # Must be a conversation span by name pattern
    main_patterns = [
        "Claude_Code_Internal_Prompt_",
        "Claude_Code_Final_Output_",
        "litellm_request",
        "raw_gen_ai_request",
    ]

    if not any(name.startswith(p) or name == p for p in main_patterns):
        return False

    # Check input for known ancillary patterns that should be excluded
    # These are internal prompts for safety/routing checks, not user messages
    input_val = _extract_input_value(span)

    # Ancillary input patterns to exclude
    ancillary_input_patterns = [
        '<policy_spec>',          # Command prefix extraction prompt
        'Claude Code Code Bash command prefix detection',  # Haiku safety check
    ]

    for pattern in ancillary_input_patterns:
        if pattern in input_val:
            return False

    # Check output for known ancillary patterns that should be excluded
    # These are internal routing/classification responses, not user-visible conversation
    output_val = _extract_output_value(span)

    # Ancillary output patterns to exclude
    ancillary_output_patterns = [
        '"isNewTopic"',           # Topic detection
        '"is_displaying_contents"',  # Content display check
        '<is_displaying_contents>',  # XML variant
    ]

    for pattern in ancillary_output_patterns:
        if pattern in output_val:
            return False

    # Check for very short outputs that are just routing signals
    # e.g., output is just "#" (quota check) or "{" (delegation)
    stripped_output = output_val.strip()
    if stripped_output in ('#', '{', '[{"type": "text", "text": "{"}]'):
        return False

    # Filter out Haiku safety check responses
    # These are very short responses (typically just echoing a command) from Haiku
    # that are used for bash command safety validation
    model = _extract_model(span)
    if model and 'haiku' in model.lower():
        # Haiku responses that are just short command fragments or validation outputs
        # These appear between Opus assistant messages as safety checks
        if len(stripped_output) < 50 and not output_val.startswith('['):
            # Very short output from Haiku that's not a JSON array - likely a safety check
            return False

    return True


# System prompt markers that indicate subagent LLM calls
# These are specialized prompts given to subagents for specific tasks
SUBAGENT_SYSTEM_MARKERS = [
    "file search specialist",  # Explore subagent
    "READ-ONLY exploration task",  # Explore subagent
    "READ-ONLY MODE - NO FILE MODIFICATIONS",  # Explore subagent
    "You excel at thoroughly navigating and exploring codebases",  # Explore subagent
    "general-purpose agent",  # General-purpose subagent
    "research complex questions",  # General-purpose subagent
]


def _is_subagent_llm_span(span: dict[str, Any]) -> bool:
    """
    Check if an LLM span is from a subagent execution (not the main thread).

    Subagent LLM calls are identified by specialized system prompts that include
    markers like "file search specialist" (Explore subagent) or role-specific
    instructions that differ from the main Claude Code system prompt.

    This is used to prevent subagent prompts from being displayed as "👤 User"
    messages in markdown exports.

    Args:
        span: A span dictionary with raw_attributes or raw_attributes_json.

    Returns:
        True if this span appears to be a subagent LLM call.
    """
    # Try both field names - raw_attributes_json is the original, but
    # parquet_query.py renames it to raw_attributes after parsing
    raw_attrs = span.get("raw_attributes") or span.get("raw_attributes_json")
    if not raw_attrs:
        return False

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Extract system prompt from llm.None.system path
        llm_attrs = attrs.get("attributes", {}).get("llm", {}).get("None", {})
        system_prompt = str(llm_attrs.get("system", ""))

        # Check for subagent markers in the system prompt
        for marker in SUBAGENT_SYSTEM_MARKERS:
            if marker in system_prompt:
                return True

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return False


def _has_meaningful_user_input(input_val: str) -> bool:
    """
    Check if input contains meaningful user content (not just system/tool data).

    A "meaningful" user input is one that:
    - Has text content (not empty or whitespace)
    - Is not purely tool_result blocks
    - Is not just a continuation marker
    """
    if not input_val or not input_val.strip():
        return False

    # If it's a continuation marker, it's not meaningful user input
    if COMPACTION_CONTINUATION_MARKER in input_val:
        return False

    # Try to parse as JSON to check for user role messages
    try:
        parsed = json.loads(input_val)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    # Check for actual user text content
                    if item.get("type") == "text":
                        text = item.get("text", "").strip()
                        if text and len(text) > 10:  # Non-trivial text
                            return True
                    # tool_result blocks are not user input
                    elif item.get("type") == "tool_result":
                        continue
            return False
    except json.JSONDecodeError:
        pass

    # Plain text - check if it's substantive
    return len(input_val.strip()) > 10


def _detect_mid_stream_start(
    chain: ConversationChain,
    session_lookup: dict[str, dict[str, Any]],
) -> bool:
    """
    Detect if a chain starts mid-stream (assistant output before user input).

    This happens when:
    1. The first session has no compaction continuation marker
    2. The first main thread span with output has no meaningful user input preceding it

    Returns True if the chain appears to start mid-stream.
    """
    if not chain.session_ids:
        return False

    # Get first session
    first_session_id = chain.session_ids[0]
    first_session = session_lookup.get(first_session_id, {})
    spans = first_session.get("spans", [])

    if not spans:
        return False

    # Sort spans by start time
    sorted_spans = sorted(
        spans,
        key=lambda s: _parse_timestamp(s.get("start_time")) or datetime.min
    )

    # Find first main thread span with actual output (assistant message)
    first_output_span = None
    for span in sorted_spans:
        if not _is_main_thread_span(span):
            continue
        output_val = _extract_output_value(span)
        if output_val and output_val.strip():
            first_output_span = span
            break

    if not first_output_span:
        return False

    # Find first main thread span with meaningful user input
    first_user_input_span = None
    for span in sorted_spans:
        if not _is_main_thread_span(span):
            continue
        input_val = _extract_input_value(span)
        if _has_meaningful_user_input(input_val):
            first_user_input_span = span
            break

    # Mid-stream if there's output but no meaningful user input,
    # OR if the first output appears before the first user input
    if not first_user_input_span:
        return True  # Output but no user input at all

    output_time = _parse_timestamp(first_output_span.get("start_time"))
    input_time = _parse_timestamp(first_user_input_span.get("start_time"))

    if output_time and input_time:
        return output_time < input_time  # Output appears before user input

    return False


def _parse_message_content(content: str) -> list[dict[str, Any]]:
    """
    Parse message content which may be JSON array of message blocks.

    Content may be stored as JSON (double quotes) or Python literals (single quotes).
    Returns list of message dictionaries with 'type' and 'text' keys.
    """
    if not content:
        return []

    parsed = None

    # Try to parse as JSON array first
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        pass

    # If JSON fails, try Python literal (handles single quotes)
    if parsed is None:
        try:
            parsed = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            pass

    # Process the parsed content if successful
    if isinstance(parsed, list):
        # Extract text from message blocks
        messages = []
        for item in parsed:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    messages.append({"type": "text", "text": item.get("text", "")})
                elif item.get("type") == "tool_use":
                    tool_name = item.get("name", "unknown")
                    tool_input = item.get("input", {})
                    tool_id = item.get("id", "")
                    messages.append({
                        "type": "tool_use",
                        "tool": tool_name,
                        "input": tool_input,
                        "id": tool_id,
                    })
                elif item.get("type") == "tool_result":
                    messages.append({
                        "type": "tool_result",
                        "content": item.get("content", ""),
                    })
        return messages

    # Return as plain text if parsing failed
    return [{"type": "text", "text": content}]


def _format_user_message(text: str, max_length: int = 5000) -> str:
    """Format a user message for markdown output."""
    # Truncate very long messages
    if len(text) > max_length:
        text = text[:max_length] + "\n\n... [truncated]"
    return text


def _format_assistant_message(text: str, max_length: int = 10000) -> str:
    """Format an assistant message for markdown output."""
    # Truncate very long messages
    if len(text) > max_length:
        text = text[:max_length] + "\n\n... [truncated]"
    return text


def _format_tool_input(tool_name: str, tool_input: dict[str, Any], max_length: int = 2000) -> str:
    """
    Format tool input for markdown display.

    Returns a formatted string showing the tool's key parameters.
    """
    lines: list[str] = []

    if tool_name == "Bash":
        # Show the command being run
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            lines.append(f"> {desc}")
        if cmd:
            # Truncate very long commands
            if len(cmd) > max_length:
                cmd = cmd[:max_length] + "... [truncated]"
            lines.append(f"> ```bash\n> {cmd}\n> ```")

    elif tool_name == "Read":
        # Show file being read
        path = tool_input.get("file_path", "")
        lines.append(f"> File: `{path}`")
        if tool_input.get("offset"):
            lines.append(f"> Lines: {tool_input.get('offset')}-{tool_input.get('offset', 0) + tool_input.get('limit', 0)}")

    elif tool_name == "Write":
        # Show file being written
        path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        lines.append(f"> File: `{path}`")
        if content:
            preview = content[:200] + "..." if len(content) > 200 else content
            lines.append(f"> Content preview: {len(content)} chars")

    elif tool_name == "Edit":
        # Show file and what's being changed
        path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")
        lines.append(f"> File: `{path}`")
        if old:
            # Show first few lines of what's being replaced
            old_preview = old[:500] if len(old) > 500 else old
            if len(old) > 500:
                old_preview += "..."
            lines.append(f"> Replacing:")
            lines.append(f"> ```")
            for line in old_preview.split("\n")[:10]:
                lines.append(f"> {line}")
            if old.count("\n") > 10:
                lines.append(f"> ... ({old.count(chr(10)) - 10} more lines)")
            lines.append(f"> ```")

    elif tool_name == "Glob":
        # Show pattern
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", ".")
        lines.append(f"> Pattern: `{pattern}` in `{path}`")

    elif tool_name == "Grep":
        # Show search pattern
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", ".")
        lines.append(f"> Search: `{pattern}` in `{path}`")

    elif tool_name == "TodoWrite":
        # Show todo items
        todos = tool_input.get("todos", [])
        if todos:
            lines.append(f"> {len(todos)} todo items")
            for todo in todos[:5]:  # Show first 5
                status = todo.get("status", "pending")
                content = todo.get("content", "")[:50]
                lines.append(f">   - [{status}] {content}")
            if len(todos) > 5:
                lines.append(f">   ... and {len(todos) - 5} more")

    elif tool_name == "WebFetch":
        # Show URL
        url = tool_input.get("url", "")
        lines.append(f"> URL: {url}")

    elif tool_name == "WebSearch":
        # Show query
        query = tool_input.get("query", "")
        lines.append(f"> Query: `{query}`")

    elif tool_name.startswith("mcp__"):
        # MCP tool - show key parameters
        for key, value in list(tool_input.items())[:3]:
            if isinstance(value, str) and len(value) > 100:
                value = value[:100] + "..."
            lines.append(f"> {key}: `{value}`")

    else:
        # Generic: show first few parameters
        for key, value in list(tool_input.items())[:3]:
            if isinstance(value, str):
                if len(value) > 100:
                    value = value[:100] + "..."
                lines.append(f"> {key}: `{value}`")
            elif isinstance(value, (int, float, bool)):
                lines.append(f"> {key}: `{value}`")

    return "\n".join(lines) if lines else ""


def _format_tool_result(result_content: Any, max_length: int = 1500) -> str:
    """
    Format tool result for markdown display.

    Returns a truncated, formatted string of the result.
    """
    if not result_content:
        return ""

    # Handle different result formats
    if isinstance(result_content, str):
        text = result_content
    elif isinstance(result_content, list):
        # May be a list of content blocks
        texts = []
        for item in result_content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif isinstance(item, str):
                texts.append(item)
        text = "\n".join(texts)
    elif isinstance(result_content, dict):
        if result_content.get("type") == "text":
            text = result_content.get("text", "")
        else:
            text = json.dumps(result_content, indent=2)
    else:
        text = str(result_content)

    # Truncate
    if len(text) > max_length:
        text = text[:max_length] + "... [truncated]"

    # Format as blockquote
    lines = text.split("\n")[:25]  # Max 25 lines
    result = "\n".join(f"> {line}" for line in lines)
    if text.count("\n") > 25:
        result += f"\n> ... ({text.count(chr(10)) - 25} more lines)"
    return result


@dataclass
class SubagentExport:
    """Data for a subagent's separate markdown file."""

    tool_use_id: str
    subagent_type: str
    prompt: str
    spans: list[dict[str, Any]]
    parent_session_id: str
    tool_result: Any = None  # The result returned by the subagent


# Size threshold for inline vs file (chars)
SMALL_RESULT_THRESHOLD = 500


@dataclass
class ToolCallExport:
    """Data for a tool call's separate markdown file."""

    tool_use_id: str
    tool_number: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_result: Any
    result_size: int  # Size in chars for classification
    summary: str  # Short summary for inline display

    @property
    def is_small(self) -> bool:
        """Whether this result is small enough to inline."""
        return self.result_size <= SMALL_RESULT_THRESHOLD


def _generate_tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Generate a short summary of a tool call for inline display."""
    if tool_name == "Bash":
        desc = tool_input.get("description", "")
        cmd = tool_input.get("command", "")[:60]
        return desc if desc else cmd
    elif tool_name == "Read":
        path = tool_input.get("file_path", "")
        return f"Read {path.split('/')[-1] if path else 'file'}"
    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        return f"Write {path.split('/')[-1] if path else 'file'}"
    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        return f"Edit {path.split('/')[-1] if path else 'file'}"
    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"Glob {pattern}"
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"Search '{pattern[:30]}'"
    elif tool_name == "TodoWrite":
        todos = tool_input.get("todos", [])
        return f"{len(todos)} todo items"
    elif tool_name == "BashOutput":
        return "Check command output"
    elif tool_name == "KillShell":
        return "Kill background process"
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return f"Fetch {url[:40]}..."
    elif tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return f"Search '{query[:30]}'"
    elif tool_name == "Task":
        subagent = tool_input.get("subagent_type", "unknown")
        return f"Subagent: {subagent}"
    elif tool_name.startswith("mcp__"):
        # MCP tools - extract meaningful params
        parts = tool_name.split("__")
        short_name = parts[-1] if len(parts) > 1 else tool_name
        return short_name.replace("_", " ")
    else:
        # Generic - show first param
        params = list(tool_input.keys())[:1]
        return f"{tool_name}({', '.join(params)})"


def generate_tool_call_markdown(
    tool_call: ToolCallExport,
    parent_chain_id: str,
) -> str:
    """
    Generate markdown content for a tool call's detail file.

    Args:
        tool_call: The ToolCallExport data.
        parent_chain_id: The parent chain's ID for reference.

    Returns:
        Markdown content as a string.
    """
    lines: list[str] = []

    lines.append(f"# Tool Call #{tool_call.tool_number}: {tool_call.tool_name}")
    lines.append("")
    lines.append(f"**Summary**: {tool_call.summary}")
    lines.append(f"**Result Size**: {tool_call.result_size:,} chars")
    lines.append(f"**Tool Use ID**: `{tool_call.tool_use_id}`")
    lines.append("")

    # Input section
    lines.append("## Input")
    lines.append("")
    lines.append("```json")
    input_json = json.dumps(tool_call.tool_input, indent=2, default=str)
    # Truncate very large inputs
    if len(input_json) > 5000:
        input_json = input_json[:5000] + "\n... [truncated]"
    lines.append(input_json)
    lines.append("```")
    lines.append("")

    # Result section
    lines.append("## Result")
    lines.append("")
    result_str = str(tool_call.tool_result) if tool_call.tool_result else "(no result)"
    # Don't truncate result in detail file - that's the point
    lines.append("```")
    lines.append(result_str)
    lines.append("```")
    lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Part of chain {parent_chain_id[:8]}...*")

    return "\n".join(lines)


@dataclass
class MarkdownExportResult:
    """Result of markdown export including main content and subagent data."""

    main_content: str
    subagents: list[SubagentExport]
    tool_calls: list[ToolCallExport]
    metrics: dict[str, Any]


def generate_subagent_markdown(
    subagent: SubagentExport,
    parent_chain_id: str,
    max_message_length: int = 10000,
) -> str:
    """
    Generate markdown content for a subagent's conversation.

    Args:
        subagent: The SubagentExport data.
        parent_chain_id: The parent chain's ID for reference.
        max_message_length: Maximum length for individual messages.

    Returns:
        Markdown content as a string.
    """
    lines: list[str] = []

    # Header
    lines.append(f"# Subagent: {subagent.subagent_type}")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- **Type**: {subagent.subagent_type}")
    lines.append(f"- **Tool Use ID**: `{subagent.tool_use_id}`")
    lines.append(f"- **Parent Chain**: {parent_chain_id[:8]}...")
    lines.append(f"- **Parent Session**: {subagent.parent_session_id[:8]}...")
    lines.append("")

    # Task prompt
    lines.append("## Task")
    lines.append("")
    prompt_text = subagent.prompt
    if len(prompt_text) > max_message_length:
        prompt_text = prompt_text[:max_message_length] + "\n\n... [truncated]"
    lines.append(prompt_text)
    lines.append("")

    # Subagent conversation
    if subagent.spans:
        lines.append("---")
        lines.append("")
        lines.append("## Conversation")
        lines.append("")

        # Sort spans by start time
        sorted_spans = sorted(
            subagent.spans,
            key=lambda s: _parse_timestamp(s.get("start_time")) or datetime.min
        )

        for span in sorted_spans:
            if not _is_main_thread_span(span):
                continue

            input_val = _extract_input_value(span)
            output_val = _extract_output_value(span)
            model = _extract_model(span)

            # Parse messages
            input_messages = _parse_message_content(input_val)
            output_messages = _parse_message_content(output_val)

            # Show input
            for msg in input_messages:
                if msg["type"] == "text" and msg.get("text", "").strip():
                    text = msg["text"].strip()
                    if text and not text.startswith("<system") and len(text) > 10:
                        lines.append("### 📋 Task Input")
                        lines.append("")
                        lines.append(_format_user_message(text, max_message_length))
                        lines.append("")

            # Show output
            for msg in output_messages:
                if msg["type"] == "text" and msg.get("text", "").strip():
                    text = msg["text"].strip()
                    if text:
                        model_label = f" ({model})" if model else ""
                        lines.append(f"### 🤖 Subagent{model_label}")
                        lines.append("")
                        lines.append(_format_assistant_message(text, max_message_length))
                        lines.append("")
    else:
        # No span data available, but we might have the tool_result
        lines.append("---")
        lines.append("")

        if subagent.tool_result:
            # Show the subagent's response from the tool_result
            lines.append("## Response")
            lines.append("")

            # Parse the tool_result - it might be a list with text content
            result_content = subagent.tool_result
            if isinstance(result_content, list):
                for item in result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            if len(text) > max_message_length:
                                text = text[:max_message_length] + "\n\n... [truncated]"
                            lines.append(text)
                            lines.append("")
            elif isinstance(result_content, str):
                text = result_content
                if len(text) > max_message_length:
                    text = text[:max_message_length] + "\n\n... [truncated]"
                lines.append(text)
                lines.append("")
            else:
                # Fallback: convert to string
                text = str(result_content)
                if len(text) > max_message_length:
                    text = text[:max_message_length] + "\n\n... [truncated]"
                lines.append(text)
                lines.append("")
        else:
            lines.append("*No detailed conversation data available for this subagent.*")
            lines.append("")

    # Footer
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Subagent of chain {parent_chain_id[:8]}...*")

    return "\n".join(lines)


def export_chain_to_markdown(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
    include_tool_calls: bool = True,
    include_metadata: bool = True,
    max_message_length: int = 10000,
    output_basename: str | None = None,
    scaffolded: bool = True,
) -> MarkdownExportResult:
    """
    Export a conversation chain to readable markdown format.

    Args:
        chain: The ConversationChain to export.
        sessions: List of all session dictionaries.
        include_tool_calls: Whether to include tool call details.
        include_metadata: Whether to include session/span metadata.
        max_message_length: Maximum length for individual messages.
        output_basename: Base name for subagent/tool file links (e.g., "chain_abc").
        scaffolded: If True (default), tool calls are summarized inline with full
            details in separate files. If False, tool calls are expanded inline.

    Returns:
        MarkdownExportResult with main content, subagent data, tool calls, and metrics.
    """
    session_lookup = {s.get("session_id", ""): s for s in sessions}

    lines: list[str] = []
    subagents: list[SubagentExport] = []
    tool_calls: list[ToolCallExport] = []
    tool_call_number = 0  # Global counter for tool calls

    # Track metrics as we process
    main_thread_turns = 0
    ancillary_turns = 0
    model_counts: dict[str, int] = {}
    total_tokens_prompt = 0
    total_tokens_completion = 0

    # Build span lookup for subagent content extraction
    # Maps tool_use_id to the Claude_Code_Tool_Task span and its children
    task_spans_by_id: dict[str, dict[str, Any]] = {}
    all_spans_by_parent: dict[str, list[dict[str, Any]]] = {}

    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            parent_id = span.get("parent_id")
            if parent_id:
                if parent_id not in all_spans_by_parent:
                    all_spans_by_parent[parent_id] = []
                all_spans_by_parent[parent_id].append(span)

            # Track Claude_Code_Tool_Task spans
            if span.get("name") == "Claude_Code_Tool_Task":
                raw_attrs = span.get("raw_attributes_json")
                if raw_attrs:
                    try:
                        attrs = json.loads(raw_attrs) if isinstance(raw_attrs, str) else raw_attrs
                        input_data = attrs.get("attributes", {}).get("input", {}).get("value", "")
                        if input_data:
                            tool_data = json.loads(input_data) if isinstance(input_data, str) else input_data
                            tool_id = tool_data.get("id")
                            if tool_id:
                                task_spans_by_id[tool_id] = {
                                    "span": span,
                                    "tool_data": tool_data,
                                    "session_id": session_id,
                                }
                    except (json.JSONDecodeError, TypeError, KeyError):
                        pass

    # Build set of trace IDs that belong to subagent executions
    # Subagent traces have raw_gen_ai_request spans with subagent-specific system prompts
    subagent_trace_ids: set[str] = set()
    # Also build mapping from tool_use_id to trace_id for subagent extraction
    tool_id_to_trace_id: dict[str, str] = {}

    # Build a reverse index: span_id -> list of child spans for efficient lookup
    span_children_map: dict[str, list[dict[str, Any]]] = {}
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            parent_id = span.get("parent_id")
            if parent_id:
                if parent_id not in span_children_map:
                    span_children_map[parent_id] = []
                span_children_map[parent_id].append(span)

    # Debug: track mapping attempts
    import os
    debug_enabled = os.environ.get("DAL_DEBUG_SUBAGENT") == "1"
    mapping_attempts = 0
    successful_mappings = 0

    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            if span.get("name") == "raw_gen_ai_request" and _is_subagent_llm_span(span):
                trace_id = span.get("trace_id")
                if trace_id:
                    subagent_trace_ids.add(trace_id)
                    mapping_attempts += 1

                    # Link this trace to a Task tool_use_id by finding the Task span
                    # The raw_gen_ai_request span's parent should be within the Task execution tree
                    # Walk up the parent chain to find Claude_Code_Tool_Task
                    current_span = span
                    found_task = False
                    for depth in range(15):  # Limit depth to avoid infinite loops
                        parent_id = current_span.get("parent_id")
                        if not parent_id:
                            if debug_enabled:
                                print(f"  Depth {depth}: No parent_id, stopping")
                            break

                        # Find parent span
                        parent_span = None
                        for check_span in session.get("spans", []):
                            if check_span.get("span_id") == parent_id:
                                parent_span = check_span
                                break

                        if not parent_span:
                            if debug_enabled:
                                print(f"  Depth {depth}: Parent span not found: {parent_id}")
                            break

                        if debug_enabled and depth < 5:
                            print(f"  Depth {depth}: {parent_span.get('name')}")

                        # Check if this is the Task span
                        if parent_span.get("name") == "Claude_Code_Tool_Task":
                            # Extract tool_use_id from Task span
                            raw_attrs = parent_span.get("raw_attributes_json")
                            if raw_attrs:
                                try:
                                    attrs = json.loads(raw_attrs) if isinstance(raw_attrs, str) else raw_attrs
                                    input_data = attrs.get("attributes", {}).get("input", {}).get("value", "")
                                    if input_data:
                                        tool_data = json.loads(input_data) if isinstance(input_data, str) else input_data
                                        tool_id = tool_data.get("id")
                                        if tool_id and tool_id not in tool_id_to_trace_id:
                                            tool_id_to_trace_id[tool_id] = trace_id
                                            successful_mappings += 1
                                            found_task = True
                                            if debug_enabled:
                                                print(f"  SUCCESS: Mapped {tool_id[:20]} -> {trace_id[:20]}")
                                except (json.JSONDecodeError, TypeError, KeyError) as e:
                                    if debug_enabled:
                                        print(f"  ERROR parsing Task attrs: {e}")
                                    pass
                            break

                        # Move up to parent
                        current_span = parent_span

                    if debug_enabled and not found_task:
                        print(f"  FAILED to find Task span for trace {trace_id[:20]}")

    if debug_enabled:
        print(f"\nSubagent mapping summary:")
        print(f"  Mapping attempts: {mapping_attempts}")
        print(f"  Successful mappings: {successful_mappings}")
        print(f"  tool_id_to_trace_id size: {len(tool_id_to_trace_id)}")

    # Header
    lines.append(f"# Conversation: {chain.chain_id[:8]}...")
    lines.append("")

    # Process each session to gather metrics first
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        for span in session.get("spans", []):
            if _is_user_input_span(span):
                input_val = _extract_input_value(span)
                if COMPACTION_CONTINUATION_MARKER in input_val or COMPACTION_TASK_MARKER in input_val:
                    continue

                classification = classify_span_thread({
                    **span,
                    "input_value": input_val,
                    "output_value": _extract_output_value(span),
                })
                if classification.thread_type.value == "main_thread":
                    main_thread_turns += 1
                else:
                    ancillary_turns += 1

                model = _extract_model(span)
                if model:
                    model_counts[model] = model_counts.get(model, 0) + 1

                tokens_prompt = span.get("llm_token_count_prompt") or 0
                tokens_completion = span.get("llm_token_count_completion") or 0
                total_tokens_prompt += tokens_prompt
                total_tokens_completion += tokens_completion

    # Metrics dict
    metrics = {
        "duration_seconds": int(chain.duration_minutes * 60) if chain.duration_minutes else None,
        "total_turns": main_thread_turns + ancillary_turns,
        "main_thread_turns": main_thread_turns,
        "ancillary_turns": ancillary_turns,
        "subagent_count": len(task_spans_by_id),
        "models_used": model_counts,
        "tokens": {
            "prompt": total_tokens_prompt,
            "completion": total_tokens_completion,
            "total": total_tokens_prompt + total_tokens_completion,
        },
    }

    if include_metadata:
        lines.append("## Metadata")
        lines.append("")
        lines.append(f"- **Sessions**: {chain.session_count}")
        lines.append(f"- **Compactions**: {chain.compaction_count}")
        if chain.start_time:
            lines.append(f"- **Started**: {chain.start_time.isoformat()}")
        if chain.end_time:
            lines.append(f"- **Ended**: {chain.end_time.isoformat()}")
        lines.append(f"- **Duration**: {chain.duration_minutes:.1f} minutes")
        lines.append(f"- **Main thread turns**: {main_thread_turns}")
        lines.append(f"- **Ancillary turns**: {ancillary_turns}")
        lines.append(f"- **Subagents spawned**: {len(task_spans_by_id)}")
        if model_counts:
            models_str = ", ".join(f"{m}: {c}" for m, c in model_counts.items())
            lines.append(f"- **Models**: {models_str}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Conversation")
    lines.append("")

    # Check if chain starts mid-stream and add warning note
    if _detect_mid_stream_start(chain, session_lookup):
        lines.append("> **Note:** This conversation continues from earlier context not captured in this export.")
        lines.append("> The original user request that prompted this conversation is not available.")
        lines.append("")

    # Track seen messages to deduplicate across compaction boundaries
    # Use a hash of the first 500 chars to detect duplicates
    seen_messages: set[str] = set()

    def _message_key(text: str) -> str:
        """Generate a key for deduplication (first 500 chars, normalized)."""
        return text[:500].strip().lower()

    # Build tool_results mapping: tool_use_id -> result content
    # Tool results appear in the INPUT of subsequent spans as tool_result messages
    tool_results: dict[str, Any] = {}
    for session_id_for_results in chain.session_ids:
        session_for_results = session_lookup.get(session_id_for_results, {})
        for span in session_for_results.get("spans", []):
            input_val = _extract_input_value(span)
            if not input_val:
                continue
            try:
                parsed = json.loads(input_val)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and item.get("type") == "tool_result":
                            tool_use_id = item.get("tool_use_id")
                            content = item.get("content")
                            if tool_use_id and content:
                                tool_results[tool_use_id] = content
            except json.JSONDecodeError:
                pass

    # Process each session in the chain
    compaction_count = 0  # Track actual compactions with content
    for session_idx, session_id in enumerate(chain.session_ids):
        session = session_lookup.get(session_id, {})
        spans = session.get("spans", [])

        # Track whether we've added the compaction marker for this session
        # We only add it when we find actual content to display
        compaction_marker_added = False

        def _maybe_add_compaction_marker() -> None:
            """Add compaction marker before first content in a continuation session."""
            nonlocal compaction_marker_added, compaction_count
            if session_idx > 0 and not compaction_marker_added:
                compaction_count += 1
                lines.append("")
                lines.append("---")
                lines.append(f"### 🔄 Compaction #{compaction_count}")
                lines.append("*Session continued after context window limit*")
                lines.append("")
                lines.append("---")
                lines.append("")
                compaction_marker_added = True

        # Sort spans by start time
        sorted_spans = sorted(
            spans,
            key=lambda s: _parse_timestamp(s.get("start_time")) or datetime.min
        )

        # Extract main thread spans with user/assistant messages
        for span in sorted_spans:
            if not _is_main_thread_span(span):
                continue

            # Skip subagent spans - their prompts are from the Task tool,
            # not real user input. Subagent content is captured separately via
            # SubagentExport when processing the Task tool_use in the output.
            # Check both: 1) direct subagent detection via system prompt, and
            # 2) whether this span is in a subagent trace (for Internal_Prompt spans)
            span_trace_id = span.get("trace_id")
            if _is_subagent_llm_span(span) or span_trace_id in subagent_trace_ids:
                continue

            input_val = _extract_input_value(span)
            output_val = _extract_output_value(span)
            model = _extract_model(span)

            # Handle compaction continuation - show summary AND process the assistant output
            if COMPACTION_CONTINUATION_MARKER in input_val:
                # Extract the full summary for context
                if "The conversation is summarized below:" in input_val:
                    _maybe_add_compaction_marker()
                    summary_start = input_val.find("The conversation is summarized below:")
                    # Get the full summary text (no truncation)
                    summary_text = input_val[summary_start:]
                    lines.append("> **Previous Context Summary**")
                    lines.append(">")
                    for line in summary_text.split("\n"):
                        # Strip any truncation markers that might exist in raw data
                        clean_line = line.replace("[summary continues]", "").rstrip()
                        lines.append(f"> {clean_line}")
                    lines.append("")

                # Process the OUTPUT of this span (assistant's continuation message + tools)
                # Don't skip - fall through to output processing below
                # Clear input_messages so we don't show the compaction marker as user input
                input_messages = []
                output_messages = _parse_message_content(output_val)

                # Show assistant output from continuation
                assistant_text_shown = False
                for msg in output_messages:
                    if msg["type"] == "text" and msg.get("text", "").strip():
                        text = msg["text"].strip()
                        if text:
                            msg_key = _message_key(text)
                            if msg_key in seen_messages:
                                continue
                            seen_messages.add(msg_key)
                            _maybe_add_compaction_marker()
                            model_label = f" ({model})" if model else ""
                            lines.append(f"### 🤖 Assistant{model_label}")
                            lines.append("")
                            lines.append(_format_assistant_message(text, max_message_length))
                            lines.append("")
                            assistant_text_shown = True
                    elif msg["type"] == "tool_use" and include_tool_calls:
                        tool = msg.get("tool", "unknown")
                        tool_id = msg.get("id", "")
                        tool_input = msg.get("input", {})
                        tool_result = tool_results.get(tool_id, None) if tool_id else None
                        result_size = len(str(tool_result)) if tool_result else 0
                        summary = _generate_tool_summary(tool, tool_input)
                        tool_call_number += 1

                        if scaffolded:
                            existing_ids = {tc.tool_use_id for tc in tool_calls}
                            if tool_id and tool_id not in existing_ids:
                                tool_calls.append(ToolCallExport(
                                    tool_use_id=tool_id,
                                    tool_number=tool_call_number,
                                    tool_name=tool,
                                    tool_input=tool_input,
                                    tool_result=tool_result,
                                    result_size=result_size,
                                    summary=summary,
                                ))

                            _maybe_add_compaction_marker()
                            if result_size <= SMALL_RESULT_THRESHOLD:
                                lines.append(f"> 🔧 **#{tool_call_number} {tool}**: {summary}")
                                if tool_result:
                                    result_preview = str(tool_result)[:200].replace("\n", " ")
                                    lines.append(f">   → `{result_preview}`")
                            else:
                                # Large result: show link if basename provided, else show inline
                                if output_basename and tool_id:
                                    filename = f"tool_calls/{tool_call_number:03d}_{tool_id[:8]}.md"
                                    lines.append(f"> 🔧 **#{tool_call_number} {tool}**: {summary} → [details](./{filename})")
                                else:
                                    # No basename - fall back to inline display
                                    lines.append(f"> 🔧 **#{tool_call_number} {tool}**: {summary}")
                                    if tool_result:
                                        lines.append(_format_tool_result(tool_result))
                            lines.append("")
                        else:
                            _maybe_add_compaction_marker()
                            lines.append(f"**🔧 Tool: {tool}**")
                            lines.append(_format_tool_input(tool, tool_input))
                            if tool_result:
                                lines.append(_format_tool_result(tool_result))
                            lines.append("")

                continue  # Skip normal input/output processing since we handled it above

            # Skip compaction task spans
            if COMPACTION_TASK_MARKER in input_val:
                continue

            # Parse and format messages
            input_messages = _parse_message_content(input_val)
            output_messages = _parse_message_content(output_val)

            # Show user input (if not empty/tool result)
            for msg in input_messages:
                if msg["type"] == "text" and msg.get("text", "").strip():
                    text = msg["text"].strip()
                    # Handle warmup/initialization messages FIRST (before length filter)
                    # "Warmup" is only 6 chars but is a valid session init message
                    if text == "Warmup" or text.startswith('"Warmup"'):
                        _maybe_add_compaction_marker()
                        lines.append("### 👤 User")
                        lines.append("")
                        lines.append("*[Session initialization]*")
                        lines.append("")
                        continue
                    # Skip very short messages (likely noise, but check after warmup)
                    if not text or len(text) <= 10:
                        continue
                    # Skip system reminder blocks
                    if text.startswith("<system"):
                        continue
                    # Skip tool results being echoed back (Command: ... Output: ...)
                    if text.startswith("Command:") and "\nOutput:" in text:
                        continue
                    # Skip JSON-like tool results
                    if text.startswith("{") and text.endswith("}"):
                        continue

                    # Deduplicate: skip if we've seen this message before
                    msg_key = _message_key(text)
                    if msg_key in seen_messages:
                        continue
                    seen_messages.add(msg_key)

                    _maybe_add_compaction_marker()
                    lines.append("### 👤 User")
                    lines.append("")
                    lines.append(_format_user_message(text, max_message_length))
                    lines.append("")

            # Show assistant output
            assistant_text_shown = False
            for msg in output_messages:
                if msg["type"] == "text" and msg.get("text", "").strip():
                    text = msg["text"].strip()
                    if text:
                        # Deduplicate: skip if we've seen this message before
                        msg_key = _message_key(text)
                        if msg_key in seen_messages:
                            continue
                        seen_messages.add(msg_key)

                        _maybe_add_compaction_marker()
                        model_label = f" ({model})" if model else ""
                        lines.append(f"### 🤖 Assistant{model_label}")
                        lines.append("")
                        lines.append(_format_assistant_message(text, max_message_length))
                        lines.append("")
                        assistant_text_shown = True
                elif msg["type"] == "tool_use":
                    tool = msg.get("tool", "unknown")
                    tool_id = msg.get("id", "")

                    # Check if this is a Task (subagent) tool call
                    if tool == "Task" and tool_id:
                        subagent_info = msg.get("input", {})
                        subagent_type = subagent_info.get("subagent_type", "unknown")
                        prompt = subagent_info.get("prompt", "")[:200]

                        # Get subagent spans from the full trace, not just Task span children
                        subagent_spans = []
                        subagent_trace_id = tool_id_to_trace_id.get(tool_id)
                        if subagent_trace_id:
                            # Collect all spans with this trace_id from all sessions
                            for check_session_id in chain.session_ids:
                                check_session = session_lookup.get(check_session_id, {})
                                for span in check_session.get("spans", []):
                                    if span.get("trace_id") == subagent_trace_id:
                                        subagent_spans.append(span)

                        # Create subagent export data (deduplicate by tool_use_id)
                        # Same subagent may appear multiple times due to compaction
                        existing_ids = {s.tool_use_id for s in subagents}
                        if tool_id not in existing_ids:
                            # Get the tool result for this subagent
                            subagent_result = tool_results.get(tool_id)
                            subagents.append(SubagentExport(
                                tool_use_id=tool_id,
                                subagent_type=subagent_type,
                                prompt=subagent_info.get("prompt", ""),
                                spans=subagent_spans,
                                parent_session_id=session_id,
                                tool_result=subagent_result,
                            ))

                        # Add link in main markdown with cleaner filename
                        _maybe_add_compaction_marker()
                        if output_basename:
                            # Use a cleaner filename based on subagent type and index
                            # Count how many subagents of this type we've seen
                            subagent_type_count = len([s for s in subagents if s.subagent_type == subagent_type])
                            safe_type = subagent_type.lower().replace(' ', '_').replace('-', '_')
                            filename = f"{output_basename}_subagent_{safe_type}_{subagent_type_count}.md"
                            lines.append(f"> 📦 **Subagent**: {subagent_type} - [{filename}](./{filename})")
                        else:
                            lines.append(f"> 📦 **Subagent**: {subagent_type}")
                        lines.append(f'> *"{prompt}..."*')
                        lines.append("")

                    elif include_tool_calls:
                        tool_input = msg.get("input", {})
                        tool_result = tool_results.get(tool_id, None) if tool_id else None
                        result_size = len(str(tool_result)) if tool_result else 0
                        summary = _generate_tool_summary(tool, tool_input)

                        # Increment tool number and track
                        tool_call_number += 1

                        if scaffolded:
                            # Create tool call export (deduplicate by tool_use_id)
                            existing_ids = {t.tool_use_id for t in tool_calls}
                            if tool_id and tool_id not in existing_ids:
                                tool_calls.append(ToolCallExport(
                                    tool_use_id=tool_id,
                                    tool_number=tool_call_number,
                                    tool_name=tool,
                                    tool_input=tool_input,
                                    tool_result=tool_result,
                                    result_size=result_size,
                                    summary=summary,
                                ))

                            # Compact inline reference
                            _maybe_add_compaction_marker()
                            if result_size <= SMALL_RESULT_THRESHOLD:
                                # Small result: show inline
                                lines.append(f"> 🔧 **#{tool_call_number} {tool}**: {summary}")
                                if tool_result:
                                    result_preview = str(tool_result)[:200]
                                    if len(str(tool_result)) > 200:
                                        result_preview += "..."
                                    lines.append(f">   → `{result_preview}`")
                                lines.append("")
                            else:
                                # Large result: show link if basename provided, else show inline
                                if output_basename and tool_id:
                                    filename = f"tool_calls/{tool_call_number:03d}_{tool_id[:8]}.md"
                                    lines.append(f"> 🔧 **#{tool_call_number} {tool}**: {summary} → [details](./{filename})")
                                else:
                                    # No basename - fall back to inline display
                                    lines.append(f"> 🔧 **#{tool_call_number} {tool}**: {summary}")
                                    if tool_result:
                                        lines.append(_format_tool_result(tool_result))
                                lines.append("")
                        else:
                            # Non-scaffolded: full inline (legacy mode)
                            _maybe_add_compaction_marker()
                            lines.append(f"**🔧 Tool: {tool}**")
                            input_formatted = _format_tool_input(tool, tool_input)
                            if input_formatted:
                                lines.append(input_formatted)
                            if tool_result:
                                result_formatted = _format_tool_result(tool_result)
                                if result_formatted:
                                    lines.append("> **Result:**")
                                    lines.append(result_formatted)
                            lines.append("")

    # Footer
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Exported from {chain.session_count} sessions with {chain.compaction_count} compactions*")

    return MarkdownExportResult(
        main_content="\n".join(lines),
        subagents=subagents,
        tool_calls=tool_calls,
        metrics=metrics,
    )


def export_chain_to_file(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
    output_path: str,
    **kwargs: Any,
) -> list[str]:
    """
    Export a conversation chain to markdown file(s).

    Writes the main conversation to output_path, and writes separate
    files for each subagent in the same directory. When scaffolded=True
    (the default), also writes tool call detail files.

    Args:
        chain: The ConversationChain to export.
        sessions: List of all session dictionaries.
        output_path: Path to write the main markdown file.
        **kwargs: Additional arguments passed to export_chain_to_markdown.

    Returns:
        List of paths to all written files (main file first, then subagent/tool files).
    """
    from pathlib import Path

    output_file = Path(output_path)
    output_dir = output_file.parent
    basename = output_file.stem  # e.g., "chain_abc123" without .md

    # Pass basename to export function for subagent link generation
    kwargs["output_basename"] = basename

    result = export_chain_to_markdown(chain, sessions, **kwargs)

    # Write main file
    output_file.write_text(result.main_content, encoding="utf-8")
    written_files = [str(output_file)]

    # Write subagent files
    # Track subagent types for consistent numbering
    subagent_type_counters: dict[str, int] = {}
    for subagent in result.subagents:
        # Use cleaner filename based on subagent type
        safe_type = subagent.subagent_type.lower().replace(' ', '_').replace('-', '_')
        subagent_type_counters[safe_type] = subagent_type_counters.get(safe_type, 0) + 1
        subagent_filename = f"{basename}_subagent_{safe_type}_{subagent_type_counters[safe_type]}.md"
        subagent_path = output_dir / subagent_filename
        subagent_content = generate_subagent_markdown(
            subagent,
            parent_chain_id=chain.chain_id,
            max_message_length=kwargs.get("max_message_length", 10000),
        )
        subagent_path.write_text(subagent_content, encoding="utf-8")
        written_files.append(str(subagent_path))

    # Write tool call detail files (scaffolded mode)
    if result.tool_calls:
        tool_calls_dir = output_dir / "tool_calls"
        tool_calls_dir.mkdir(exist_ok=True)

        for tool_call in result.tool_calls:
            # Only write files for large results (small ones are inlined)
            if not tool_call.is_small:
                tool_filename = f"{tool_call.tool_number:03d}_{tool_call.tool_use_id[:8]}.md"
                tool_path = tool_calls_dir / tool_filename
                tool_content = generate_tool_call_markdown(
                    tool_call,
                    parent_chain_id=chain.chain_id,
                )
                tool_path.write_text(tool_content, encoding="utf-8")
                written_files.append(str(tool_path))

    return written_files


def _is_user_input_span(span: dict[str, Any]) -> bool:
    """Check if span contains user input (Internal_Prompt)."""
    name = _safe_str(span.get("name"))
    return name.startswith("Claude_Code_Internal_Prompt_")


def _is_assistant_output_span(span: dict[str, Any]) -> bool:
    """Check if span contains assistant output (Final_Output)."""
    name = _safe_str(span.get("name"))
    return name.startswith("Claude_Code_Final_Output_")


def _extract_subagent_refs(output_val: str) -> list[dict[str, Any]]:
    """
    Extract subagent references from assistant output.

    When the assistant uses the Task tool to spawn a subagent, the output
    contains a tool_use block with name="Task" and input containing:
    - subagent_type: The type of subagent (e.g., "Explore", "Plan")
    - prompt: The task given to the subagent

    Returns:
        List of subagent reference dicts with type and prompt.
    """
    if not output_val:
        return []

    refs: list[dict[str, Any]] = []

    try:
        # Output is typically a JSON array of content blocks
        content = json.loads(output_val) if isinstance(output_val, str) else output_val
        if not isinstance(content, list):
            return refs

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") == "Task":
                inp = item.get("input", {})
                if isinstance(inp, dict):
                    refs.append({
                        "tool_use_id": item.get("id"),
                        "subagent_type": inp.get("subagent_type", "unknown"),
                        "prompt": inp.get("prompt", "")[:500],  # Truncate long prompts
                    })
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    return refs


def _extract_tool_use_ids(output_val: str) -> list[str]:
    """
    Extract all tool_use IDs from assistant output.

    Used to build a mapping from tool_use_id to the turn that initiated it.
    This enables linking ancillary turns (tool results) back to main thread turns.

    Returns:
        List of tool_use IDs found in the output.
    """
    if not output_val:
        return []

    ids: list[str] = []

    try:
        content = json.loads(output_val) if isinstance(output_val, str) else output_val
        if not isinstance(content, list):
            return ids

        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_id = item.get("id")
                if tool_id:
                    ids.append(tool_id)
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    return ids


def _extract_tool_use_id_from_input(input_val: str) -> str | None:
    """
    Extract tool_use_id from ancillary input (tool result continuation).

    Ancillary turns that are tool result continuations contain a tool_use_id
    field that references the tool_use in the main thread that triggered it.

    Returns:
        The tool_use_id if found, None otherwise.
    """
    if not input_val:
        return None

    try:
        content = json.loads(input_val) if isinstance(input_val, str) else input_val
        if not isinstance(content, list):
            return None

        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                return item.get("tool_use_id")
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    return None


def export_chain_to_jsonl_v1(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
    include_raw_attributes: bool = True,
    include_ancillary: bool = False,
) -> list[dict[str, Any]]:
    """
    [LEGACY] Turn-based JSONL export. Use export_chain_to_jsonl() for event-based format.

    Export a conversation chain to JSONL format (list of records).

    Each record represents either:
    - A header with chain metadata
    - A compaction boundary marker
    - A conversation turn (user input + assistant output paired)

    Turn records include:
    - thread_type: main_thread, sub_agent, or ancillary
    - parent_id: For sub_agent turns, references the parent span
    - is_compaction: Whether this turn is part of compaction handling
    - subagent_span_ids: For main_thread turns, list of child subagent span IDs

    This is the legacy turn-based format. Each line can be processed
    independently for streaming/grep-ability.

    By default, only main_thread turns are exported (much smaller files).
    Use include_ancillary=True to also export ancillary turns.

    Args:
        chain: The ConversationChain to export.
        sessions: List of all session dictionaries.
        include_raw_attributes: Whether to include raw_attributes_json (default: True).
        include_ancillary: Whether to include ancillary turns (default: False).
            Ancillary turns are tool result continuations, system reminders, etc.
            When False, reduces file size from ~16MB to ~500KB for typical chains.

    Returns:
        List of dictionaries, each representing one JSONL line.
    """
    session_lookup = {s.get("session_id", ""): s for s in sessions}
    records: list[dict[str, Any]] = []

    # First record: header with chain metadata
    records.append({
        "record_type": "header",
        "schema_version": "1.2",  # Added metrics, subagent_refs, tool_use_id linking
        "chain_id": chain.chain_id,
        "session_ids": chain.session_ids,
        "session_count": chain.session_count,
        "compaction_count": chain.compaction_count,
        "start_time": chain.start_time.isoformat() if chain.start_time else None,
        "end_time": chain.end_time.isoformat() if chain.end_time else None,
        "duration_minutes": round(chain.duration_minutes, 2),
        "total_spans": chain.total_spans,
        "total_tokens": chain.total_tokens,
    })

    turn_index = 0
    compaction_index = 0
    subagent_count = 0

    # Mapping from tool_use_id to turn_index for linking ancillary turns
    # Built from main thread outputs, used to link ancillary tool results
    tool_use_to_turn: dict[str, int] = {}

    # Stats for footer and header metrics
    thread_type_counts: dict[str, int] = {
        "main_thread": 0,
        "sub_agent": 0,
        "ancillary": 0,
        "unknown": 0,
    }

    # Aggregated metrics for header
    model_counts: dict[str, int] = {}
    total_tokens_prompt = 0
    total_tokens_completion = 0

    for session_idx, session_id in enumerate(chain.session_ids):
        session = session_lookup.get(session_id, {})
        spans = session.get("spans", [])

        # Mark compaction boundary
        if session_idx > 0:
            compaction_index += 1
            records.append({
                "record_type": "compaction",
                "compaction_index": compaction_index,
                "previous_session_id": chain.session_ids[session_idx - 1],
                "next_session_id": session_id,
            })

        # Sort spans by start time
        sorted_spans = sorted(
            spans,
            key=lambda s: _parse_timestamp(s.get("start_time")) or datetime.min
        )

        # Build span_id -> span lookup for parent references
        span_lookup: dict[str, dict[str, Any]] = {
            _safe_str(s.get("span_id")): s for s in sorted_spans if s.get("span_id")
        }

        # Build parent_id -> child span_ids mapping for subagent tracking
        children_by_parent: dict[str, list[str]] = {}
        for span in sorted_spans:
            parent_id = span.get("parent_id")
            span_id = span.get("span_id")
            if parent_id and span_id:
                if parent_id not in children_by_parent:
                    children_by_parent[parent_id] = []
                children_by_parent[parent_id].append(span_id)

        # Collect Internal_Prompt spans by turn number suffix
        # Each Internal_Prompt span contains BOTH input and output
        # (Final_Output spans are redundant - only exist for the final turn)
        internal_prompts: dict[str, dict[str, Any]] = {}

        for span in sorted_spans:
            name = _safe_str(span.get("name"))

            if _is_user_input_span(span):
                # Extract turn number from name (e.g., "123" from "Claude_Code_Internal_Prompt_123")
                suffix = name.replace("Claude_Code_Internal_Prompt_", "")
                internal_prompts[suffix] = span

        # Process turns in order by suffix number
        all_suffixes = sorted(
            internal_prompts.keys(),
            key=lambda x: int(x) if x.isdigit() else 0
        )

        for suffix in all_suffixes:
            input_span = internal_prompts[suffix]

            input_val = _extract_input_value(input_span)

            # Skip compaction system prompts
            if COMPACTION_CONTINUATION_MARKER in input_val:
                continue
            if COMPACTION_TASK_MARKER in input_val:
                continue

            # Output is stored IN the Internal_Prompt span itself, not in separate Final_Output
            # Final_Output spans only exist for the final turn of a session
            output_val = _extract_output_value(input_span)

            # Classify the span's thread type
            # Create a span dict with extracted values for proper classification
            # (raw spans may have values nested in raw_attributes_json)
            classification_span = {
                **input_span,
                "input_value": input_val,
                "output_value": output_val,
            }
            classification = classify_span_thread(classification_span)
            thread_type = classification.thread_type.value
            thread_type_counts[thread_type] = thread_type_counts.get(thread_type, 0) + 1

            # Extract model and tokens for metrics BEFORE filtering
            # This ensures header metrics reflect ALL turns, not just included ones
            model = _extract_model(input_span)
            tokens_prompt = input_span.get("llm_token_count_prompt") or 0
            tokens_completion = input_span.get("llm_token_count_completion") or 0
            tokens_total = input_span.get("llm_token_count_total")

            # Accumulate metrics for header (count ALL turns for stats)
            if model:
                model_counts[model] = model_counts.get(model, 0) + 1
            total_tokens_prompt += tokens_prompt
            total_tokens_completion += tokens_completion

            # Filter by thread type if not including ancillary
            # Main thread and sub_agent are always included
            # Ancillary is only included if include_ancillary=True
            if thread_type == "ancillary" and not include_ancillary:
                continue

            # Get parent_id for hierarchy
            parent_id = input_span.get("parent_id")
            input_span_id = input_span.get("span_id")

            # Extract subagent references from the assistant's output
            # These are Task tool_use blocks indicating subagent spawning
            subagent_refs = _extract_subagent_refs(output_val)
            subagent_count += len(subagent_refs)

            # Build tool_use_id -> turn_index mapping for ALL turns
            # This enables linking tool_result turns back to their originating tool_use
            # Note: Both main_thread and ancillary turns can spawn tool calls
            tool_ids = _extract_tool_use_ids(output_val)
            for tid in tool_ids:
                tool_use_to_turn[tid] = turn_index

            # For ancillary turns: look up the linked turn that spawned this tool result
            linked_turn_index: int | None = None
            tool_use_id: str | None = None

            if thread_type == "ancillary":
                # Try to find the tool_use_id in this turn's input
                # and link it back to the turn that spawned it
                tool_use_id = _extract_tool_use_id_from_input(input_val)
                if tool_use_id:
                    linked_turn_index = tool_use_to_turn.get(tool_use_id)

            # Build turn record
            # Note: Both input and output are in the Internal_Prompt span
            turn_record: dict[str, Any] = {
                "record_type": "turn",
                "turn_index": turn_index,
                "session_id": session_id,
                "session_index": session_idx,
                "turn_suffix": suffix,
                # Thread classification
                "thread_type": thread_type,
                "is_compaction": classification.is_compaction,
                "classification_reason": classification.reason,
                "classification_confidence": classification.confidence,
                # Hierarchy
                "parent_id": parent_id,
                "span_id": input_span_id,
                # Subagent references (for main_thread turns that spawn subagents)
                "subagent_refs": subagent_refs if subagent_refs else None,
                # Ancillary linking (for ancillary turns back to main thread)
                "tool_use_id": tool_use_id,
                "linked_turn_index": linked_turn_index,
                # Timing
                "start_time": input_span.get("start_time"),
                "end_time": input_span.get("end_time"),
                # Model and tokens (all from the same span)
                "model": model,
                "tokens_prompt": tokens_prompt if tokens_prompt else None,
                "tokens_completion": tokens_completion if tokens_completion else None,
                "tokens_total": tokens_total,
                # Content
                "input": input_val,
                "output": output_val,
            }

            if include_raw_attributes:
                turn_record["raw_attributes"] = input_span.get("raw_attributes_json")

            records.append(turn_record)
            turn_index += 1

    # Update header (records[0]) with aggregated metrics
    # These are computed after processing all turns
    records[0]["metrics"] = {
        "duration_seconds": int(chain.duration_minutes * 60) if chain.duration_minutes else None,
        "total_turns": sum(thread_type_counts.values()),
        "main_thread_turns": thread_type_counts.get("main_thread", 0),
        "ancillary_turns": thread_type_counts.get("ancillary", 0),
        "subagent_turns": thread_type_counts.get("sub_agent", 0),
        "subagent_count": subagent_count,
        "models_used": model_counts,
        "tokens": {
            "prompt": total_tokens_prompt,
            "completion": total_tokens_completion,
            "total": total_tokens_prompt + total_tokens_completion,
        },
    }

    # Add footer with turn count and thread type breakdown
    records.append({
        "record_type": "footer",
        "turn_count": turn_index,
        "compaction_count": compaction_index,
        "subagent_count": subagent_count,
        "thread_type_counts": thread_type_counts,
    })

    return records


def export_chain_to_json(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
    include_raw_attributes: bool = True,
    include_ancillary: bool = False,
) -> dict[str, Any]:
    """
    Export a conversation chain to a JSON-serializable dictionary.

    DEPRECATED: Prefer export_chain_to_jsonl for streaming/grep-ability.

    This is the canonical, reversible format containing full metadata
    and raw content for each turn in the conversation.

    Args:
        chain: The ConversationChain to export.
        sessions: List of all session dictionaries.
        include_raw_attributes: Whether to include raw_attributes_json (default: True).
        include_ancillary: Whether to include ancillary turns (default: False).

    Returns:
        Dictionary with full chain data, suitable for JSON serialization.
    """
    # Use JSONL export and convert to single JSON structure
    records = export_chain_to_jsonl(
        chain, sessions, include_raw_attributes, include_ancillary
    )

    # Extract header and footer
    header = records[0] if records and records[0].get("record_type") == "header" else {}
    footer = records[-1] if records and records[-1].get("record_type") == "footer" else {}

    # Filter to just turns and compactions
    turns = [r for r in records if r.get("record_type") in ("turn", "compaction")]

    return {
        "schema_version": header.get("schema_version", "1.0"),
        "chain_id": header.get("chain_id", chain.chain_id),
        "session_ids": header.get("session_ids", chain.session_ids),
        "session_count": header.get("session_count", chain.session_count),
        "compaction_count": header.get("compaction_count", chain.compaction_count),
        "start_time": header.get("start_time"),
        "end_time": header.get("end_time"),
        "duration_minutes": header.get("duration_minutes", round(chain.duration_minutes, 2)),
        "total_spans": header.get("total_spans", chain.total_spans),
        "total_tokens": header.get("total_tokens", chain.total_tokens),
        "turn_count": footer.get("turn_count", 0),
        "turns": turns,
    }


# =============================================================================
# V2 JSONL Export - Event-based format for markdown rendering
# =============================================================================
#
# This is the new event-based JSONL format that:
# 1. Extracts messages in correct chronological order using cumulative message diffing
# 2. Links tools to assistant messages via tool_use_id
# 3. Produces records suitable for markdown_renderer.py
#
# The key insight is that raw_gen_ai_request spans contain cumulative conversation
# history in correct order. By diffing successive spans, we get the true message order.


def _extract_cumulative_messages_from_raw_span(span: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract the cumulative conversation messages from a raw_gen_ai_request span.

    The correct path is: attributes -> llm -> None -> messages

    This is the SOURCE OF TRUTH for message ordering - it contains the full
    conversation history in correct chronological order at the time of the LLM call.

    Args:
        span: A raw_gen_ai_request span dict.

    Returns:
        List of message dicts with 'role' and 'content' keys, or empty list.
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        raw_attrs = span.get("raw_attributes")
    if not raw_attrs:
        return []

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        # Navigate: attributes -> llm -> None -> messages
        inner = attrs.get("attributes", {})
        llm = inner.get("llm", {})
        # The key is literally the string "None" (not Python None)
        none_key = llm.get("None", llm.get(None, {}))

        messages_str = none_key.get("messages", "")

        if not messages_str:
            return []

        # Parse the messages string (it's a JSON/Python literal string)
        try:
            messages = json.loads(messages_str)
        except json.JSONDecodeError:
            try:
                messages = ast.literal_eval(messages_str)
            except (ValueError, SyntaxError):
                return []

        return messages if isinstance(messages, list) else []

    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return []


def _get_message_content_key(msg: dict[str, Any]) -> str:
    """
    Generate a unique key for a message based on role and content.

    Used for deduplication when diffing successive spans.
    """
    role = msg.get("role", "")
    content = msg.get("content", "")

    if isinstance(content, list):
        # Get first text content for hashing
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    content = block.get("text", "")[:200]
                    break
                elif block.get("type") == "tool_result":
                    # Include tool_use_id for uniqueness
                    tool_id = block.get("tool_use_id", "")
                    content = f"tool_result:{tool_id}"
                    break
        else:
            content = str(content)[:200]
    elif isinstance(content, str):
        content = content[:200]
    else:
        content = str(content)[:200]

    return f"{role}:{hash(content)}"


def _get_message_text(msg: dict[str, Any]) -> str:
    """Extract text content from a message dict."""
    content = msg.get("content", "")

    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    # Include tool result content
                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        texts.append(f"[tool_result: {result_content[:100]}]")
        return " ".join(texts)
    elif isinstance(content, str):
        return content

    return str(content)


def _extract_ordered_messages(
    all_spans: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """
    Extract messages in correct chronological order by diffing successive
    raw_gen_ai_request spans.

    This is the SOURCE OF TRUTH approach - each raw_gen_ai_request span contains
    the cumulative conversation history in correct order. By finding main thread
    spans (those with increasing message counts) and diffing them, we get messages
    in the order they actually occurred.

    Args:
        all_spans: List of all spans for a session.

    Returns:
        List of (role, text) tuples in correct chronological order.
    """
    # Filter to raw_gen_ai_request spans only
    raw_spans = [s for s in all_spans if s.get("name") == "raw_gen_ai_request"]

    if not raw_spans:
        return []

    # Sort by start_time
    raw_spans.sort(key=lambda s: s.get("start_time", ""))

    # Find main thread spans (those with increasing cumulative message counts)
    main_thread_spans = []
    prev_count = 0

    for span in raw_spans:
        messages = _extract_cumulative_messages_from_raw_span(span)
        count = len(messages)

        if count >= 2:
            # Verify starts with user role
            if messages and messages[0].get("role") == "user":
                # Include if:
                # 1. Count is increasing (normal case)
                # 2. OR count dropped significantly (compaction/new turn) but we have valid messages
                #
                # Require at least 4 messages for compaction reset to distinguish from subagent calls
                is_increasing = count > prev_count
                is_compaction_reset = (
                    prev_count > 0 and
                    count < prev_count / 2 and
                    count >= 4
                )

                if is_increasing or is_compaction_reset:
                    main_thread_spans.append((span, messages))
                    prev_count = count

    if not main_thread_spans:
        return []

    # Diff successive spans to get ordered messages
    ordered_messages: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for span, messages in main_thread_spans:
        for msg in messages:
            key = _get_message_content_key(msg)
            if key not in seen_keys:
                seen_keys.add(key)
                role = msg.get("role", "unknown")
                text = _get_message_text(msg)
                if not text.strip():
                    continue

                # Skip system/internal messages that we don't export
                if role == "user":
                    # Skip tool results (internal system messages)
                    if text.startswith("[tool_result:"):
                        continue
                    # Skip system reminders
                    if text.startswith("<system-reminder>") or text.startswith("<system"):
                        continue
                    # Skip subagent prompts (not actual user turns)
                    if text.startswith("Explore the ~/") or text.startswith("Search for and read"):
                        continue
                    # Skip command/JSON outputs
                    if text.startswith("{") and text.endswith("}"):
                        continue
                    if text.startswith("Command:") and "\nOutput:" in text:
                        continue

                ordered_messages.append((role, text))

    return ordered_messages


def _parse_message_content(content: str) -> list[dict[str, Any]]:
    """
    Parse message content which may be JSON array of message blocks.

    Content may be stored as JSON (double quotes) or Python literals (single quotes).
    Returns list of message dictionaries with 'type' and content keys.
    """
    if not content:
        return []

    parsed = None

    # Try to parse as JSON array first
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        pass

    # If JSON fails, try Python literal (handles single quotes)
    if parsed is None:
        try:
            parsed = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            pass

    # Process the parsed content if successful
    if isinstance(parsed, list):
        messages = []
        for item in parsed:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    messages.append({"type": "text", "text": item.get("text", "")})
                elif item.get("type") == "tool_use":
                    messages.append({
                        "type": "tool_use",
                        "name": item.get("name", "unknown"),
                        "input": item.get("input", {}),
                        "id": item.get("id", ""),
                    })
                elif item.get("type") == "tool_result":
                    messages.append({
                        "type": "tool_result",
                        "tool_use_id": item.get("tool_use_id", ""),
                        "content": item.get("content", ""),
                    })
        return messages

    # Return as plain text if parsing failed
    if content.strip():
        return [{"type": "text", "text": content}]
    return []


def _extract_task_span_info(span: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract subagent info from a Claude_Code_Tool_Task span.

    Returns dict with: tool_use_id, subagent_type, description, prompt, response
    """
    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        return None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        attributes = attrs.get("attributes", {})

        # Get input - matches original _extract_task_span_info format
        input_data = attributes.get("input", {}).get("value", "")
        if isinstance(input_data, str):
            input_data = json.loads(input_data) if input_data else {}

        if not input_data or input_data.get("type") != "tool_use":
            return None

        # Extract tool_use_id from input's id field (not tool_use_id)
        tool_use_id = input_data.get("id", "")
        tool_input = input_data.get("input", {})

        # Get output
        output_data = attributes.get("output", {}).get("value", "")
        if isinstance(output_data, str):
            # Try to parse as Python literal (Phoenix uses single quotes)
            try:
                output_data = ast.literal_eval(output_data)
            except (ValueError, SyntaxError):
                try:
                    output_data = json.loads(output_data)
                except json.JSONDecodeError:
                    output_data = []

        # Extract response text
        response_text = ""
        if isinstance(output_data, list):
            for item in output_data:
                if isinstance(item, dict) and item.get("type") == "text":
                    response_text = item.get("text", "")
                    break
        elif isinstance(output_data, str):
            response_text = output_data

        return {
            "tool_use_id": tool_use_id,
            "subagent_type": tool_input.get("subagent_type", "unknown"),
            "description": tool_input.get("description", ""),
            "prompt": tool_input.get("prompt", ""),
            "response": response_text,
        }
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return None


def _extract_tool_span_info(span: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract tool info from a Claude_Code_Tool_* span (non-Task).

    Returns dict with: tool_use_id, name, input, result
    """
    name = span.get("name", "")
    if not name.startswith("Claude_Code_Tool_") or name == "Claude_Code_Tool_Task":
        return None

    # Extract tool name from span name
    tool_name = name.replace("Claude_Code_Tool_", "")

    raw_attrs = span.get("raw_attributes_json")
    if not raw_attrs:
        return None

    try:
        if isinstance(raw_attrs, str):
            attrs = json.loads(raw_attrs)
        else:
            attrs = raw_attrs

        attributes = attrs.get("attributes", {})

        # Get input
        input_data = attributes.get("input", {}).get("value", "")
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data) if input_data else {}
            except json.JSONDecodeError:
                input_data = {}

        tool_use_id = ""
        tool_input = {}

        if isinstance(input_data, dict):
            if input_data.get("type") == "tool_use":
                tool_use_id = input_data.get("id", "")
                tool_input = input_data.get("input", {})
            else:
                tool_input = input_data

        # Get output
        output_data = attributes.get("output", {}).get("value", "")
        result = ""
        if isinstance(output_data, str):
            result = output_data
        elif isinstance(output_data, (dict, list)):
            result = str(output_data)

        return {
            "tool_use_id": tool_use_id,
            "name": tool_name,
            "input": tool_input,
            "result": result,
        }
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        return None


def export_chain_to_jsonl(
    chain: ConversationChain,
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Export a conversation chain to JSONL format with event-based records.

    This is the canonical format designed for markdown_renderer.py. It produces:
    - A header record with chain metadata
    - Event records for each conversation event (user, assistant, tool, subagent, compaction)
    - A footer record with statistics

    The key improvement over the legacy V1 format (export_chain_to_jsonl_v1) is using
    cumulative message diffing from raw_gen_ai_request spans to get correct chronological ordering.

    Args:
        chain: The ConversationChain to export.
        sessions: List of all session dictionaries.

    Returns:
        List of dictionaries, each representing one JSONL line.
    """
    session_lookup = {s.get("session_id", ""): s for s in sessions}
    records: list[dict[str, Any]] = []

    # Collect all spans from all sessions
    all_spans: list[dict[str, Any]] = []
    for session_id in chain.session_ids:
        session = session_lookup.get(session_id, {})
        all_spans.extend(session.get("spans", []))

    # Extract Claude session UUID
    claude_session_id = chain.claude_session_id
    if not claude_session_id:
        for span in all_spans:
            raw_attrs = span.get("raw_attributes_json")
            if raw_attrs:
                try:
                    if isinstance(raw_attrs, str):
                        attrs = json.loads(raw_attrs)
                    else:
                        attrs = raw_attrs
                    # Try to find session UUID in attributes
                    meta = attrs.get("attributes", {}).get("metadata", {})
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except json.JSONDecodeError:
                            meta = {}
                    session_uuid = meta.get("sessionId") or meta.get("session_id")
                    if session_uuid and len(session_uuid) == 36:
                        claude_session_id = session_uuid
                        break
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

    if not claude_session_id:
        claude_session_id = chain.chain_id

    # Get timestamps from ALL spans
    all_timestamps: list[datetime] = []
    for span in all_spans:
        ts = _parse_timestamp(span.get("start_time"))
        if ts:
            all_timestamps.append(ts)
        ts = _parse_timestamp(span.get("end_time"))
        if ts:
            all_timestamps.append(ts)

    start_time = min(all_timestamps) if all_timestamps else None
    end_time = max(all_timestamps) if all_timestamps else None

    # LiteLLM-specific tracking
    total_tokens = 0
    models_used: set[str] = set()

    # Stats
    stats = {
        "user_turns": 0,
        "assistant_turns": 0,
        "tool_calls": 0,
        "subagents": 0,
        "compactions": 0,
    }

    # Identify main thread trace_ids
    main_thread_trace_ids: set[str] = set()
    for span in all_spans:
        if span.get("name") == "Claude_Code_Tool_Task":
            trace_id = span.get("trace_id")
            if trace_id:
                main_thread_trace_ids.add(trace_id)

    # Build ordering index from raw_gen_ai_request spans
    ordered_messages = _extract_ordered_messages(all_spans)
    ordering_index: dict[int, tuple[int, str]] = {}
    max_order_idx = len(ordered_messages)
    for idx, (role, text) in enumerate(ordered_messages):
        text_clean = text.strip()
        if text_clean:
            content_hash = hash(text_clean[:100])
            if content_hash not in ordering_index:
                ordering_index[content_hash] = (idx, role)

    # Collect conversation events: tuples of (timestamp, event_type, data)
    conversation_events: list[tuple[datetime | None, str, dict[str, Any]]] = []

    # Track seen messages and tool_use_ids for deduplication
    seen_tool_use_ids: set[str] = set()
    task_tool_ids: set[str] = set()
    seen_user_messages: set[str] = set()
    seen_assistant_messages: set[str] = set()
    seen_compaction_hashes: set[int] = set()

    # Map tool_use_id -> order_idx for correct tool positioning
    tool_use_id_to_order_idx: dict[str, int] = {}

    # Collect Task spans first
    for span in all_spans:
        if span.get("name") == "Claude_Code_Tool_Task":
            task_info = _extract_task_span_info(span)
            if task_info:
                tool_use_id = task_info["tool_use_id"]
                if tool_use_id in seen_tool_use_ids:
                    continue
                seen_tool_use_ids.add(tool_use_id)
                task_tool_ids.add(tool_use_id)
                ts = _parse_timestamp(span.get("start_time"))
                conversation_events.append((ts, "subagent", task_info))
                stats["subagents"] += 1

    # Collect tool spans (non-Task) from main thread traces only
    for span in all_spans:
        trace_id = span.get("trace_id")
        if trace_id and trace_id not in main_thread_trace_ids:
            continue

        name = span.get("name", "")
        if name.startswith("Claude_Code_Tool_") and name != "Claude_Code_Tool_Task":
            tool_info = _extract_tool_span_info(span)
            if tool_info:
                tool_use_id = tool_info.get("tool_use_id", "")
                if tool_use_id and tool_use_id in seen_tool_use_ids:
                    continue
                if tool_use_id:
                    seen_tool_use_ids.add(tool_use_id)
                ts = _parse_timestamp(span.get("start_time"))
                conversation_events.append((ts, "tool", tool_info))
                stats["tool_calls"] += 1

    # Collect main thread conversation spans for user/assistant messages
    for span in all_spans:
        trace_id = span.get("trace_id")
        if trace_id and trace_id not in main_thread_trace_ids:
            continue

        name = span.get("name", "")
        if not name.startswith("Claude_Code_Internal_Prompt_"):
            continue

        ts = _parse_timestamp(span.get("start_time"))
        if not ts:
            continue

        input_val = _extract_input_value(span)
        output_val = _extract_output_value(span)

        # Track tokens and models
        tokens_prompt = span.get("llm_token_count_prompt") or 0
        tokens_completion = span.get("llm_token_count_completion") or 0
        total_tokens += tokens_prompt + tokens_completion

        model = _extract_model(span)
        if model:
            models_used.add(model)

        # Check for compaction
        if COMPACTION_CONTINUATION_MARKER in input_val:
            if "The conversation is summarized below:" in input_val:
                summary_start = input_val.find("The conversation is summarized below:")
                summary_text = input_val[summary_start:]
                summary_hash = hash(summary_text[:500])
                if summary_hash not in seen_compaction_hashes:
                    seen_compaction_hashes.add(summary_hash)
                    stats["compactions"] += 1
                    conversation_events.append((ts, "compaction", {
                        "number": stats["compactions"],
                        "summary": summary_text,
                    }))
            continue

        # Skip compaction task spans
        if COMPACTION_TASK_MARKER in input_val:
            continue

        # Parse messages
        input_messages = _parse_message_content(input_val)
        output_messages = _parse_message_content(output_val)

        input_ts = ts
        output_ts = _parse_timestamp(span.get("end_time")) or ts

        # Extract user messages
        for msg in input_messages:
            if msg["type"] == "text":
                text = msg.get("text", "").strip()
                if text and len(text) > 10:
                    # Skip system/internal messages
                    if text.startswith("<system"):
                        continue
                    if text.startswith("Command:") and "\nOutput:" in text:
                        continue
                    if text.startswith("{") and text.endswith("}"):
                        continue
                    if text.startswith("Files modified by"):
                        continue
                    if text.startswith("Explore the ~/") or text.startswith("Search for and read"):
                        continue

                    text_hash = hash(text[:100])
                    if text_hash in seen_user_messages:
                        continue
                    seen_user_messages.add(text_hash)

                    order_info = ordering_index.get(text_hash)
                    if order_info is not None:
                        order_idx, _ = order_info
                        conversation_events.append((input_ts, "user", {"text": text, "order_idx": order_idx}))
                    else:
                        conversation_events.append((input_ts, "user", {"text": text}))
                    stats["user_turns"] += 1

        # Extract assistant messages
        assistant_order_idx_for_this_span: int | None = None
        for msg in output_messages:
            if msg["type"] == "text":
                text = msg.get("text", "").strip()
                if text and len(text) >= 5:
                    text_hash = hash(text[:100])
                    if text_hash in seen_assistant_messages:
                        continue
                    seen_assistant_messages.add(text_hash)

                    order_info = ordering_index.get(text_hash)
                    if order_info is not None:
                        order_idx, _ = order_info
                        assistant_order_idx_for_this_span = order_idx
                        conversation_events.append((output_ts, "assistant", {"text": text, "order_idx": order_idx}))
                    else:
                        conversation_events.append((output_ts, "assistant", {"text": text}))
                    stats["assistant_turns"] += 1

        # Map tool_use_ids to order_idx
        if assistant_order_idx_for_this_span is not None:
            for msg in output_messages:
                if msg["type"] == "tool_use":
                    tool_use_id = msg.get("id", "")
                    if tool_use_id:
                        tool_use_id_to_order_idx[tool_use_id] = assistant_order_idx_for_this_span

    # Assign order_idx to tools/subagents using tool_use_id linking
    for i, (ts, event_type, data) in enumerate(conversation_events):
        if event_type in ("tool", "subagent") and "order_idx" not in data:
            tool_use_id = data.get("tool_use_id", "")
            if tool_use_id and tool_use_id in tool_use_id_to_order_idx:
                spawning_assistant_idx = tool_use_id_to_order_idx[tool_use_id]
                data["order_idx"] = spawning_assistant_idx + 0.5
            else:
                # Fallback: Find last assistant before this timestamp
                best_idx = 0
                for other_ts, other_type, other_data in conversation_events:
                    if other_type == "assistant" and "order_idx" in other_data:
                        if other_ts and ts and other_ts <= ts:
                            best_idx = max(best_idx, other_data["order_idx"])
                data["order_idx"] = best_idx + 0.5

    # Event type order within same order_idx
    EVENT_TYPE_ORDER = {
        "user": 0,
        "assistant": 1,
        "tool": 2,
        "subagent": 3,
        "compaction": 4,
    }

    # Interpolate order_idx for events without one
    events_with_idx = [(ts, t, d) for ts, t, d in conversation_events if "order_idx" in d]
    events_without_idx = [(ts, t, d) for ts, t, d in conversation_events if "order_idx" not in d]

    if events_with_idx and events_without_idx:
        events_with_idx.sort(key=lambda e: e[2].get("order_idx", 0))

        for ts, event_type, data in events_without_idx:
            if ts is None:
                data["order_idx"] = max_order_idx + 1
                continue

            ts_value = ts.timestamp()
            best_idx = 0
            for other_ts, other_type, other_data in events_with_idx:
                other_idx = other_data.get("order_idx", 0)
                if other_ts and other_ts.timestamp() <= ts_value:
                    best_idx = max(best_idx, other_idx)
            data["order_idx"] = best_idx + 0.1

    def sort_key(event):
        ts, event_type, data = event
        order_idx = data.get("order_idx", max_order_idx + 1)
        type_order = EVENT_TYPE_ORDER.get(event_type, 5)
        ts_value = ts.timestamp() if ts else 0
        return (order_idx, type_order, ts_value)

    conversation_events.sort(key=sort_key)

    # Build header record
    records.append({
        "record_type": "header",
        "schema_version": "2.0",
        "chain_id": chain.chain_id,
        "claude_session_id": claude_session_id,
        "session_ids": chain.session_ids,
        "session_count": chain.session_count,
        "compaction_count": chain.compaction_count,
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_minutes": round(chain.duration_minutes, 2),
        "total_spans": chain.total_spans,
        "total_tokens": total_tokens,
        "metrics": {
            "models_used": {m: 1 for m in models_used},
        },
    })

    # Build event records
    for ts, event_type, data in conversation_events:
        event_record: dict[str, Any] = {
            "record_type": "event",
            "event_type": event_type,
            "timestamp": ts.isoformat() if ts else None,
            "order_idx": data.get("order_idx"),
        }

        if event_type == "user":
            event_record["text"] = data.get("text", "")
        elif event_type == "assistant":
            event_record["text"] = data.get("text", "")
        elif event_type == "tool":
            event_record["tool_use_id"] = data.get("tool_use_id", "")
            event_record["name"] = data.get("name", "")
            event_record["input"] = data.get("input", {})
            event_record["result"] = data.get("result", "")
        elif event_type == "subagent":
            event_record["tool_use_id"] = data.get("tool_use_id", "")
            event_record["subagent_type"] = data.get("subagent_type", "")
            event_record["description"] = data.get("description", "")
            event_record["prompt"] = data.get("prompt", "")
            event_record["response"] = data.get("response", "")
        elif event_type == "compaction":
            event_record["number"] = data.get("number", 0)
            event_record["summary"] = data.get("summary", "")

        records.append(event_record)

    # Build footer record
    records.append({
        "record_type": "footer",
        "stats": stats,
    })

    return records
