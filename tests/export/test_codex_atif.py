"""
Codex session -> ATIF (ENG2-402), and Codex discovery for `dal ingest-sessions --agent codex`.

Fixtures are synthetic but shaped from 50 real sessions measured 2026-09-23: the current
envelope format (``{"timestamp", "type", "payload"}``) and the older flat format (a
``{"id", "timestamp", "instructions"}`` header, then bare items with no timestamps).

The behaviours that matter, each a way a converter goes quietly wrong:

  * ``event_msg`` echoes every turn; counting both it and ``response_item`` doubles them.
  * Codex writes injected context (``<environment_context>``, AGENTS.md) as user-role
    messages. Recording those as user turns invents turns nobody typed.
  * One model response is one agent step, with every tool call it made; outputs attach by
    ``call_id``, or the TOOL spans land empty.
  * ``token_count`` repeats unchanged; only a change in the running total is a new call.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_agent_lens.export import ingest
from dev_agent_lens.export.codex_atif import (
    codex_session_file_to_atif,
    codex_session_to_atif,
    read_codex_cwd,
)

SESSION = "019e0000-1111-7222-8333-444455556666"


def _env(ts: str, rtype: str, payload: dict) -> dict:
    return {"timestamp": ts, "type": rtype, "payload": payload}


def _context(cwd: str) -> str:
    """The context Codex injects as a user-role message."""
    return f"<environment_context>\n  <cwd>{cwd}</cwd>\n</environment_context>"


def _usage(inp: int, out: int, cached: int = 0, reasoning: int = 0) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cached_input_tokens": cached,
        "reasoning_output_tokens": reasoning,
        "total_tokens": inp + out,
    }


def envelope_session(cwd: str = "/work/teraflop/repo") -> list[dict]:
    return [
        _env(
            "2026-09-23T10:00:00Z",
            "session_meta",
            {
                "id": SESSION,
                "cli_version": "0.156.1",
                "cwd": cwd,
                "git": {"branch": "adam/eng2-402-codex"},
                "originator": "codex_cli_rs",
            },
        ),
        _env(
            "2026-09-23T10:00:00Z",
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": _context(cwd),
                    }
                ],
            },
        ),
        _env("2026-09-23T10:00:01Z", "turn_context", {"cwd": cwd, "model": "gpt-5.5"}),
        _env(
            "2026-09-23T10:00:01Z",
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list the files"}],
            },
        ),
        # The UI echo of the same user turn: must not become a second step.
        _env(
            "2026-09-23T10:00:01Z",
            "event_msg",
            {"type": "user_message", "message": "list the files"},
        ),
        _env(
            "2026-09-23T10:00:02Z",
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "I should run ls."}],
                "encrypted_content": "gAAAA...",
            },
        ),
        _env(
            "2026-09-23T10:00:02Z",
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_a",
                "arguments": json.dumps({"cmd": "ls"}),
            },
        ),
        _env(
            "2026-09-23T10:00:02Z",
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "call_b",
                "input": "*** Begin Patch",
                "status": "completed",
            },
        ),
        _env(
            "2026-09-23T10:00:03Z",
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": _usage(100, 20),
                    "last_token_usage": _usage(100, 20, 40, 5),
                },
            },
        ),
        # Unchanged re-emit (rate-limit refresh): not a second model call.
        _env(
            "2026-09-23T10:00:03Z",
            "event_msg",
            {
                "type": "token_count",
                "info": {"total_token_usage": _usage(100, 20), "last_token_usage": _usage(100, 20)},
            },
        ),
        _env(
            "2026-09-23T10:00:04Z",
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call_a",
                "output": "a.py\nb.py",
            },
        ),
        _env(
            "2026-09-23T10:00:04Z",
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call_b",
                "output": json.dumps(
                    {"output": "Success. Updated a.py", "metadata": {"exit_code": 0}}
                ),
            },
        ),
        _env(
            "2026-09-23T10:00:05Z",
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Two files: a.py and b.py."}],
            },
        ),
        _env(
            "2026-09-23T10:00:06Z",
            "event_msg",
            {
                "type": "token_count",
                "info": {"total_token_usage": _usage(250, 35), "last_token_usage": _usage(150, 15)},
            },
        ),
    ]


def flat_session() -> list[dict]:
    return [
        {"id": SESSION, "timestamp": "2025-08-07T20:16:24.247Z", "instructions": None},
        {"record_type": "state"},
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": _context("/work/teraflop/old"),
                }
            ],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "fix the test"}],
        },
        {
            "type": "function_call",
            "name": "shell",
            "call_id": "c1",
            "arguments": json.dumps({"command": ["pytest"]}),
        },
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"output": "1 passed", "metadata": {"exit_code": 0}}),
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Fixed."}],
        },
    ]


class TestEnvelopeFormat:
    def test_one_step_per_model_response_and_no_double_counted_turns(self):
        trajectory, stats = codex_session_to_atif(envelope_session())
        sources = [s["source"] for s in trajectory["steps"]]
        # context injection -> system; the typed turn once (not echoed); two responses.
        assert sources == ["system", "user", "agent", "agent"]
        assert stats["dropped_event_msg"] == 1  # the user_message echo

    def test_tool_calls_and_outputs_join_by_call_id(self):
        trajectory, stats = codex_session_to_atif(envelope_session())
        first = trajectory["steps"][2]
        assert [c["function_name"] for c in first["tool_calls"]] == ["exec_command", "apply_patch"]
        assert first["tool_calls"][0]["arguments"] == {"cmd": "ls"}
        results = {r["source_call_id"]: r["content"] for r in first["observation"]["results"]}
        # The older {"output": ...} wrapper is unwrapped to the text a person would read.
        assert results == {"call_a": "a.py\nb.py", "call_b": "Success. Updated a.py"}
        assert stats["orphan_tool_result"] == 0

    def test_reasoning_summary_model_and_tokens_land_on_the_step_they_measured(self):
        trajectory, stats = codex_session_to_atif(envelope_session())
        first, second = trajectory["steps"][2], trajectory["steps"][3]
        assert first["reasoning_content"] == "I should run ls."
        assert first["model_name"] == "gpt-5.5"
        assert first["metrics"] == {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 40,
            "extra": {"reasoning_output_tokens": 5},
        }
        assert second["message"] == "Two files: a.py and b.py."
        assert second["metrics"]["prompt_tokens"] == 150
        assert stats["metrics_preserved"] == 2  # the unchanged re-emit is not a call

    def test_trajectory_header_names_the_harness(self):
        trajectory, _ = codex_session_to_atif(envelope_session())
        assert trajectory["session_id"] == SESSION
        assert trajectory["git_branch"] == "adam/eng2-402-codex"
        assert trajectory["agent"] == {
            "name": "codex",
            "version": "0.156.1",
            "model_name": "gpt-5.5",
        }
        assert trajectory["final_metrics"]["total_prompt_tokens"] == 250
        assert all(s.get("timestamp") for s in trajectory["steps"])


    def test_two_calls_on_one_step_add_up_and_step_totals_equal_the_session_total(self):
        """A reasoning-only call then the answer land on one agent step. Replacing the
        first call's usage undercounted output by ~6% on a real session."""
        records = envelope_session()
        at = next(i for i, r in enumerate(records)
                  if r["payload"].get("type") == "token_count")
        extra_call = _env(
            "2026-09-23T10:00:02Z",
            "event_msg",
            {"type": "token_count",
             "info": {"total_token_usage": _usage(130, 27),
                      "last_token_usage": _usage(30, 7, 10, 2)}},
        )
        records.insert(at + 1, extra_call)
        # every later running total now includes the extra call; the re-emit stays a re-emit
        for r in records[at + 2:]:
            info = r["payload"].get("info") if r["payload"].get("type") == "token_count" else None
            if info and info["total_token_usage"]["input_tokens"] == 100:
                info["total_token_usage"] = _usage(130, 27)
            elif info and info["total_token_usage"]["input_tokens"] == 250:
                info["total_token_usage"] = _usage(280, 42)
        trajectory, _ = codex_session_to_atif(records)
        first = trajectory["steps"][2]
        assert first["metrics"] == {
            "prompt_tokens": 130,
            "completion_tokens": 27,
            "cached_tokens": 50,
            "extra": {"reasoning_output_tokens": 7},
        }
        steps_total = sum((st.get("metrics") or {}).get("prompt_tokens") or 0
                          for st in trajectory["steps"])
        assert steps_total == trajectory["final_metrics"]["total_prompt_tokens"] == 280


