"""
Unit tests for the Claude Code session -> ATIF exporter.

The two behaviours worth pinning down both failed silently in practice:

  * Tool results ride on `user` lines. Treating them as human turns invents
    turns that never happened (112 of 154 user lines in a measured session).
  * ATIF observations must be `{"results": [{source_call_id, content}]}`. Emit a
    flat `{"content": ...}` and the document still validates, the OTLP push
    still returns 200, and every TOOL span lands with an empty output.
"""

from dev_agent_lens.export.atif import (
    ATIF_SCHEMA_VERSION,
    session_to_atif,
)


def _assistant(text=None, tool_use=None, thinking=None, usage=None, ts="2026-08-04T10:00:00Z"):
    content = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
    if text:
        content.append({"type": "text", "text": text})
    for call_id, name, args in tool_use or []:
        content.append({"type": "tool_use", "id": call_id, "name": name, "input": args})
    message = {"role": "assistant", "content": content, "model": "claude-opus-5"}
    if usage:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1",
            "version": "2.1.0", "message": message}


def _user_text(text, ts="2026-08-04T09:59:00Z"):
    return {"type": "user", "timestamp": ts, "sessionId": "s1",
            "message": {"role": "user", "content": text}}


def _tool_result(call_id, content, ts="2026-08-04T10:00:05Z"):
    return {"type": "user", "timestamp": ts, "sessionId": "s1",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": content}]}}


def test_produces_valid_atif_envelope():
    traj, _ = session_to_atif([_user_text("hi"), _assistant(text="hello")])
    assert traj["schema_version"] == ATIF_SCHEMA_VERSION
    assert traj["agent"]["name"] == "claude-code"
    assert traj["agent"]["version"] == "2.1.0"
    assert traj["session_id"] == "s1"
    assert [s["source"] for s in traj["steps"]] == ["user", "agent"]
    assert all("step_id" in s and "message" in s for s in traj["steps"])


def test_tool_results_are_not_counted_as_user_turns():
    lines = [
        _user_text("run ls"),
        _assistant(tool_use=[("call_1", "Bash", {"command": "ls"})]),
        _tool_result("call_1", "file_a\nfile_b"),
    ]
    traj, stats = session_to_atif(lines)

    sources = [s["source"] for s in traj["steps"]]
    assert sources == ["user", "agent"], "the tool result must not become a user turn"
    assert stats["step:user"] == 1
    assert stats["tool_result_attached"] == 1


def test_observation_uses_keyed_results_array():
    lines = [
        _assistant(tool_use=[("call_1", "Bash", {"command": "ls"})]),
        _tool_result("call_1", "file_a"),
    ]
    traj, _ = session_to_atif(lines)

    observation = traj["steps"][0]["observation"]
    # Shape matters: harbor-atif2otel keys its observation map on
    # results[].source_call_id. A flat {"content": ...} is silently dropped.
    assert set(observation) == {"results"}
    assert observation["results"] == [
        {"source_call_id": "call_1", "content": "file_a"}
    ]


def test_result_attaches_to_the_step_that_issued_the_call():
    lines = [
        _assistant(tool_use=[("call_a", "Read", {})]),
        _assistant(tool_use=[("call_b", "Bash", {})]),
        _tool_result("call_a", "from A"),
        _tool_result("call_b", "from B"),
    ]
    traj, _ = session_to_atif(lines)

    first, second = traj["steps"][0], traj["steps"][1]
    assert first["observation"]["results"][0]["content"] == "from A"
    assert second["observation"]["results"][0]["content"] == "from B"


def test_tool_calls_and_metrics_are_preserved():
    lines = [_assistant(
        tool_use=[("call_1", "Bash", {"command": "ls"})],
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
    )]
    traj, _ = session_to_atif(lines)

    step = traj["steps"][0]
    assert step["tool_calls"] == [
        {"tool_call_id": "call_1", "function_name": "Bash", "arguments": {"command": "ls"}}
    ]
    assert step["metrics"] == {
        "prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 100
    }
    assert step["model_name"] == "claude-opus-5"


def test_thinking_text_is_read_from_the_thinking_key():
    # A thinking part stores text under "thinking", not "text".
    traj, stats = session_to_atif([_assistant(thinking="deliberating", text="done")])
    assert traj["steps"][0]["reasoning_content"] == "deliberating"
    assert stats["reasoning_preserved"] == 1


def test_empty_thinking_blocks_yield_no_reasoning():
    # Claude Code writes signature-only thinking blocks in practice.
    traj, stats = session_to_atif([_assistant(thinking="", text="done")])
    assert "reasoning_content" not in traj["steps"][0]
    assert stats["reasoning_preserved"] == 0


def test_bookkeeping_lines_are_dropped():
    lines = [
        {"type": "queue-operation", "sessionId": "s1"},
        {"type": "file-history-snapshot", "sessionId": "s1"},
        {"type": "ai-title", "sessionId": "s1"},
        _user_text("real turn"),
    ]
    traj, stats = session_to_atif(lines)
    assert len(traj["steps"]) == 1
    assert stats["dropped_non_conversation"] == 3


def test_step_ids_are_sequential_from_one():
    traj, _ = session_to_atif([_user_text("a"), _assistant(text="b"), _user_text("c")])
    assert [s["step_id"] for s in traj["steps"]] == [1, 2, 3]
