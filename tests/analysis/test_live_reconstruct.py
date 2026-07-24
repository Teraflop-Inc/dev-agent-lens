"""
Tests for live session reconstruction (ENG2-1435).

These build synthetic `litellm_request` spans (the shape live Supabase-Phoenix
stores) and assert the JSONL records fed to the markdown renderer contain the
right conversation events:
- genuine human turns (not system-reminders / tool-result echoes)
- assistant text
- tool results (unpaired, since live spans don't record tool_use blocks)
- Haiku ancillary calls are excluded (main-thread parity with `dal reconstruct`)
- post-compaction summaries render as compaction events
"""

from __future__ import annotations

import json

from dev_agent_lens.analysis.live_reconstruct import build_session_records
from dev_agent_lens.export.markdown_renderer import render_jsonl_to_markdown


def _span(
    start, model, user_content, assistant_content, tokens=(10, 5), input_value=None
):
    """Build a synthetic live litellm_request span.

    user_content / assistant_content are Python-repr block-list strings, exactly
    as Phoenix stores `attributes.llm.{input,output}_messages[i].message.content`.
    `input_value` optionally sets `attributes.input.value` (the serialized
    request string) to exercise the input.value fallback.
    """
    attrs = {
        "llm": {
            "model_name": model,
            "input_messages": [{"message": {"role": "user", "content": user_content}}],
            "output_messages": [
                {"message": {"role": "assistant", "content": assistant_content}}
            ],
        }
    }
    if input_value is not None:
        attrs["input"] = {"value": input_value}
    return {
        "name": "litellm_request",
        "start_time": start,
        "end_time": start,
        "trace_id": "t1",
        "raw_attributes_json": json.dumps({"attributes": attrs}),
        "llm_token_count_prompt": tokens[0],
        "llm_token_count_completion": tokens[1],
    }


def _events(records):
    return [r for r in records if r.get("record_type") == "event"]


def test_reconstructs_human_turn_and_assistant():
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'how do we tell unorouter which model to use?'}]",
            "[{'type': 'text', 'text': 'You configure it via the router config.'}]",
        ),
    ]
    records = build_session_records("sess-1", spans)
    evts = _events(records)
    assert evts[0]["event_type"] == "user"
    assert "unorouter" in evts[0]["text"]
    assert evts[1]["event_type"] == "assistant"
    assert "router config" in evts[1]["text"]


def test_system_reminders_are_not_human_turns():
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': '<system-reminder>\\nnoise\\n</system-reminder>'}, "
            "{'type': 'text', 'text': 'actual human question here please'}]",
            "[{'type': 'text', 'text': 'sure'}]",
        ),
    ]
    evts = _events(build_session_records("s", spans))
    user_turns = [e for e in evts if e["event_type"] == "user"]
    assert len(user_turns) == 1
    assert user_turns[0]["text"] == "actual human question here please"


def test_haiku_ancillary_spans_excluded():
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-haiku-4-5-20251001",
            "[{'type': 'text', 'text': 'quota'}]",
            "[{'type': 'text', 'text': '#'}]",
        ),
        _span(
            "2026-07-23T12:00:01",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'real human turn that is long enough'}]",
            "[{'type': 'text', 'text': 'ok'}]",
        ),
    ]
    records = build_session_records("s", spans)
    header = records[0]
    evts = _events(records)
    # Haiku call fully dropped: no '#' assistant turn, model not counted
    assert all("#" != e.get("text") for e in evts)
    assert "claude-haiku-4-5-20251001" not in header["metrics"]["models_used"]
    assert header["metrics"]["models_used"] == {"claude-opus-4-8": 1}