class TestFlatFormat:
    def test_older_sessions_convert_with_the_header_id_and_start_time(self):
        trajectory, stats = codex_session_to_atif(flat_session())
        assert trajectory["session_id"] == SESSION
        assert [s["source"] for s in trajectory["steps"]] == ["system", "user", "agent", "agent"]
        tool_step = trajectory["steps"][2]
        assert tool_step["tool_calls"][0]["arguments"] == {"command": ["pytest"]}
        assert tool_step["observation"]["results"][0]["content"] == "1 passed"
        # The format has no per-line clock: every step carries the session start.
        assert {s["timestamp"] for s in trajectory["steps"]} == {"2025-08-07T20:16:24.247Z"}
        assert stats["dropped_other"] == 1  # {"record_type": "state"}


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


class TestFiles:
    def test_file_conversion_falls_back_to_the_uuid_in_the_file_name(self, tmp_path):
        records = [r for r in envelope_session() if r["type"] != "session_meta"]
        path = _write(tmp_path / f"rollout-2026-09-23T10-00-00-{SESSION}.jsonl", records)
        trajectory, _ = codex_session_file_to_atif(path)
        assert trajectory["session_id"] == SESSION

    def test_cwd_comes_from_meta_or_from_the_injected_context(self, tmp_path):
        new = _write(tmp_path / "a" / "rollout-a.jsonl", envelope_session("/work/teraflop/x"))
        old = _write(tmp_path / "b" / "rollout-b.jsonl", flat_session())
        assert read_codex_cwd(new) == "/work/teraflop/x"
        assert read_codex_cwd(old) == "/work/teraflop/old"


