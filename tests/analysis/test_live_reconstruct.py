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

import inspect
import json

from dev_agent_lens.analysis.live_reconstruct import build_session_records
from dev_agent_lens.export.markdown_renderer import (
    COMPACTION_SUMMARY_INLINE_LIMIT,
    _render_compaction_section,
    render_jsonl_to_markdown,
)


def _span(start, model, user_content, assistant_content, tokens=(10, 5)):
    """Build a synthetic live litellm_request span.

    user_content / assistant_content are Python-repr block-list strings, exactly
    as Phoenix stores `attributes.llm.{input,output}_messages[i].message.content`.
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


def test_compaction_marker_on_haiku_span_is_detected():
    """Regression for ENG2-1441 — the shape a synthetic clean test can't catch.

    On real prod/seed data the ONLY span carrying the post-compaction
    continuation marker is a *haiku* background call (a title/topic call that
    carries the resumed context); no main-model span in the window carries it.
    The marker also sits several blocks deep in a python-repr content string and
    uses the "The summary below covers..." phrasing (no `SUMMARY_MARKER`). The
    original code skipped all haiku spans BEFORE detection, so it reported 0
    compactions on a genuinely compacted session. Detection must fire anyway.
    """
    continuation = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion of the "
        "conversation.\\n\\nSummary:\\n## 1. Primary Request and Intent\\n\\n"
        "The user asked to implement ticket ENG2-1431."
    )
    haiku_span = _span(
        "2026-07-23T12:00:05",
        "claude-haiku-4-5-20251001",
        # Multiple blocks, marker is NOT first — mirrors the real cumulative
        # history the haiku call receives as context.
        "[{'type': 'text', 'text': '<system-reminder>\\nnoise\\n</system-reminder>'}, "
        f'{{\'type\': \'text\', \'text\': "{continuation}"}}]',
        "[{'type': 'text', 'text': 'ancillary haiku output'}]",
    )
    opus_span = _span(
        "2026-07-23T12:00:06",
        "claude-opus-4-8",
        "[{'type': 'text', 'text': 'a real human turn after the compaction here'}]",
        "[{'type': 'text', 'text': 'picking up where we left off'}]",
    )
    records = build_session_records("s", [haiku_span, opus_span])
    header = records[0]
    evts = _events(records)

    compactions = [e for e in evts if e["event_type"] == "compaction"]
    assert len(compactions) == 1, "haiku-only continuation marker must be counted"
    assert header["compaction_count"] == 1
    # Summary is bounded to the continuation block, not the whole history.
    assert compactions[0]["summary"].startswith("This session is being continued")
    assert "ENG2-1431" in compactions[0]["summary"]
    assert "<system-reminder>" not in compactions[0]["summary"]
    # The haiku span's ancillary body is still excluded from the conversation.
    assert all(e.get("text") != "ancillary haiku output" for e in evts)
    assert header["metrics"]["models_used"] == {"claude-opus-4-8": 1}


def test_compaction_deduped_across_cumulative_spans():
    """The same continuation persists in later spans' cumulative history on prod;
    it must count as ONE compaction, not one per span carrying it."""
    continuation = (
        "This session is being continued from a previous conversation that ran "
        "out of context.\\n\\nSummary:\\n## 1. Primary Request and Intent"
    )
    user_content = f'[{{\'type\': \'text\', \'text\': "{continuation}"}}]'
    spans = [
        _span("2026-07-23T12:00:00", "claude-opus-4-8", user_content,
              "[{'type': 'text', 'text': 'reply one'}]"),
        _span("2026-07-23T12:00:01", "claude-opus-4-8", user_content,
              "[{'type': 'text', 'text': 'reply two'}]"),
    ]
    records = build_session_records("s", spans)
    compactions = [e for e in _events(records) if e["event_type"] == "compaction"]
    assert len(compactions) == 1
    assert records[0]["compaction_count"] == 1


def test_two_distinct_compactions_counted_separately():
    """Dedup must collapse the SAME continuation across cumulative spans, but
    must NOT collapse two DIFFERENT compactions in one session — even though
    real summaries share a long boilerplate preamble. Guards the dedup-key
    window against the ENG2-1441 class of 'clean assumption breaks on real
    data'."""
    preamble = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion of the "
        "conversation.\\n\\nSummary:\\n## 1. Primary Request and Intent\\n\\n"
    )
    first = preamble + "The user asked to implement ticket ENG2-1431."
    second = preamble + "The user asked to refactor the export pipeline."
    spans = [
        _span("2026-07-23T12:00:00", "claude-opus-4-8",
              f'[{{\'type\': \'text\', \'text\': "{first}"}}]',
              "[{'type': 'text', 'text': 'reply one'}]"),
        _span("2026-07-23T13:00:00", "claude-opus-4-8",
              f'[{{\'type\': \'text\', \'text\': "{second}"}}]',
              "[{'type': 'text', 'text': 'reply two'}]"),
    ]
    records = build_session_records("s", spans)
    compactions = [e for e in _events(records) if e["event_type"] == "compaction"]
    assert len(compactions) == 2
    assert records[0]["compaction_count"] == 2
    assert compactions[0]["number"] == 1 and compactions[1]["number"] == 2


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


