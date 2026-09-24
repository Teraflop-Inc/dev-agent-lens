"""
Unit tests for the whole-folder session ingest (`dal ingest-sessions`).

The behaviour that must not regress is the scoping. `~/.claude/projects` mixes
work, personal, and other clients' sessions in one flat directory (measured on
one laptop: 88 project dirs, 455 sessions, 921 MB, of which 23 dirs were
work-pathed). A whole-folder ingest with no allowlist pushes the other 65 into
shared Phoenix, which happened once during development and had to be deleted
row by row. So:

  * discovery REFUSES to run without an explicit include pattern, and
  * a non-matching project dir is never selected, however the run is spelled.

The second measured trap is the encoding. Claude Code flattens the project cwd
into the directory name by replacing every separator with ``-``, which is
lossy: ``/``, ``.``, ``_`` and a literal ``-`` all become the same character.
Matching a user's glob against a path reconstructed from that name is matching
against a guess. The session file records its real ``cwd``, so that is what the
allowlist is applied to.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_agent_lens.export import ingest

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _session_lines(session_id: str, cwd: str | None) -> list[dict]:
    """A minimal but realistic session: bookkeeping first, then conversation.

    `cwd` deliberately does not appear on line 0 -- in every real session
    measured it first shows up around line 2-5, behind queue-operation and
    other bookkeeping lines.
    """
    lines: list[dict] = [
        {
            "type": "queue-operation",
            "operation": "add",
            "sessionId": session_id,
            "timestamp": "2026-08-04T09:58:00Z",
        },
    ]
    user: dict = {
        "type": "user",
        "sessionId": session_id,
        "timestamp": "2026-08-04T09:59:00Z",
        "message": {"role": "user", "content": "hi"},
    }
    assistant: dict = {
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": "2026-08-04T10:00:00Z",
        "version": "2.1.0",
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    if cwd is not None:
        user["cwd"] = cwd
        assistant["cwd"] = cwd
    lines += [user, assistant]
    return lines


def _write_session(
    project_dir: Path, session_id: str, cwd: str | None = None, lines: list[dict] | None = None
) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    payload = lines if lines is not None else _session_lines(session_id, cwd)
    path.write_text("\n".join(json.dumps(line) for line in payload) + "\n")
    return path


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """A ~/.claude/projects stand-in holding work and non-work sessions."""
    root = tmp_path / "projects"
    _write_session(
        root / "-Users-me-git-work-teraflop-agent-forge",
        "work-1",
        cwd="/Users/me/git/work/teraflop/agent-forge",
    )
    _write_session(
        root / "-Users-me-git-work-teraflop-Solutions-Fabric",
        "work-2",
        cwd="/Users/me/git/work/teraflop/Solutions-Fabric",
    )
    _write_session(
        root / "-Users-me-personal-Artist-Vault-Kit",
        "personal-1",
        cwd="/Users/me/personal/Artist-Vault-Kit",
    )
    _write_session(root / "-Users-me-clients-acme", "other-client-1", cwd="/Users/me/clients/acme")
    return root


def _ids(sessions) -> set[str]:
    return {s.session_id for s in sessions}


# --------------------------------------------------------------------------
# scoping -- the safety control
# --------------------------------------------------------------------------


def test_discovery_refuses_without_an_include_pattern(projects_root: Path):
    """No allowlist must never mean "everything"."""
    with pytest.raises(ValueError, match="include"):
        ingest.discover_sessions(projects_root, include=[])


def test_include_selects_only_matching_projects(projects_root: Path):
    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])

    assert _ids(sessions) == {"work-1", "work-2"}


def test_personal_and_other_client_sessions_are_never_selected(projects_root: Path):
    """The regression this whole command is shaped around."""
    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])

    selected = {s.project_path for s in sessions}
    assert not any("personal" in p or "clients" in p for p in selected), selected


def test_several_include_patterns_union(projects_root: Path):
    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*", "*clients*"])

    assert _ids(sessions) == {"work-1", "work-2", "other-client-1"}


def test_matching_is_case_sensitive(projects_root: Path):
    """Predictable beats platform-clever: a case slip yields nothing, not everything."""
    assert ingest.discover_sessions(projects_root, include=["*TERAFLOP*"]) == []


# --------------------------------------------------------------------------
# project path resolution
# --------------------------------------------------------------------------


def test_project_path_comes_from_the_recorded_cwd_not_the_directory_name(
    projects_root: Path,
):
    sessions = ingest.discover_sessions(projects_root, include=["*Solutions-Fabric*"])

    assert len(sessions) == 1
    session = sessions[0]
    # The directory name has flattened every separator to "-"; the cwd has not.
    assert session.project_path == "/Users/me/git/work/teraflop/Solutions-Fabric"
    assert session.project_path_source == "cwd"


def test_a_session_with_no_recorded_cwd_falls_back_to_the_directory_name(
    tmp_path: Path,
):
    root = tmp_path / "projects"
    _write_session(root / "-Users-me-git-work-teraflop", "no-cwd", cwd=None)

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])

    assert len(sessions) == 1
    assert sessions[0].project_path_source == "dirname"
    assert sessions[0].project_path == "-Users-me-git-work-teraflop"


# --------------------------------------------------------------------------
# what counts as a session
# --------------------------------------------------------------------------


def _write_sidechain(project_dir: Path, parent_session: str, agent_id: str, cwd: str) -> Path:
    """A subagent transcript, in the layout Claude Code actually writes.

    Measured on a real tree: 213 sidechains, every one at
    `<project>/<session-id>/subagents/agent-<id>.jsonl`, ZERO as flat siblings.
    The sidechain records the PARENT's sessionId -- that is the hazard the
    converter's distinct trajectory_id exists to work around.
    """
    nest = project_dir / parent_session / "subagents"
    nest.mkdir(parents=True, exist_ok=True)
    path = nest / f"agent-{agent_id}.jsonl"
    lines = _session_lines(parent_session, cwd)
    for line in lines:
        line["isSidechain"] = True
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def test_subagent_sidechains_are_attached_not_listed_as_sessions(tmp_path: Path):
    """`agent-*.jsonl` records the PARENT's sessionId; listing it standalone
    collapses both onto one trace id and drops spans.

    Regression: sidechains are NESTED. A non-recursive glob for them finds zero
    on real data and every Task span silently loses its subagent trajectory.
    """
    root = tmp_path / "projects"
    project = root / "-Users-me-git-work-teraflop"
    _write_session(project, "main-1", cwd="/Users/me/git/work/teraflop")
    _write_sidechain(project, "main-1", "abc123", "/Users/me/git/work/teraflop")

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])

    assert _ids(sessions) == {"main-1"}
    assert [p.name for p in sessions[0].subagent_index] == ["agent-abc123.jsonl"]


def test_nested_non_session_files_are_not_mistaken_for_sessions(tmp_path: Path):
    """Recursing for sidechains must not also recurse for sessions.

    A real tree carries `<project>/vercel-plugin/skill-injections.jsonl` (21 of
    them measured); treating those as sessions would push junk to Phoenix.
    """
    root = tmp_path / "projects"
    project = root / "-Users-me-git-work-teraflop"
    _write_session(project, "main-1", cwd="/Users/me/git/work/teraflop")
    plugin = project / "vercel-plugin"
    plugin.mkdir(parents=True)
    (plugin / "skill-injections.jsonl").write_text('{"type":"whatever"}\n')

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])

    assert _ids(sessions) == {"main-1"}


def test_since_filters_by_last_activity(tmp_path: Path):
    root = tmp_path / "projects"
    project = root / "-Users-me-git-work-teraflop"
    old = _write_session(project, "old-1", cwd="/Users/me/git/work/teraflop")
    _write_session(project, "new-1", cwd="/Users/me/git/work/teraflop")

    stale = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (stale, stale))

    cutoff = datetime(2026, 7, 1, tzinfo=timezone.utc)
    sessions = ingest.discover_sessions(root, include=["*teraflop*"], since=cutoff)

    assert _ids(sessions) == {"new-1"}


# --------------------------------------------------------------------------
# ingest loop
# --------------------------------------------------------------------------


def test_dry_run_sends_nothing(projects_root: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: calls.append(kw) or ingest.PushResult(),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    report = ingest.ingest_sessions(
        sessions, endpoint="http://localhost:6006", project="p", dry_run=True
    )

    assert calls == []
    assert report.sessions_total == 2
    assert report.estimated_spans > 0


def test_one_unreadable_session_does_not_abort_the_run(tmp_path: Path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "-Users-me-git-work-teraflop"
    _write_session(project, "good-1", cwd="/Users/me/git/work/teraflop")
    _write_session(project, "bad-1", cwd="/Users/me/git/work/teraflop")
    _write_session(project, "good-2", cwd="/Users/me/git/work/teraflop")

    def explode(**kwargs):
        trajectories = kwargs["trajectories"]
        if any(t.get("session_id") == "bad-1" for t in trajectories):
            raise RuntimeError("boom")
        return ingest.PushResult(spans_total=3, spans_sent=3, requests=1)

    monkeypatch.setattr(ingest, "push_trajectories", explode)

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="http://localhost:6006", project="p")

    assert report.sessions_ok == 2
    assert [f.session_id for f in report.failures] == ["bad-1"]
    assert report.spans_sent == 6


def test_each_session_is_pushed_separately(projects_root: Path, monkeypatch):
    """Chunk per session: one 8.5 MB session already produced 1359 spans, so a
    whole-folder push is not a single request."""
    pushes = []
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: pushes.append(kw["trajectories"]) or ingest.PushResult(),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    ingest.ingest_sessions(sessions, endpoint="http://x", project="p")

    assert len(pushes) == 2
    assert all(len(batch) == 1 for batch in pushes)


# --------------------------------------------------------------------------
# re-runnability
# --------------------------------------------------------------------------


def test_converting_the_same_session_twice_is_byte_identical(projects_root: Path):
    """The property the whole no-cursor-file design rests on.

    harbor-atif2otel seeds trace ids from `session_id` and span ids from
    `{trace_id}:{trajectory_id}` plus step index, so identical ATIF means
    identical span ids, which Phoenix's insert dedup drops on re-ingest.
    """
    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    session = sessions[0]

    first = ingest.session_to_trajectories(session)
    second = ingest.session_to_trajectories(session)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first[0]["session_id"] == session.session_id


def test_dry_run_does_not_convert(projects_root: Path, monkeypatch):
    """The preview must not reach the converter at all.

    Converting imports the optional OTel extra, so a dry run that converts
    crashes on the machine someone is standing at when they ask "what would
    this publish?" -- before they have installed anything. It would also read a
    folder that can be most of a gigabyte just to print an estimate.

    Asserted directly rather than by skipping when the extra is absent: a
    skipped test pins nothing, and the extra is installed here.
    """
    monkeypatch.setattr(
        ingest,
        "session_to_trajectories",
        lambda s: pytest.fail("dry run converted a session"),
    )
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: pytest.fail("dry run pushed"),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    report = ingest.ingest_sessions(
        sessions, endpoint="http://localhost:6006", project="p", dry_run=True
    )

    assert report.sessions_total == 2
    assert report.spans_sent == 0
    assert report.estimated_spans > 0


def test_dry_run_estimate_is_not_multiplied_by_session_count(tmp_path: Path):
    """`subagent_index` spans the whole directory, so charging every session for
    every sidechain inflated the estimate by the session count (measured 3.0x)."""
    root = tmp_path / "projects"
    project = root / "-Users-me-git-work-teraflop"
    for name in ("main-a", "main-b", "main-c"):
        _write_session(project, name, cwd="/Users/me/git/work/teraflop")
    big = project / "main-a" / "subagents"
    big.mkdir(parents=True)
    (big / "agent-huge.jsonl").write_text("Q" * 500_000)

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])
    estimates = {s.session_id: s.estimated_spans for s in sessions}

    assert len(set(estimates.values())) == 1, (
        f"one session's sidechain leaked into its siblings' estimates: {estimates}"
    )
    assert sum(estimates.values()) < 100, estimates


def test_a_session_that_fails_to_convert_does_not_abort_the_run(tmp_path: Path, monkeypatch):
    """Conversion failure, not just push failure -- moving the conversion out of
    the try block passes the push-failure test while taking the run down."""
    root = tmp_path / "projects"
    project = root / "-Users-me-git-work-teraflop"
    for name in ("good-1", "bad-1", "good-2"):
        _write_session(project, name, cwd="/Users/me/git/work/teraflop")

    real = ingest.session_to_trajectories

    def convert(session):
        if session.session_id == "bad-1":
            raise ValueError("unconvertible")
        return real(session)

    monkeypatch.setattr(ingest, "session_to_trajectories", convert)
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: ingest.PushResult(spans_total=2, spans_sent=2, requests=1),
    )

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="http://x", project="p")

    assert report.sessions_ok == 2
    assert [f.session_id for f in report.failures] == ["bad-1"]
    assert "unconvertible" in report.failures[0].reason


def test_a_partly_sent_session_is_reported_not_silently_accepted(projects_root: Path, monkeypatch):
    """Accepted != stored. A push that left chunks unsent must fail the run so
    the operator re-runs, rather than reading as success."""
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: ingest.PushResult(spans_total=10, spans_sent=5, requests=1, failed_chunks=1),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="http://x", project="p")

    assert report.incomplete_sessions == 2
    assert not report.ok


def test_a_clean_run_is_ok(projects_root: Path, monkeypatch):
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: ingest.PushResult(spans_total=3, spans_sent=3, requests=1),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="http://x", project="p")

    assert report.ok and report.sessions_ok == 2


def test_push_options_reach_the_pusher(projects_root: Path, monkeypatch):
    """--user/--chunk-size/--pause are CLI options; dropping one is invisible
    unless forwarding is asserted."""
    seen = []
    monkeypatch.setattr(
        ingest,
        "push_trajectories",
        lambda **kw: seen.append(kw) or ingest.PushResult(),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    ingest.ingest_sessions(
        sessions,
        endpoint="http://x",
        project="proj",
        user_id="alice",
        chunk_size=7,
        pause=0.25,
    )

    assert seen
    for call in seen:
        assert call["endpoint"] == "http://x"
        assert call["project"] == "proj"
        assert call["user_id"] == "alice"
        assert call["chunk_size"] == 7
        assert call["pause"] == 0.25


def test_a_missing_sessions_directory_is_empty_not_an_error(tmp_path: Path):
    assert ingest.discover_sessions(tmp_path / "nope", include=["*x*"]) == []


def test_cwd_beyond_the_scan_window_falls_back_to_the_directory_name(
    tmp_path: Path,
):
    root = tmp_path / "projects"
    _write_session(root / "-Users-me-git-work-teraflop", "deep", cwd="/Users/me/git/work/teraflop")

    sessions = ingest.discover_sessions(root, include=["*teraflop*"])
    assert sessions[0].project_path_source == "cwd"

    path = sessions[0].path
    assert ingest.read_session_cwd(path, max_lines=1) is None


def test_a_catch_all_pattern_is_refused(projects_root: Path):
    """`--include '*'` was a working "all", which contradicts the promise that
    there is no all. fnmatchcase does not treat `/` specially, so `*` crosses
    separators and reaches every session on the machine."""
    with pytest.raises(ValueError, match="matches every session"):
        ingest.discover_sessions(projects_root, include=["*"])

    with pytest.raises(ValueError, match="matches every session"):
        ingest.discover_sessions(projects_root, include=["*teraflop*", "**"])


def test_a_missing_otlp_extra_fails_once_before_converting_anything(
    projects_root: Path, monkeypatch
):
    """Without the optional extra, the ImportError used to surface per session:
    a full ATIF conversion of every file in a folder that can be 921 MB, each
    ending in the same error swallowed by the per-session handler."""
    monkeypatch.setattr(
        ingest,
        "_import_otel",
        lambda: (_ for _ in ()).throw(ImportError("OTLP export needs harbor-atif2otel")),
    )
    monkeypatch.setattr(
        ingest,
        "session_to_trajectories",
        lambda s: pytest.fail("converted before checking the extra was importable"),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    with pytest.raises(ImportError, match="harbor-atif2otel"):
        ingest.ingest_sessions(sessions, endpoint="http://x", project="p")


def test_the_extra_is_not_needed_for_a_dry_run(projects_root: Path, monkeypatch):
    """The preflight must sit AFTER the dry-run short-circuit, or previewing
    would require the very thing the preview exists to avoid installing."""
    monkeypatch.setattr(
        ingest,
        "_import_otel",
        lambda: (_ for _ in ()).throw(ImportError("nope")),
    )

    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="http://x", project="p", dry_run=True)

    assert report.sessions_total == 2


# --------------------------------------------------------------------------
# --to-store: the five-minute story (ENG2-1612) needs no Phoenix
# --------------------------------------------------------------------------
def test_to_store_lands_sessions_in_the_span_store(
    projects_root: Path, tmp_path: Path, monkeypatch
):
    """Same conversion as the push path, different sink: rows land in spans_raw stamped
    with the project as source, and the typed layout builds from them."""
    pytest.importorskip("harbor_atif2otel")
    import duckdb

    from dev_agent_lens.storage.layouts import get_layout
    from dev_agent_lens.storage.spanstore import open_store

    monkeypatch.setenv("DAL_SPAN_STORE", f"file://{tmp_path}/store")
    sessions = ingest.discover_sessions(projects_root, include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="", project="my-claude", to_store=True)
    assert report.sessions_ok == 2 and not report.failures, report.failures
    assert report.spans_sent == report.spans_total > 0

    store = open_store()
    con = duckdb.connect()
    store.attach_duckdb(con)
    rows = con.execute(
        f"SELECT source, count(*), count(DISTINCT trace_id) FROM read_parquet('{store.read_glob('spans_raw')}', "  # noqa: E501 -- complete test payload
        "hive_partitioning=true, union_by_name=true) GROUP BY 1"
    ).fetchall()
    assert rows == [("my-claude", report.spans_sent, 2)]
    lay = get_layout("typed")
    assert (
        lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == report.spans_sent
    )
