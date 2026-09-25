"""Token totals from session files match the files, for Claude and Codex.

Measured on real sessions before this was fixed: Claude output tokens ~3x (usage repeated
on every content-block line), no cache writes at all, and Codex prompt/completion 2x (the
session total on the root span on top of every call). Sums over the typed layout must
equal what the harness itself recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_agent_lens.export import ingest

pytest.importorskip("harbor_atif2otel")


def _line(kind: str, ts: str, message: dict, cwd: str = "/Users/me/git/work/teraflop/x") -> dict:
    return {"type": kind, "sessionId": "tok-1", "timestamp": ts, "version": "2.1.0",
            "cwd": cwd, "message": message}


def _assistant(msg_id: str, ts: str, block: dict, usage: dict) -> dict:
    return _line("assistant", ts, {"id": msg_id, "role": "assistant", "model": "claude-opus-5",
                                   "content": [block], "usage": usage})


def _typed(tmp_path: Path):
    import duckdb

    from dev_agent_lens.storage.layouts import get_layout
    from dev_agent_lens.storage.spanstore import open_store

    store = open_store()
    con = duckdb.connect()
    lay = get_layout("typed")
    lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)
    lay.attach(con, store)
    return con


def test_claude_session_file_totals_match_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DAL_SPAN_STORE", f"file://{tmp_path}/store")
    monkeypatch.setenv("DAL_IDENTITY", str(tmp_path / "none.yaml"))
    u1 = {"input_tokens": 3, "output_tokens": 50, "cache_read_input_tokens": 1000,
          "cache_creation_input_tokens": 400}
    u2 = {"input_tokens": 2, "output_tokens": 9, "cache_read_input_tokens": 1400}
    lines = [
        _line("user", "2026-08-04T09:59:00Z", {"role": "user", "content": "fix it"}),
        _assistant("m1", "2026-08-04T10:00:00Z", {"type": "thinking", "thinking": "hm",
                                                  "signature": "s"}, u1),
        _assistant("m1", "2026-08-04T10:00:01Z", {"type": "text", "text": "on it"}, u1),
        _assistant("m1", "2026-08-04T10:00:02Z",
                   {"type": "tool_use", "id": "c1", "name": "Bash", "input": {"command": "ls"}},
                   u1),
        _line("user", "2026-08-04T10:00:03Z", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "a b"}]}),
        _assistant("m2", "2026-08-04T10:00:04Z", {"type": "text", "text": "done"}, u2),
    ]
    project = tmp_path / "projects" / "-Users-me-git-work-teraflop-x"
    project.mkdir(parents=True)
    (project / "tok-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    sessions = ingest.discover_sessions(tmp_path / "projects", include=["*teraflop*"])
    report = ingest.ingest_sessions(sessions, endpoint="", project="claude-files", to_store=True)
    assert report.sessions_ok == 1 and not report.failures, report.failures

    con = _typed(tmp_path)
    got = con.execute("""SELECT sum(tokens_prompt), sum(tokens_completion),
        sum(tokens_cache_read), sum(tokens_cache_write) FROM spans""").fetchone()
    # prompt = fresh input + cache reads, per message; completion once per message
    assert got == (1003 + 1402, 50 + 9, 1000 + 1400, 400)


def test_codex_root_total_is_not_counted_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("DAL_SPAN_STORE", f"file://{tmp_path}/store")
    monkeypatch.setenv("DAL_IDENTITY", str(tmp_path / "none.yaml"))
    from dev_agent_lens.export.store_ingest import land_trajectories

    trajectory = {
        "schema_version": "ATIF-v1.7",
        "session_id": "codex-1",
        "trajectory_id": "codex-1",
        "agent": {"name": "codex", "version": "0.1", "model_name": "gpt-5"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "go", "timestamp": "2026-08-04T10:00:00Z"},
            {"step_id": 2, "source": "agent", "message": "ok", "model_name": "gpt-5",
             "timestamp": "2026-08-04T10:00:01Z",
             "metrics": {"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 100}},
            {"step_id": 3, "source": "agent", "message": "done", "model_name": "gpt-5",
             "timestamp": "2026-08-04T10:00:02Z",
             "metrics": {"prompt_tokens": 130, "completion_tokens": 4, "cached_tokens": 120}},
        ],
        "final_metrics": {"total_prompt_tokens": 250, "total_completion_tokens": 12,
                          "total_cached_tokens": 220},
    }
    land_trajectories([trajectory], project="codex-sessions")

    con = _typed(tmp_path)
    by_kind = dict(con.execute(
        "SELECT span_kind, sum(tokens_prompt) FROM spans GROUP BY 1").fetchall())
    assert by_kind.get("AGENT") is None  # the root still exists, its rollup is not counted
    assert con.execute("SELECT sum(tokens_prompt), sum(tokens_completion) FROM spans"
                       ).fetchone() == (250, 12)
