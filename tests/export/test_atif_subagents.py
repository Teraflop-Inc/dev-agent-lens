"""
Unit tests for embedding subagent sidechains into a parent ATIF trajectory.

The behaviour worth pinning down: every `agent-<agentId>.jsonl` records the
PARENT's sessionId, and harbor-atif2otel seeds trace ids from `session_id` and
span ids from `{trace_id}:{trajectory_id}`. If the subagents don't each get a
distinct `trajectory_id`, a directory of sidechains collapses onto one trace
with identical span seeds and the backend drops the collisions silently.
"""

import json

from dev_agent_lens.export.atif import session_tree_to_atif

SESSION_ID = "11111111-2222-3333-4444-555555555555"
AGENT_ID = "a0123456789abcdef"


def _line(**fields):
    return {"sessionId": SESSION_ID, "version": "2.1.0", **fields}


def _parent_lines(agent_id=AGENT_ID, call_id="call-1"):
    return [
        _line(
            type="user",
            timestamp="2026-08-10T10:00:00Z",
            message={"role": "user", "content": "delegate this"},
        ),
        _line(
            type="assistant",
            timestamp="2026-08-10T10:00:01Z",
            message={
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": "Task",
                        "input": {"prompt": "go"},
                    }
                ],
            },
        ),
        _line(
            type="user",
            timestamp="2026-08-10T10:00:30Z",
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": "done"}
                ],
            },
            toolUseResult={"agentId": agent_id, "content": [{"type": "text", "text": "done"}]},
        ),
    ]


def _subagent_lines():
    return [
        _line(
            type="user",
            isSidechain=True,
            timestamp="2026-08-10T10:00:02Z",
            message={"role": "user", "content": "go"},
        ),
        _line(
            type="assistant",
            isSidechain=True,
            timestamp="2026-08-10T10:00:03Z",
            message={
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        ),
    ]


def _write(directory, name, lines):
    path = directory / name
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return path


def test_subagent_is_embedded_and_referenced(tmp_path):
    _write(tmp_path, f"{SESSION_ID}.jsonl", _parent_lines())
    _write(tmp_path, f"agent-{AGENT_ID}.jsonl", _subagent_lines())

    trajectories, stats = session_tree_to_atif(tmp_path)

    assert len(trajectories) == 1, "a sidechain file is not a session of its own"
    parent = trajectories[0]
    assert stats["subagent_linked"] == 1

    embedded = parent["subagent_trajectories"]
    assert len(embedded) == 1
    assert embedded[0]["trajectory_id"] == f"agent-{AGENT_ID}"

    refs = [
        ref
        for step in parent["steps"]
        for result in (step.get("observation") or {}).get("results", [])
        for ref in result.get("subagent_trajectory_ref", [])
    ]
    assert refs == [{"trajectory_id": f"agent-{AGENT_ID}"}]


def test_subagent_trajectory_id_is_distinct_from_the_parent(tmp_path):
    """Sharing session_id is fine; sharing trajectory_id collides span seeds."""
    _write(tmp_path, f"{SESSION_ID}.jsonl", _parent_lines())
    _write(tmp_path, f"agent-{AGENT_ID}.jsonl", _subagent_lines())

    parent = session_tree_to_atif(tmp_path)[0][0]
    child = parent["subagent_trajectories"][0]

    assert child["session_id"] == parent["session_id"]
    assert child["trajectory_id"] != parent["trajectory_id"]


def test_task_call_without_a_transcript_is_counted_not_dropped_silently(tmp_path):
    _write(tmp_path, f"{SESSION_ID}.jsonl", _parent_lines())  # no agent-*.jsonl

    trajectories, stats = session_tree_to_atif(tmp_path)

    assert stats["subagent_transcript_missing"] == 1
    assert "subagent_trajectories" not in trajectories[0]


def test_multiple_subagents_get_distinct_trajectory_ids(tmp_path):
    lines = _parent_lines(agent_id="a1", call_id="call-1")
    lines += _parent_lines(agent_id="a2", call_id="call-2")[1:]
    _write(tmp_path, f"{SESSION_ID}.jsonl", lines)
    _write(tmp_path, "agent-a1.jsonl", _subagent_lines())
    _write(tmp_path, "agent-a2.jsonl", _subagent_lines())

    parent, stats = session_tree_to_atif(tmp_path)
    embedded = parent[0]["subagent_trajectories"]

    assert stats["subagent_linked"] == 2
    assert {t["trajectory_id"] for t in embedded} == {"agent-a1", "agent-a2"}
