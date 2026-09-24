"""
Whole-folder backfill of Claude Code sessions into an OTLP backend (Phoenix).

Composes the two halves that already exist -- :mod:`dev_agent_lens.export.atif`
converts a session JSONL to ATIF, :mod:`dev_agent_lens.export.otlp` pushes it --
into one re-runnable command over ``~/.claude/projects``.

**Scoping is the primary safety control, not a convenience flag.** That folder
is flat and undifferentiated: one measured laptop held 88 project dirs / 455
sessions / 921 MB, of which 23 dirs were work-pathed and 65 were personal or
other clients'. A whole-folder ingest publishes all of them to shared Phoenix.
That happened once during development, with a personal session, and had to be
deleted row by row. Hence :func:`discover_sessions` refuses to run without an
explicit allowlist -- there is deliberately no "all" and no default.

Two details that decide whether the scoping is trustworthy:

  * **The allowlist matches the recorded ``cwd``, not the directory name.**
    Claude Code flattens the cwd into the directory name by replacing every
    separator with ``-``, so ``/``, ``.``, ``_`` and a literal ``-`` all land on
    the same character and the name cannot be decoded back. Matching a glob
    against a reconstruction is matching against a guess. The session file
    records its real ``cwd`` within the first few lines (found by line 5 in all
    80 project dirs measured), so that is read and matched instead. The
    directory name is the fallback only when no ``cwd`` is recorded at all --
    2 of 286 sessions measured. That fallback can WIDEN, which is worth stating
    plainly rather than assuming it only narrows: because the encoding collapses
    separators, a pattern holding a literal ``-`` can match a dirname whose true
    cwd it would have excluded. Slash-anchored patterns fail closed, since a
    dirname contains no ``/``. The CLI flags dirname-sourced groups so they are
    never silently in scope.
  * **Matching is case-sensitive.** On a case-insensitive filesystem the
    tempting default is to normcase, but for a safety allowlist predictable
    beats clever: a case slip yields zero sessions, which ``--dry-run`` shows
    immediately, rather than a wider set than intended.

Re-running is safe by construction, which is why there is no cursor file and no
sync state. harbor-atif2otel seeds trace ids from ``session_id`` and span ids
from ``{trace_id}:{trajectory_id}`` plus step index, all derived from
trajectory content, so a second ingest of an unchanged session produces the
same ids and Phoenix's insert dedup drops them.

Work is pushed one session at a time. A single 8.5 MB session already produced
1359 spans, so a whole folder is plausibly ~100k: converting the tree in one go
would hold all of it in memory, and one unreadable file would take the run down
with it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from dev_agent_lens.export.atif import (
    SUBAGENT_FILE_PREFIX,
    index_subagent_files,
    session_with_subagents_to_atif,
)
from dev_agent_lens.export.otlp import PushResult, _import_otel, push_trajectories

logger = logging.getLogger(__name__)

__all__ = [
    "DiscoveredSession",
    "IngestFailure",
    "IngestReport",
    "PushResult",
    "discover_sessions",
    "ingest_sessions",
    "push_trajectories",
    "read_session_cwd",
    "session_to_trajectories",
]

#: How far into a session file to look for the ``cwd``. It is absent from
#: bookkeeping lines (queue-operation and friends) that open most sessions; in
#: all 80 project dirs measured it appeared by line 5. 200 is slack, not need.
CWD_SCAN_LINES = 200

#: Bytes of session JSONL per emitted span, from a measured conversion (8.5 MB
#: -> 1359 spans). Used only to size up a ``--dry-run``; the real count comes
#: from the conversion.
ESTIMATED_BYTES_PER_SPAN = 6300


def read_session_cwd(path: Path, max_lines: int = CWD_SCAN_LINES) -> str | None:
    """The working directory a session recorded, or None if it never did."""
    try:
        with path.open(errors="replace") as handle:
            for index, raw in enumerate(handle):
                if index >= max_lines:
                    break
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                cwd = line.get("cwd") if isinstance(line, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError as exc:
        logger.warning("[ingest] cannot read %s: %s", path, exc)
    return None


@dataclass(frozen=True)
class DiscoveredSession:
    """One main session file, with the sidechains it spawned."""

    path: Path
    project_dir: Path
    project_path: str
    """The path the allowlist was matched against."""
    project_path_source: str
    """``"cwd"`` when read from the session, ``"dirname"`` when it had none."""
    size_bytes: int
    modified: datetime
    subagent_index: tuple[Path, ...] = ()
    """Every sidechain in the project dir -- NOT just this session's.

    Which ones this session actually spawned is only knowable by parsing it
    (``_task_call_map``), which is what the converter does. Discovery hands over
    the whole directory index and lets the converter select.
    """

    agent: str = "claude-code"
    """Which harness wrote the file: ``claude-code`` or ``codex`` (ENG2-402)."""

    @property
    def session_id(self) -> str:
        return self.path.stem

    @property
    def estimated_spans(self) -> int:
        """
        A byte-derived guess for ``--dry-run`` only. See the constant.

        Counts the main session file alone. Sidechain bytes are deliberately
        excluded: `subagent_index` spans the whole directory, so adding it here
        charged every session for every sidechain and inflated the total by the
        session count. Under-counting a dir that used subagents is the tolerable
        direction for a number whose only job is to size up a run.
        """
        return max(1, round(self.size_bytes / ESTIMATED_BYTES_PER_SPAN))


def _check_include(include: Sequence[str]) -> None:
    """Refuse an empty or match-everything scope. Shared by both harnesses."""
    if any(pattern.strip("*") == "" for pattern in include):
        raise ValueError(
            "--include '*' matches every session, including personal and other "
            "clients'. Name the scope explicitly, e.g. '*your-org*'."
        )
    if not include:
        raise ValueError(
            "an explicit --include pattern is required: session folders mix "
            "work, personal, and other clients' sessions, so there is no safe "
            "default. Try --include '*<your-org>*' and confirm with --dry-run."
        )


def discover_sessions(
    root: str | Path,
    include: Sequence[str],
    since: datetime | None = None,
) -> list[DiscoveredSession]:
    """
    Find the sessions under `root` whose project path matches `include`.

    Args:
        root: A ``~/.claude/projects``-shaped directory of project dirs.
        include: Glob patterns matched case-sensitively against each session's
            recorded ``cwd``. **Required** -- an empty list raises rather than
            meaning "everything", because that folder holds sessions the caller
            has no business publishing.
        since: Keep only sessions modified at or after this moment. Modification
            time is last activity, not session start: a resumed session counts
            as recent.

    Raises:
        ValueError: if `include` is empty.
    """
    _check_include(include)

    root = Path(root).expanduser()
    started = time.perf_counter()
    if not root.is_dir():
        logger.warning("[ingest] no such sessions directory: %s", root)
        return []

    cutoff = since.timestamp() if since else None
    sessions: list[DiscoveredSession] = []
    scanned = skipped_scope = skipped_age = 0

    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        # Two different globs on purpose, measured against a real tree (286 main
        # sessions / 213 sidechains / 21 unrelated nested files):
        #   * mains are DIRECT children only. Recursing for them would sweep in
        #     `<project>/vercel-plugin/skill-injections.jsonl` and friends, which
        #     are not sessions at all.
        #   * sidechains are NESTED, at `<session-id>/subagents/agent-<id>.jsonl`.
        #     A non-recursive glob finds zero of them and every Task span
        #     silently loses its subagent trajectory.
        mains = [
            f
            for f in sorted(project_dir.glob("*.jsonl"))
            if not f.name.startswith(SUBAGENT_FILE_PREFIX)
        ]
        if not mains:
            continue
        subagents = index_subagent_files(
            sorted(project_dir.rglob(f"{SUBAGENT_FILE_PREFIX}*.jsonl"))
        )

        for main in mains:
            scanned += 1
            cwd = read_session_cwd(main)
            # The dir name is used AS RECORDED, never decoded: the encoding
            # collapses `/`, `.`, `_` and `-` onto one character, so any
            # reconstruction would be a guess.
            project_path = cwd or project_dir.name
            source = "cwd" if cwd else "dirname"

            if not any(fnmatchcase(project_path, pattern) for pattern in include):
                skipped_scope += 1
                continue

            stat = main.stat()
            if cutoff is not None and stat.st_mtime < cutoff:
                skipped_age += 1
                continue

            sessions.append(
                DiscoveredSession(
                    path=main,
                    project_dir=project_dir,
                    project_path=project_path,
                    project_path_source=source,
                    size_bytes=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    subagent_index=tuple(sorted(subagents.values())),
                )
            )

    logger.info(
        "[ingest] discovered %d/%d sessions in %s (out of scope: %d, older than "
        "--since: %d) in %.0fms",
        len(sessions),
        scanned,
        root,
        skipped_scope,
        skipped_age,
        (time.perf_counter() - started) * 1000,
    )
    return sessions


#: Bytes a Codex session file needs before it can hold a conversation. Below this it is
#: a lone header from a session opened and closed with nothing in it (14 of 50 measured).
CODEX_MIN_SESSION_BYTES = 1024


def discover_codex_sessions(
    root: str | Path,
    include: Sequence[str],
    since: datetime | None = None,
) -> list[DiscoveredSession]:
    """
    Find Codex sessions under `root` (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``).

    Same contract as :func:`discover_sessions`: `include` is required and matched
    against the cwd each session recorded; there is no "everything".
    """
    from dev_agent_lens.export.codex_atif import read_codex_cwd

    _check_include(include)
    root = Path(root).expanduser()
    started = time.perf_counter()
    if not root.is_dir():
        logger.warning("[ingest] no such codex sessions directory: %s", root)
        return []

    cutoff = since.timestamp() if since else None
    sessions: list[DiscoveredSession] = []
    scanned = skipped_scope = skipped_age = skipped_empty = 0
    for path in sorted(root.rglob("rollout-*.jsonl")):
        scanned += 1
        stat = path.stat()
        if stat.st_size < CODEX_MIN_SESSION_BYTES:
            skipped_empty += 1
            continue
        if cutoff is not None and stat.st_mtime < cutoff:
            skipped_age += 1
            continue
        cwd = read_codex_cwd(path)
        project_path = cwd or path.parent.as_posix()
        if not any(fnmatchcase(project_path, pattern) for pattern in include):
            skipped_scope += 1
            continue
        sessions.append(
            DiscoveredSession(
                path=path,
                project_dir=path.parent,
                project_path=project_path,
                project_path_source="cwd" if cwd else "dirname",
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                agent="codex",
            )
        )
    logger.info(
        "[ingest] discovered %d/%d codex sessions in %s (out of scope: %d, older than "
        "--since: %d, empty: %d) in %.0fms",
        len(sessions),
        scanned,
        root,
        skipped_scope,
        skipped_age,
        skipped_empty,
        (time.perf_counter() - started) * 1000,
    )
    return sessions


def session_to_trajectories(session: DiscoveredSession) -> list[dict[str, Any]]:
    """Convert one session, subagents embedded, to the list push expects."""
    if session.agent == "codex":
        from dev_agent_lens.export.codex_atif import codex_session_file_to_atif

        trajectory, _ = codex_session_file_to_atif(session.path)
        return [trajectory]
    index = index_subagent_files(session.subagent_index)
    trajectory, _ = session_with_subagents_to_atif(session.path, index)
    return [trajectory]


@dataclass(frozen=True)
class IngestFailure:
    session_id: str
    path: Path
    reason: str


@dataclass
class IngestReport:
    """Outcome of a run. `spans_sent` counts spans the backend ACCEPTED."""

    sessions_total: int = 0
    sessions_ok: int = 0
    spans_total: int = 0
    spans_sent: int = 0
    requests: int = 0
    clamped_spans: int = 0
    retries_503: int = 0
    """Backoff waits for a full ingest queue -- the measured failure mode of a
    large backfill, so an operator wants the count."""
    estimated_spans: int = 0
    incomplete_sessions: int = 0
    """Sessions whose push left chunks unsent; re-running fills the gaps."""
    failures: list[IngestFailure] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures and not self.incomplete_sessions


def ingest_sessions(
    sessions: Iterable[DiscoveredSession],
    endpoint: str,
    project: str,
    user_id: str | None = None,
    chunk_size: int = 100,
    pause: float = 1.0,
    dry_run: bool = False,
    on_progress: Callable[[int, int, DiscoveredSession, str], None] | None = None,
    to_store: bool = False,
) -> IngestReport:
    """
    Convert and push each session in turn.

    One session per push, so memory stays bounded and a single bad file costs
    that file rather than the run. Failures are collected into the report;
    nothing raises.

    A finished push means every chunk was ACCEPTED, which is not the same as
    stored -- the backend enqueues and answers before it writes. Confirm by
    counting rows in the backing store.

    `dry_run` plans from discovery metadata alone: it neither converts nor
    sends. That keeps the preview instant over a folder that can be most of a
    gigabyte, and -- the reason it matters -- keeps it working without the
    optional OTel extra installed, which is exactly the machine someone is
    standing at when they check what an ingest would publish.
    """
    sessions = list(sessions)
    report = IngestReport(sessions_total=len(sessions), dry_run=dry_run)
    report.estimated_spans = sum(s.estimated_spans for s in sessions)
    started = time.perf_counter()
    logger.info(
        "[ingest] %s %d sessions -> %s project=%s",
        "planning" if dry_run else "pushing",
        len(sessions),
        endpoint,
        project,
    )

    if dry_run:
        logger.info(
            "[ingest] dry run: %d sessions, ~%d spans, nothing converted or sent",
            len(sessions),
            report.estimated_spans,
        )
        for index, session in enumerate(sessions, start=1):
            if on_progress:
                on_progress(index, len(sessions), session, "planned")
        return report

    # Fail once, before converting anything. Without this the missing optional
    # extra surfaces per session: 455 full ATIF conversions of a 921 MB folder,
    # each ending in the same ImportError swallowed by the per-session handler.
    _import_otel()

    store = con = None
    if to_store:
        # One store handle and one DuckDB connection for the whole run; the S3 path
        # resolves credentials on open and that is not something to redo per session.
        import duckdb

        from dev_agent_lens.storage.spanstore import open_store

        store = open_store()
        store.ensure()
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")

    for index, session in enumerate(sessions, start=1):
        turn = time.perf_counter()
        try:
            trajectories = session_to_trajectories(session)
            if to_store:
                from dev_agent_lens.export.store_ingest import land_trajectories

                landed = land_trajectories(trajectories, project, user_id, store=store, con=con)
                result = PushResult(
                    spans_total=landed["spans_total"],
                    clamped_spans=landed["clamped_spans"],
                    spans_sent=landed["spans_sent"],
                    requests=1,
                )
            else:
                result = push_trajectories(
                    endpoint=endpoint,
                    trajectories=trajectories,
                    project=project,
                    user_id=user_id,
                    chunk_size=chunk_size,
                    pause=pause,
                )
        except Exception as exc:  # one bad file must not take the run down
            report.failures.append(
                IngestFailure(session.session_id, session.path, f"{type(exc).__name__}: {exc}")
            )
            logger.warning(
                "[ingest] %d/%d session=%s FAILED: %s",
                index,
                len(sessions),
                session.session_id,
                exc,
            )
            if on_progress:
                on_progress(index, len(sessions), session, "failed")
            continue

        report.sessions_ok += 1
        report.spans_total += result.spans_total
        report.spans_sent += result.spans_sent
        report.requests += result.requests
        report.clamped_spans += result.clamped_spans
        report.retries_503 += result.retries_503
        status = "ok"
        if not result.complete:
            report.incomplete_sessions += 1
            status = "incomplete"
        logger.info(
            "[ingest] %d/%d session=%s spans=%d %s in %.0fms",
            index,
            len(sessions),
            session.session_id,
            result.spans_total,
            status,
            (time.perf_counter() - turn) * 1000,
        )
        if on_progress:
            on_progress(index, len(sessions), session, status)

    logger.info(
        "[ingest] done ok=%d failed=%d incomplete=%d spans_sent=%d in %.1fs",
        report.sessions_ok,
        len(report.failures),
        report.incomplete_sessions,
        report.spans_sent,
        time.perf_counter() - started,
    )
    return report