# ── ENG2-1446: no dangling `→ Full summary` sidecar link ──────────────────────

def test_long_compaction_summary_renders_inline_with_no_sidecar_link():
    """A compaction summary longer than the old inline limit must render fully
    inline — NO `→ Full summary` link and NO `compaction_N_summary` sidecar.

    The reconstruct-live/reconstruct-session CLIs write only `main_content` and
    discard `tool_result_files`, so any such link would dangle (ENG2-1446).
    """
    long_summary = "SUMMARY_SENTINEL " + ("blah " * COMPACTION_SUMMARY_INLINE_LIMIT)
    assert len(long_summary) > COMPACTION_SUMMARY_INLINE_LIMIT  # guard the premise

    tool_result_files: dict[str, str] = {}
    lines = _render_compaction_section(
        1,
        long_summary,
        pipeline="litellm",
        tool_result_files=tool_result_files,
    )
    out = "\n".join(lines)

    # No dangling pointer of any form.
    assert "Full summary" not in out
    assert "compaction_" not in out
    assert ".txt" not in out
    # And nothing was stashed for a sidecar the CLI would never write.
    assert tool_result_files == {}
    # The summary itself is present inline (bounded, but not linked away).
    assert "SUMMARY_SENTINEL" in out
    assert "> **Context Summary**:" in out


def test_reconstruct_live_export_has_no_compaction_sidecar_file():
    """End-to-end through the shared renderer: a long compaction summary leaves
    no `compaction_*` entry in `export.tool_result_files` and no dangling link
    in `main_content`."""
    long_summary = "END_TO_END_SENTINEL " + ("x " * COMPACTION_SUMMARY_INLINE_LIMIT)
    records = [
        {"record_type": "header", "session_id": "sess-1446", "compaction_count": 1},
        {
            "record_type": "event",
            "event_type": "compaction",
            "number": 1,
            "summary": long_summary,
        },
    ]
    export = render_jsonl_to_markdown(records)

    assert "Full summary" not in export.main_content
    assert "END_TO_END_SENTINEL" in export.main_content
    assert not any(k.startswith("compaction_") for k in export.tool_result_files)


def test_claude_pipeline_still_writes_sidecar_and_link():
    """Gate boundary: the `dal reconstruct` (pipeline="claude") path DOES persist
    `tool_result_files` to disk (via `export_to_files`), so it keeps the
    truncate-inline + `→ Full summary` sidecar link for long summaries. ENG2-1446
    only drops the link on the live path that discards those files — don't let a
    future refactor silently strip the sidecar the Claude path actually writes."""
    long_summary = "CLAUDE_SENTINEL " + ("blah " * COMPACTION_SUMMARY_INLINE_LIMIT)

    tool_result_files: dict[str, str] = {}
    lines = _render_compaction_section(
        1,
        long_summary,
        pipeline="claude",
        tool_result_files=tool_result_files,
    )
    out = "\n".join(lines)

    assert "→ Full summary: [compaction_1_summary.txt](./compaction_1_summary.txt)" in out
    assert tool_result_files["compaction_1_summary"] == long_summary


def test_reconstruct_session_litellm_path_has_no_dangling_link():
    """Criterion 2: the reconstruct-session renderer (markdown_litellm) already
    renders the compaction summary inline with no `→ Full summary` pointer, so it
    never emitted the dangling link. Pin that so it can't regress into one."""
    from dev_agent_lens.export import markdown_litellm

    src = inspect.getsource(markdown_litellm)
    # The litellm renderer must not introduce a compaction sidecar pointer.
    assert "Full summary" not in src
    assert "compaction_{" not in src  # no f-string sidecar filename
    # It does render the summary inline under a Context Summary blockquote.
    assert "Previous Context Summary" in src
