"""Fabric convenience layer over DAL span data (ENG2-1313).

"Give me a readable conversation per session, filtered by signal" as one call —
so IR-eval / M12 / future eval mining stops re-hand-rolling DuckDB + per-session
stitching. Everything here is a THIN wrapper over the existing infra:

    query.query_sessions()                  → parquet-backed session dicts
    analysis.chains.get_chain_for_session() → spans → linked ConversationChain
    export.markdown_litellm.export_chain_to_unified_markdown() → chain → markdown

This module also backs the ``dal meeting-sessions`` / ``ticket-sessions`` /
``session-context`` CLI commands, which import from here.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Linear-style ticket ids (ENG2-123, CWORK-456) — specific enough to run over any text.
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
# Meeting ids are UUIDs. Match only over conversation CONTENT (not raw span attributes),
# else every trace_id / span_id becomes a false-positive "meeting".
_UUID_RE = re.compile(
    r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)


def _content_text(spans: list[dict[str, Any]]) -> str:
    """The conversation content of a session (input+output values) — for entity extraction."""
    parts: list[str] = []
    for span in spans:
        for key in ("input_value", "output_value"):
            v = span.get(key)
            if isinstance(v, str) and v:
                parts.append(v)
    return "\n".join(parts)


def _full_text(spans: list[dict[str, Any]]) -> str:
    """Content plus raw attributes — for tool-name matching (tool ids can live in attrs)."""
    parts = [_content_text(spans)]
    for span in spans:
        raw = span.get("raw_attributes_json") or span.get("attributes")
        if isinstance(raw, str) and raw:
            parts.append(raw)
    return "\n".join(parts)


def _sources() -> list[str]:
    """All parquet source names under the DAL storage dir."""
    from dev_agent_lens.query.parquet_query import find_parquet_files

    found = find_parquet_files()
    return list(found.keys()) if found else []


def _query(
    search: str | None = None,
    session_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """query_sessions() over one source, or ALL sources aggregated when source is None.

    query_sessions() returns nothing for a bare search/list with no source, so the
    convenience layer fans out across every source itself.
    """
    from dev_agent_lens.query.query import query_sessions

    kw = dict(search=search, session_id=session_id, start_time=since, end_time=until)
    if source is not None:
        return query_sessions(source=source, **kw)
    out: list[dict[str, Any]] = []
    for src in _sources():
        out.extend(query_sessions(source=src, **kw))
    return out


# ── listing ───────────────────────────────────────────────────────────────────
def list_sessions(
    source: str | None = None,
    pattern: str | None = None,
    tools: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    min_spans: int = 0,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """List sessions matching filters, newest-first.

    Wraps :func:`query.query_sessions` (parquet-backed pattern/date/source filters)
    then applies the ``tools`` + ``min_spans`` predicates the query layer doesn't cover.

    Args:
        source: parquet source name (e.g. ``phoenix-local-alex``); ``None`` = all sources.
        pattern: substring/regex to match in span content.
        tools: keep only sessions whose spans reference ALL of these tool names.
        since / until: ISO date/datetime bounds (string-compared against span start_time).
        min_spans: drop sessions with fewer than this many spans.
        session_id: restrict to a single session id.

    Returns:
        List of session dicts: ``{session_id, spans, span_count, start_time, end_time}``.

    Example:
        >>> list_sessions(source="phoenix-local-alex", pattern="transcript", min_spans=50)
        [{'session_id': '...', 'span_count': 123, 'start_time': '2026-05-…', ...}, ...]
    """
    sessions = _query(
        search=pattern,
        session_id=session_id,
        since=since,
        until=until,
        source=source,
    )

    if min_spans:
        sessions = [
            s
            for s in sessions
            if s.get("span_count", len(s.get("spans", []))) >= min_spans
        ]
    if tools:
        sessions = [s for s in sessions if _matches_all_tools(s.get("spans", []), tools)]

    sessions.sort(key=lambda s: s.get("start_time") or "", reverse=True)
    return sessions


def _matches_all_tools(spans: list[dict[str, Any]], tools: list[str]) -> bool:
    text = _full_text(spans)
    return all(tool in text for tool in tools)


# ── reconstruction ──────────────────────────────────────────────────────────────
def reconstruct_session(session_id: str, source: str | None = None):
    """Reconstruct a single session's conversation to markdown, in start_time order.

    Returns the export object whose ``.main_content`` is the readable markdown (plus
    ``.subagent_files`` / ``.tool_result_files`` ancillary content), or ``None`` if
    the session isn't found.

    Example:
        >>> export = reconstruct_session("abc123", source="phoenix-local-alex")
        >>> Path("session.md").write_text(export.main_content)
    """
    from dev_agent_lens.analysis.chains import get_chain_for_session
    from dev_agent_lens.export.markdown_litellm import export_chain_to_unified_markdown

    sessions = _query(session_id=session_id, source=source)
    if not sessions:
        log.warning(
            "[fabric] reconstruct_session: session %s not found (source=%s)", session_id, source
        )
        return None

    chain = get_chain_for_session(session_id, sessions)
    if chain is None:
        # get_chain_for_session drops sessions with no Claude-UUID *and* no temporal
        # neighbors (a chain-centric assumption from `chain-export`). reconstruct is
        # session-centric — the caller handed us an id — so synthesize a single-session
        # chain from the queried spans rather than render nothing.
        from dev_agent_lens.analysis.chains import ConversationChain, get_session_time_range

        spans = sessions[0].get("spans", [])
        start, end = get_session_time_range(spans)
        chain = ConversationChain(
            chain_id=session_id,
            session_ids=[session_id],
            claude_session_id=session_id,
            total_spans=len(spans),
            start_time=start,
            end_time=end,
        )

    return export_chain_to_unified_markdown(chain, sessions)


# ── bulk export ──────────────────────────────────────────────────────────────────
def _safe_name(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:120] or "session"


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically: temp file in the same dir, then ``os.replace``.

    A crash mid-write leaves the temp file (cleaned up), never a truncated ``path`` —
    so a downstream miner never reads a half-written session (AC: atomic export).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_conversations(
    source: str | None = None,
    filter: str | None = None,  # noqa: A002 — mirrors the CLI --filter flag
    pattern: str | None = None,
    tools: list[str] | None = None,
    since: str | None = None,
    limit: int | None = None,
    output_dir: str | Path = ".",
    min_spans: int = 0,
) -> list[Path]:
    """Bulk-export matching sessions to one markdown file per session, atomically.

    ``filter`` is an alias for ``pattern`` (the CLI exposes ``--filter``). Each file is
    written temp-then-rename, so a crash never leaves a partial session on disk.

    Returns the list of written paths.

    Example:
        >>> export_conversations(source="phoenix-lambda2-dal", pattern="transcript",
        ...                      limit=20, output_dir="./batch/")
        [PosixPath('batch/abc123.md'), ...]
    """
    sessions = list_sessions(
        source=source,
        pattern=pattern or filter,
        tools=tools,
        since=since,
        min_spans=min_spans,
    )
    if limit is not None:
        sessions = sessions[:limit]

    out = Path(output_dir)
    written: list[Path] = []
    for s in sessions:
        sid = s.get("session_id")
        if not sid:
            continue
        export = reconstruct_session(sid, source=source)
        if export is None:
            log.warning("[fabric] export_conversations: skipped %s (no reconstruction)", sid)
            continue
        path = out / f"{_safe_name(sid)}.md"
        _atomic_write(path, export.main_content)
        written.append(path)

    log.info(
        "[fabric] export_conversations: wrote %d/%d session(s) → %s",
        len(written), len(sessions), out,
    )
    return written


# ── business-entity lookups (back the meeting/ticket/session-context commands) ────
def get_meeting_sessions(meeting_id: str) -> list[dict[str, Any]]:
    """Sessions whose spans reference a meeting id (searched across all sources)."""
    return _query(search=meeting_id)


def get_ticket_sessions(ticket_id: str) -> list[dict[str, Any]]:
    """Sessions whose spans reference a Linear ticket id, e.g. ENG2-123 (all sources)."""
    return _query(search=ticket_id)


@dataclass
class SessionContext:
    """Business context extracted from a session's spans (meetings, tickets, cost)."""

    session_id: str
    spans: list[dict[str, Any]]
    token_count: int = 0
    duration_minutes: float = 0.0
    meeting_ids: list[str] = field(default_factory=list)
    ticket_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "span_count": len(self.spans),
            "token_count": self.token_count,
            "duration_minutes": round(self.duration_minutes, 2),
            "meeting_ids": self.meeting_ids,
            "ticket_ids": self.ticket_ids,
        }


def get_session_context(session_id: str) -> SessionContext:
    """Meetings, tickets, token cost, and duration referenced by a session.

    Entity ids are extracted from conversation content (best-effort); token/duration
    come from :func:`analysis.sessions.session_metrics`.
    """
    from dev_agent_lens.analysis.sessions import session_metrics

    sessions = _query(session_id=session_id)
    if not sessions:
        return SessionContext(session_id=session_id, spans=[])

    spans = sessions[0].get("spans", [])
    content = _content_text(spans)
    metrics = session_metrics(spans, session_id=session_id)
    return SessionContext(
        session_id=session_id,
        spans=spans,
        token_count=metrics.token_count_total,
        duration_minutes=metrics.duration_minutes,
        meeting_ids=sorted(set(_UUID_RE.findall(content))),
        ticket_ids=sorted(set(_TICKET_RE.findall(content))),
    )