class TestDiscovery:
    def _tree(self, root: Path) -> None:
        _write(
            root / "2026/09/23" / f"rollout-2026-09-23T10-00-00-{SESSION}.jsonl",
            envelope_session("/work/teraflop/repo"),
        )
        _write(
            root / "2026/09/22" / "rollout-2026-09-22T10-00-00-personal.jsonl",
            envelope_session("/home/me/side-project"),
        )
        # A session opened and closed with nothing in it: header only.
        _write(
            root / "2026/09/21" / "rollout-2026-09-21T10-00-00-empty.jsonl",
            [{"id": "e", "timestamp": "2026-09-21T10:00:00Z", "instructions": None}],
        )

    def test_include_scopes_by_recorded_cwd_and_empty_sessions_are_skipped(self, tmp_path):
        self._tree(tmp_path)
        found = ingest.discover_codex_sessions(tmp_path, include=["*teraflop*"])
        assert [s.project_path for s in found] == ["/work/teraflop/repo"]
        assert found[0].agent == "codex"

    def test_include_is_required_and_cannot_be_everything(self, tmp_path):
        self._tree(tmp_path)
        with pytest.raises(ValueError):
            ingest.discover_codex_sessions(tmp_path, include=[])
        with pytest.raises(ValueError):
            ingest.discover_codex_sessions(tmp_path, include=["*"])

    def test_since_filters_on_last_activity(self, tmp_path):
        self._tree(tmp_path)
        old = next(tmp_path.rglob(f"*{SESSION}.jsonl"))
        stamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(old, (stamp, stamp))
        found = ingest.discover_codex_sessions(
            tmp_path, include=["*teraflop*"], since=datetime(2026, 6, 1, tzinfo=timezone.utc)
        )
        assert found == []

    def test_a_codex_session_converts_through_the_ingest_dispatch(self, tmp_path):
        self._tree(tmp_path)
        (session,) = ingest.discover_codex_sessions(tmp_path, include=["*teraflop*"])
        (trajectory,) = ingest.session_to_trajectories(session)
        assert trajectory["agent"]["name"] == "codex"


class TestSpans:
    def test_codex_spans_carry_the_harness_as_service_and_agent_name(self):
        pytest.importorskip("harbor_atif2otel")
        from dev_agent_lens.export.otlp import build_spans

        trajectory, _ = codex_session_to_atif(envelope_session())
        resource, spans, _ = build_spans([trajectory], "codex-test")
        service = {a.key: a.value.string_value for a in resource.resource.attributes}[
            "service.name"
        ]
        assert service == "codex"  # not the old claude-code default
        roots = [s for _, s in spans if not s.parent_span_id]
        assert [a.value.string_value for a in roots[0].attributes if a.key == "agent.name"] == [
            "codex"
        ]
        assert len(spans) > len(trajectory["steps"])  # LLM plus TOOL spans, not just the root


class TestCliDefaultUser:
    """Codex files name no account, so without a user every Codex span has person NULL."""

    def _run(self, tmp_path, monkeypatch, *extra):
        from click.testing import CliRunner

        import dev_agent_lens.cli.main as cli

        TestDiscovery()._tree(tmp_path)
        seen = {}

        def fake_ingest(sessions, **kwargs):
            seen["user_id"] = kwargs.get("user_id")
            return ingest.IngestReport(sessions_total=len(list(sessions)))

        monkeypatch.setattr(cli, "_git_user_email", lambda: "developer@example.com")
        monkeypatch.setattr(ingest, "ingest_sessions", fake_ingest)
        result = CliRunner().invoke(
            cli.main,
            [
                "ingest-sessions",
                "--agent",
                "codex",
                "--sessions-dir",
                str(tmp_path),
                "--include",
                "*teraflop*",
                "--to-store",
                "--project",
                "p",
                "--yes",
                *extra,
            ],
        )
        return result, seen

    def test_codex_ingest_defaults_the_user_to_the_git_email(self, tmp_path, monkeypatch):
        result, seen = self._run(tmp_path, monkeypatch)
        assert seen["user_id"] == "developer@example.com", result.output
        # The email itself is not echoed, only its domain.
        assert "@example.com" in result.output and "developer@" not in result.output

    def test_an_explicit_user_wins(self, tmp_path, monkeypatch):
        _, seen = self._run(tmp_path, monkeypatch, "--user", "adam")
        assert seen["user_id"] == "adam"
