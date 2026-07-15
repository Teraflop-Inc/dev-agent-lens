"""Tests for human-turn extraction.

The fixtures below are verbatim from the ENG2-1393 corpus (Phoenix spans, June
2026). They are the exact strings that made a naive extraction 46% wrong.
"""

from __future__ import annotations

from dev_agent_lens.core.prompts import extract_human_turns, is_human_turn

# Real prompts Yashwanth typed.
HUMAN = [
    "so its a ledger to phoenix ?",
    "so you are saying claude -p wont work as the agent traces cannot be captured??",
    "nice I understood, now implement",
    "now give me the command to delete the t7 branch and then base off  of this t66 branch",
    "okay who is exactly the warden ?, and does it live inside the sandbox?",
    "why did we need supabase ?, I forgot",
    "okay now should we test sonnet and Haiku for this corpus ?",
    "Hi, Can you formulate a bug report for Oxen?",
]

# Text the AGENT emitted that parses as a user message. Every one of these was
# admitted by the first-pass extraction and had to be thrown out afterwards.
AGENT_NOISE = [
    "Perform a web search for the query: pass@k evaluation LLM agents",
    (
        "Describe your most recent action in 3-5 words using present tense (-ing). "
        'Name the file or function, not the branch. Do not use tools.\n\nPrevious: "R'
    ),
    "The user stepped away and is coming back. Recap in under 40 words, 1-2 plain sentences.",
    "[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]",
    "The date has changed. Today's date is now 2026-07-14.",
    "[{'tool_use_id': 'toolu_01C8tt', 'type': 'tool_result', 'content': 'File created'}]",
    "<system-reminder>\nAs you answer the user's questions, you can use the following",
    "<task-notification>\n<task-id>b5650rfvj</task-id>",
    "[Request interrupted by user]",
]


class TestIsHumanTurn:
    def test_accepts_real_human_prompts(self):
        for text in HUMAN:
            assert is_human_turn(text), f"should be human: {text!r}"

    def test_rejects_agent_emitted_text(self):
        for text in AGENT_NOISE:
            assert not is_human_turn(text), f"should be agent noise: {text!r}"

    def test_rejects_web_search_template_regardless_of_query(self):
        # This one is the 46%. It reads as a perfectly natural human request,
        # which is exactly why it has to be matched structurally.
        assert not is_human_turn("Perform a web search for the query: anything at all")

    def test_rejects_empty_and_none(self):
        assert not is_human_turn(None)
        assert not is_human_turn("")
        assert not is_human_turn("   ")

    def test_rejects_turns_below_min_chars(self):
        assert not is_human_turn("ok")
        assert not is_human_turn("continue")  # 8 chars — real, but no signal

    def test_min_chars_is_tunable(self):
        assert is_human_turn("continue", min_chars=3)

    def test_matching_is_case_insensitive(self):
        assert not is_human_turn("PERFORM A WEB SEARCH FOR THE QUERY: foo")

    def test_leading_whitespace_does_not_smuggle_noise_through(self):
        assert not is_human_turn("\n\n  Perform a web search for the query: foo")


class TestExtractHumanTurns:
    def test_separates_a_mixed_corpus(self):
        kept = extract_human_turns(HUMAN + AGENT_NOISE)
        assert len(kept) == len(HUMAN)
        assert all(not t.lower().startswith("perform a web search") for t in kept)

    def test_normalizes_whitespace_in_kept_turns(self):
        (kept,) = extract_human_turns(["so  its   a\n\nledger to phoenix ?"])
        assert kept == "so its a ledger to phoenix ?"

    def test_empty_input_is_empty_output(self):
        assert extract_human_turns([]) == []

    def test_survives_none_entries(self):
        assert extract_human_turns([None, "so its a ledger to phoenix ?", None]) == [
            "so its a ledger to phoenix ?"
        ]

    def test_warns_when_drop_rate_is_high(self, caplog):
        # The real corpus sat at ~46% dropped. A caller should be told.
        extract_human_turns(HUMAN[:1] + AGENT_NOISE)
        assert any("agent-emitted" in r.message for r in caplog.records)
