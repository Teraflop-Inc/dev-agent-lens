"""capture_parity: the local session set and the two store channels, end to end on files."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import duckdb
import pandas as pd

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "capture_parity.py"
_spec = importlib.util.spec_from_file_location("capture_parity", _SCRIPT)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _session(dirpath: pathlib.Path, sid: str, *, assistant: bool) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "message": {"role": "user", "content": "hi"}}]
    if assistant:
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": []}})
    # Claude writes compact JSON, one object per line; the detector matches that shape.
    (dirpath / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n"
    )


def test_local_sessions_scope_by_project_glob_and_need_an_assistant_turn(tmp_path):
    root = tmp_path / "projects"
    _session(root / "-Users-me-git-teraflop-a", "s1", assistant=True)
    _session(root / "-Users-me-git-teraflop-a", "s2", assistant=False)  # a prompt with no reply
    _session(root / "-Users-me-git-teraflop-a", "agent-x", assistant=True)  # a subagent file
    _session(root / "-Users-me-git-personal", "s3", assistant=True)
    assert set(cp.local_sessions(str(root), "*teraflop*")) == {"s1"}
    assert set(cp.local_sessions(str(root), "*")) == {"s1", "s3"}


def _store_with_sessions(tmp_path: pathlib.Path, name: str, sids: list[str], typed: bool) -> str:
    from dev_agent_lens.storage.spanstore import open_store

    store = open_store(f"file://{tmp_path}/{name}")
    store.ensure()
    con = duckdb.connect()
    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame(
        {
            "span_id": [f"{s}-0" for s in sids],
            "trace_id": sids,
            "name": ["litellm_request"] * len(sids),
            "start_time": [now] * len(sids),
            "attributes": [json.dumps({"claude": {"session_id": s}}) for s in sids],
        }
    )
    store.append_frame(con, df, "spans_raw")
    if typed:
        tdf = pd.DataFrame({"span_id": df.span_id, "session_id": sids, "start_time": df.start_time})
        store.append_frame(con, tdf, "spans_typed")
    return store.uri


def test_parity_holds_when_the_jsonl_channel_covers_what_the_proxy_missed(tmp_path, capsys):
    root = tmp_path / "projects"
    for s in ("s1", "s2", "s3"):
        _session(root / "-teraflop", s, assistant=True)
    proxy = _store_with_sessions(tmp_path, "proxy", ["s1", "s2"], typed=True)
    jsonl = _store_with_sessions(tmp_path, "jsonl", ["s1", "s2", "s3"], typed=False)
    rc = cp.main(
        [
            "--include",
            "*teraflop*",
            "--sessions-dir",
            str(root),
            "--phoenix-store",
            proxy,
            "--jsonl-store",
            jsonl,
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"reached via proxy \(Phoenix-synced store\)\s+2 of 3", out)
    assert re.search(r"reached via JSONL ingest\s+3 of 3", out)
    assert "parity: holds" in out


def test_proxy_only_measurement_still_reports(tmp_path, capsys):
    root = tmp_path / "projects"
    _session(root / "-teraflop", "s1", assistant=True)
    proxy = _store_with_sessions(
        tmp_path, "proxy", ["s1"], typed=False
    )  # raw-only store: session from attributes
    assert cp.main(["--include", "*", "--sessions-dir", str(root), "--phoenix-store", proxy]) == 0
    assert re.search(r"reached via JSONL ingest\s+-", capsys.readouterr().out)