def test_tool_result_rendered_when_no_tool_use():
    # Live spans carry tool_result in the *next* input but never a tool_use.
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'please list the vault files for me'}]",
            "[{'type': 'text', 'text': 'let me check'}]",
        ),
        _span(
            "2026-07-23T12:00:01",
            "claude-opus-4-8",
            "[{'type': 'tool_result', 'tool_use_id': 'toolu_1', "
            "'content': './a\\n./b', 'is_error': False}]",
            "[{'type': 'text', 'text': 'found two files'}]",
        ),
    ]
    evts = _events(build_session_records("s", spans))
    tools = [e for e in evts if e["event_type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["result"] == "./a\n./b"


def test_tool_use_paired_with_result_is_not_double_rendered():
    # Defensive path: if an assistant output DOES carry a tool_use block, the
    # matching tool_result on the next span must fill that same tool event
    # (one tool event, result populated) — not emit a second standalone one.
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'read the file for me please now'}]",
            "[{'type': 'text', 'text': 'reading it'}, "
            "{'type': 'tool_use', 'id': 'toolu_9', 'name': 'Read', "
            "'input': {'file_path': '/tmp/x'}}]",
        ),
        _span(
            "2026-07-23T12:00:01",
            "claude-opus-4-8",
            "[{'type': 'tool_result', 'tool_use_id': 'toolu_9', "
            "'content': 'file body', 'is_error': False}]",
            "[{'type': 'text', 'text': 'done reading'}]",
        ),
    ]
    evts = _events(build_session_records("s", spans))
    tools = [e for e in evts if e["event_type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["name"] == "Read"
    assert tools[0]["input"] == {"file_path": "/tmp/x"}
    assert tools[0]["result"] == "file body"


def test_compaction_summary_becomes_event():
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'This session is being continued from a previous "
            "conversation. The conversation is summarized below: we did X and Y.'}]",
            "[{'type': 'text', 'text': 'continuing'}]",
        ),
    ]
    records = build_session_records("s", spans)
    header = records[0]
    evts = _events(records)
    compactions = [e for e in evts if e["event_type"] == "compaction"]
    assert len(compactions) == 1
    assert "we did X and Y" in compactions[0]["summary"]
    assert header["compaction_count"] == 1


def test_compaction_detected_from_input_value_fallback():
    # The continuation marker may live only in the serialized input.value, not
    # the structured input_messages. It must still register as compaction.
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'ordinary continuing content here'}]",
            "[{'type': 'text', 'text': 'continuing'}]",
            input_value=(
                "This session is being continued from a previous conversation. "
                "The conversation is summarized below: we did A and B."
            ),
        ),
    ]
    records = build_session_records("s", spans)
    header = records[0]
    evts = _events(records)
    compactions = [e for e in evts if e["event_type"] == "compaction"]
    assert len(compactions) == 1
    assert "we did A and B" in compactions[0]["summary"]
    assert header["compaction_count"] == 1


def test_compaction_not_double_counted_when_marker_in_both_shapes():
    # Marker present in BOTH the structured message and input.value must emit
    # exactly one compaction event, not two.
    marker = (
        "This session is being continued from a previous conversation. "
        "The conversation is summarized below: recap text."
    )
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            f"[{{'type': 'text', 'text': '{marker}'}}]",
            "[{'type': 'text', 'text': 'continuing'}]",
            input_value=marker,
        ),
    ]
    records = build_session_records("s", spans)
    compactions = [e for e in _events(records) if e["event_type"] == "compaction"]
    assert len(compactions) == 1
    assert records[0]["compaction_count"] == 1


def test_summarization_task_span_is_skipped_entirely():
    # A compaction-summary *call* (task marker in input.value) is not
    # conversation — the whole span is skipped, so neither its prompt nor the
    # summary output leaks in as user/assistant turns.
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'placeholder'}]",
            "[{'type': 'text', 'text': 'HERE IS THE GIANT SUMMARY OUTPUT'}]",
            input_value="Your task is to create a detailed summary of the conversation",
        ),
        _span(
            "2026-07-23T12:00:01",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'a real human turn after the summary call'}]",
            "[{'type': 'text', 'text': 'ok'}]",
        ),
    ]
    evts = _events(build_session_records("s", spans))
    # Summary output must not appear; only the real turn survives.
    assert all("GIANT SUMMARY" not in e.get("text", "") for e in evts)
    users = [e for e in evts if e["event_type"] == "user"]
    assert len(users) == 1
    assert "real human turn" in users[0]["text"]


def test_output_at_parity_with_renderer():
    """The records must render cleanly through the shared markdown renderer."""
    spans = [
        _span(
            "2026-07-23T12:00:00",
            "claude-opus-4-8",
            "[{'type': 'text', 'text': 'a genuine question from the human user'}]",
            "[{'type': 'text', 'text': 'a genuine answer'}]",
        ),
    ]
    records = build_session_records("sess-parity", spans)
    export = render_jsonl_to_markdown(records)
    assert "# Session: sess-par" in export.main_content
    assert "## Conversation" in export.main_content
    assert "### User" in export.main_content
    assert "### Assistant" in export.main_content
    assert export.stats["user_turns"] == 1
    assert export.stats["assistant_turns"] == 1
