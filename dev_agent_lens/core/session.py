"""
Session ID Extractor Module

Provides functions for extracting session IDs from trace spans.
Handles both Phoenix and Arize metadata formats.

Session ID Patterns:
    - Phoenix: metadata.user_id with `_session_<id>` or `session_<id>` suffix
    - Arize: user_api_key_end_user_id or requester_metadata.user_id with `session_<id>`
"""

from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

# Legacy underscore-format identity string: user_<hash>_account_<uuid>_session_<uuid>.
# Kept only as a fallback — the current LiteLLM proxy emits a JSON object instead
# (see _parse_identity).
SESSION_PATTERN = re.compile(r"session_([a-zA-Z0-9_-]+)")
USER_PATTERN = re.compile(r"user_([a-zA-Z0-9]+)_account")
ACCOUNT_PATTERN = re.compile(r"account_([a-f0-9-]{36})")

# Keys of the current JSON-object identity emitted by the LiteLLM proxy.
_IDENTITY_KEYS = ("device_id", "account_uuid", "session_id")


def _maybe_json_obj(value: Any) -> dict | None:
    """Return ``value`` as a dict if it is one (or a JSON-object string), else None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _parse_identity(value: Any) -> dict[str, str | None]:
    """Parse a LiteLLM end-user identity into ``{device_id, account_id, session_id}``.

    Two real formats arrive on ``user_api_key_end_user_id`` / ``requester_metadata.user_id``:

    * **JSON object** (current LiteLLM proxy) —
      ``{"device_id": ..., "account_uuid": ..., "session_id": ...}``. Read the keys
      DIRECTLY. Running ``SESSION_PATTERN`` over this string would match the
      ``session_id`` *key* and return the literal ``"id"`` — that is the
      ENG2-1312/1319 bug — so a JSON object must never be regex-scanned.
    * **Legacy underscore string** — ``user_<hash>_account_<uuid>_session_<uuid>``.
      Fall back to the regexes.

    Missing components come back as ``None`` (honest non-attribution, no fallback).
    """
    out: dict[str, str | None] = {"device_id": None, "account_id": None, "session_id": None}
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return out

    obj = _maybe_json_obj(value)
    if obj is not None:
        device_id = obj.get("device_id")
        account_id = obj.get("account_uuid") or obj.get("account_id")
        session_id = obj.get("session_id")
        out["device_id"] = str(device_id) if device_id else None
        out["account_id"] = str(account_id) if account_id else None
        out["session_id"] = str(session_id) if session_id else None
        return out

    text = str(value)
    user_match = USER_PATTERN.search(text)
    account_match = ACCOUNT_PATTERN.search(text)
    session_match = SESSION_PATTERN.search(text)
    out["device_id"] = user_match.group(1) if user_match else None
    out["account_id"] = account_match.group(1) if account_match else None
    out["session_id"] = session_match.group(1) if session_match else None
    return out


def extract_session_id(metadata: Any) -> str | None:
    """
    Extract session ID from span metadata.

    Handles multiple metadata formats from Phoenix and Arize:
    - String format: "user_session_abc123" → "abc123"
    - Dict with user_id: {"user_id": "session_abc123"} → "abc123"
    - Dict with user_api_key_end_user_id: {"user_api_key_end_user_id": "session_abc123"}
    - Dict with requester_metadata: {"requester_metadata": {"user_id": "session_abc123"}}

    Args:
        metadata: The metadata field from a span. Can be a string, dict, or JSON string.

    Returns:
        The extracted session ID string, or None if no session ID found.
    """
    if metadata is None or (isinstance(metadata, float) and pd.isna(metadata)):
        return None

    # Handle string metadata (may be raw string or JSON)
    if isinstance(metadata, str):
        # Try to parse as JSON first
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict):
                return _extract_from_dict(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try to extract from string pattern
        return _extract_from_string(metadata)

    # Handle dict metadata
    if isinstance(metadata, dict):
        return _extract_from_dict(metadata)

    return None


def _extract_from_string(value: str) -> str | None:
    """Extract session ID from a string value (JSON-object or legacy string).

    Routes through _parse_identity so a JSON-object end-user id reads its
    ``session_id`` key instead of the regex matching the key name as ``"id"``.
    """
    if not value:
        return None

    return _parse_identity(value)["session_id"]


def _extract_from_dict(metadata: dict) -> str | None:
    """Extract session ID from a dict metadata structure."""
    # The dict may itself BE the JSON identity object {device_id, account_uuid,
    # session_id} — read its session_id key directly (not the regex).
    if any(k in metadata for k in _IDENTITY_KEYS):
        session_id = _parse_identity(metadata)["session_id"]
        if session_id:
            return session_id

    # Try Phoenix format: metadata.user_id with session_ pattern
    user_id = metadata.get("user_id")
    if user_id:
        session_id = _extract_from_string(str(user_id))
        if session_id:
            return session_id

    # Try Arize format: user_api_key_end_user_id
    user_id = metadata.get("user_api_key_end_user_id")
    if user_id:
        session_id = _extract_from_string(str(user_id))
        if session_id:
            return session_id

    # Try Arize format: requester_metadata.user_id
    req_meta = metadata.get("requester_metadata")
    if isinstance(req_meta, dict):
        user_id = req_meta.get("user_id")
        if user_id:
            session_id = _extract_from_string(str(user_id))
            if session_id:
                return session_id

    # Try string representation if nothing else worked
    return None


def extract_session_id_from_span(span: dict | pd.Series) -> str | None:
    """
    Extract session ID from a unified span.

    This is a convenience function that handles both dict and pandas Series
    representations of spans, looking in common metadata fields.

    Session ID extraction priority:
    1. Explicit session_id patterns in metadata fields (session_xxx)
    2. trace_id field (used as session grouping in Phoenix/Arize)
    3. Input value patterns (sometimes embedded in prompts)

    Args:
        span: A unified span as a dict or pandas Series.

    Returns:
        The extracted session ID string, or None if no session ID found.
    """
    if isinstance(span, pd.Series):
        span = span.to_dict()

    # Try various metadata field names for explicit session patterns
    metadata_fields = [
        "metadata",
        "attributes.metadata",
        "raw_attributes",
    ]

    for field in metadata_fields:
        if field in span:
            metadata = span[field]
            # If this is raw_attributes, look for metadata/attributes inside it
            if field == "raw_attributes" and isinstance(metadata, dict):
                # =============================================================
                # CLAUDE CODE SPECIFIC SESSION EXTRACTION (check FIRST!)
                # =============================================================
                # Claude Code traces store session metadata in nested dicts.
                # The session ID is embedded in a user_id string like:
                #   "user_<hash>_account_<uuid>_session_<uuid>"
                #
                # We check these BEFORE the generic extract_session_id() calls
                # because those will fallback to trace_id if they can't find
                # a session pattern, preventing us from reaching this code.
                # =============================================================

                # Claude Code via LiteLLM: attributes.metadata.user_api_key_end_user_id
                # Contains the full user_account_session string from LiteLLM proxy
                # Check nested dict path (raw_attributes.attributes.metadata)
                if "attributes" in metadata and isinstance(metadata["attributes"], dict):
                    attrs_metadata = metadata["attributes"].get("metadata")
                    if isinstance(attrs_metadata, dict):
                        # Try user_api_key_end_user_id first (LiteLLM proxy format)
                        end_user_id = attrs_metadata.get("user_api_key_end_user_id")
                        if end_user_id:
                            session_id = _extract_from_string(str(end_user_id))
                            if session_id:
                                return session_id
                        # Also try requester_metadata.user_id
                        req_meta = attrs_metadata.get("requester_metadata")
                        if isinstance(req_meta, dict):
                            user_id = req_meta.get("user_id")
                            if user_id:
                                session_id = _extract_from_string(str(user_id))
                                if session_id:
                                    return session_id

                # Also check for dotted key format (some Phoenix versions)
                if "attributes.metadata" in metadata:
                    session_id = extract_session_id(metadata["attributes.metadata"])
                    if session_id:
                        return session_id

                # Claude Code via nested dicts: attributes.llm.*.metadata.user_id
                # The structure is: attributes -> llm -> {model_key} -> metadata -> user_id
                # where model_key can be "None", a model name, or other values
                if "attributes" in metadata and isinstance(metadata["attributes"], dict):
                    llm_data = metadata["attributes"].get("llm")
                    if isinstance(llm_data, dict):
                        # Iterate through all model keys in the llm dict
                        for model_key in llm_data:
                            model_data = llm_data[model_key]
                            if isinstance(model_data, dict):
                                model_metadata = model_data.get("metadata")
                                if model_metadata:
                                    session_id = extract_session_id(model_metadata)
                                    if session_id:
                                        return session_id

                # =============================================================
                # GENERIC EXTRACTION (fallback paths)
                # =============================================================
                # Try nested metadata key (Phoenix format)
                if "metadata" in metadata:
                    session_id = extract_session_id(metadata["metadata"])
                    if session_id:
                        return session_id
                # Try nested attributes key (Arize format)
                if "attributes" in metadata:
                    session_id = extract_session_id(metadata["attributes"])
                    if session_id:
                        return session_id
                # Fall through to try the raw_attributes dict itself
            session_id = extract_session_id(metadata)
            if session_id:
                return session_id

    # Fallback to trace_id - this is the standard way Phoenix and Arize group
    # spans into sessions/traces. The trace_id represents a complete
    # conversation or agent execution session.
    trace_id = span.get("trace_id")
    if trace_id and not (isinstance(trace_id, float) and pd.isna(trace_id)):
        return str(trace_id)

    # Also check input_value for session patterns (sometimes embedded in prompts)
    input_value = span.get("input_value")
    if input_value:
        session_id = _extract_from_string(str(input_value))
        if session_id:
            return session_id

    return None


def _identity_string_from_dict(metadata: dict) -> str | None:
    """Return the raw LiteLLM end-user string from a metadata dict, if present.

    The string has the form ``user_<hash>_account_<uuid>_session_<uuid>`` and
    lives under one of the known LiteLLM proxy keys.
    """
    end_user_id = metadata.get("user_api_key_end_user_id")
    if end_user_id:
        return str(end_user_id)

    req_meta = metadata.get("requester_metadata")
    if isinstance(req_meta, dict) and req_meta.get("user_id"):
        return str(req_meta["user_id"])

    user_id = metadata.get("user_id")
    if user_id:
        return str(user_id)

    return None


def _identity_string(metadata: Any) -> str | None:
    """Extract the raw LiteLLM end-user identity (JSON or legacy string) from metadata."""
    if metadata is None or (isinstance(metadata, float) and pd.isna(metadata)):
        return None

    if isinstance(metadata, dict):
        # The dict may BE the identity object, or WRAP it under a known key.
        if any(k in metadata for k in _IDENTITY_KEYS):
            return json.dumps(metadata)
        return _identity_string_from_dict(metadata)

    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            # The string may BE the identity object, or WRAP it under a known key.
            if any(k in parsed for k in _IDENTITY_KEYS):
                return metadata
            wrapped = _identity_string_from_dict(parsed)
            if wrapped:
                return wrapped
        # The raw string may itself be the legacy underscore identity.
        if USER_PATTERN.search(metadata) or ACCOUNT_PATTERN.search(metadata):
            return metadata

    return None


def extract_user_id(metadata: Any) -> str | None:
    """Extract the canonical per-user identifier from span metadata.

    For the current JSON-object identity this is the ``device_id`` (the stable
    per-machine/per-auth hash); for the legacy underscore string it is the
    ``user_<hash>`` segment. Stored as ``user_id`` in the unified schema.

    Args:
        metadata: A metadata string, dict, or JSON string.

    Returns:
        The user/device id, or None if no user identity is present.
    """
    identity = _identity_string(metadata)
    return _parse_identity(identity)["device_id"] if identity else None


def extract_account_id(metadata: Any) -> str | None:
    """Extract the account UUID from span metadata.

    The ``account_uuid`` key of the JSON identity (or the ``account_<uuid>``
    segment of the legacy string); stable across a single user's machines.

    Args:
        metadata: A metadata string, dict, or JSON string.

    Returns:
        The account UUID, or None if no account identity is present.
    """
    identity = _identity_string(metadata)
    return _parse_identity(identity)["account_id"] if identity else None


def _iter_identity_metadata(span: dict | pd.Series) -> Any:
    """Yield candidate metadata objects from a span across known layouts.

    Mirrors the metadata locations used by ``extract_session_id_from_span`` but
    does not fall back to trace_id/input_value — user/account attribution must
    come from real proxy metadata or not at all.
    """
    if isinstance(span, pd.Series):
        span = span.to_dict()

    # Flat metadata fields (used by unify inputs and simple Phoenix rows).
    for field in ("metadata", "attributes.metadata"):
        value = span.get(field)
        if isinstance(value, dict):
            yield value

    raw = span.get("raw_attributes")
    if isinstance(raw, dict):
        # Dotted-key format (lambda2-dal): {"attributes.metadata": "<json>"}
        dotted = raw.get("attributes.metadata")
        if dotted is not None:
            yield dotted
        # Nested dict format (local-alex): {"attributes": {"metadata": {...}}}
        attributes = raw.get("attributes")
        if isinstance(attributes, dict):
            nested = attributes.get("metadata")
            if nested is not None:
                yield nested
            # Legacy llm.*.metadata format.
            llm = attributes.get("llm")
            if isinstance(llm, dict):
                for model_data in llm.values():
                    if isinstance(model_data, dict) and model_data.get("metadata") is not None:
                        yield model_data["metadata"]
        # Top-level metadata key inside raw_attributes.
        if raw.get("metadata") is not None:
            yield raw["metadata"]


def extract_user_id_from_span(span: dict | pd.Series) -> str | None:
    """Extract the user hash from a span across all known metadata layouts.

    Unlike session extraction, this never falls back to trace_id: a span without
    proxy user metadata is genuinely unattributable.
    """
    for metadata in _iter_identity_metadata(span):
        user_id = extract_user_id(metadata)
        if user_id:
            return user_id
    return None


def extract_account_id_from_span(span: dict | pd.Series) -> str | None:
    """Extract the account UUID from a span across all known metadata layouts."""
    for metadata in _iter_identity_metadata(span):
        account_id = extract_account_id(metadata)
        if account_id:
            return account_id
    return None
