"""
Unit tests for testbed content assertions (ENG2-1487).

The testbed's original assertions only proved spans *exist* (`has_llm_spans`,
tool names). Two real capture defects shipped green against them, because both
produce well-formed but hollow spans:

* ENG2-1510 — six weeks where ~98% of ``input.value`` was the literal string
  ``redacted-by-litellm``.
* The thinking-capture gap — 16,347 adaptive-thinking spans over 30 days with
  zero thinking content stored anywhere in the span.

These tests pin ``_content_assertions`` to catch both, while staying inert
where a signal genuinely cannot apply (e.g. thinking content when the request
never asked for summaries).
"""

from __future__ import annotations

import pandas as pd

# Accessed through the module (not imported by name) so pytest doesn't try to
# collect the production TestOrchestrator/TestConfig classes as test cases.
from dev_agent_lens.testing import orchestrator as orch


def make_orchestrator() -> "orch.TestOrchestrator":
    o = orch.TestOrchestrator.__new__(orch.TestOrchestrator)
    o.config = orch.TestConfig(backend=orch.TestBackend.PHOENIX, prompt_file="minimal.txt")
    return o


def assertions_for(rows: list[dict]) -> dict[str, bool]:
    return make_orchestrator()._content_assertions(pd.DataFrame(rows))


HEALTHY = {
    "span_kind": "LLM",
    "input_value": "What is 2+2?",
    "output_messages": '[{"message": {"role": "assistant", "content": "4"}}]',
    "llm_token_count_prompt": 812,
    "raw_attributes": '{"usage_object": {"cache_read_input_tokens": 174159}}',
}


# ---------------------------------------------------------------- pass / fail


def test_healthy_span_passes_everything():
    result = assertions_for([HEALTHY])
    assert result == {
        "llm_input_content_populated": True,
        "llm_output_content_populated": True,
        "llm_token_counts_populated": True,
        "cache_token_breakdown_populated": True,
    }


def test_redaction_outage_fails_input_content():
    """ENG2-1510: the redaction sentinel must fail, not count as content."""
    result = assertions_for([{**HEALTHY, "input_value": "redacted-by-litellm"}])
    assert result["llm_input_content_populated"] is False


def test_empty_and_placeholder_inputs_fail():
    for hollow in ("", "   ", "None", "nan", "null"):
        result = assertions_for([{**HEALTHY, "input_value": hollow}])
        assert result["llm_input_content_populated"] is False, repr(hollow)


def test_empty_output_messages_fail():
    result = assertions_for([{**HEALTHY, "output_messages": "[]"}])
    assert result["llm_output_content_populated"] is False


def test_zero_token_counts_fail():
    result = assertions_for([{**HEALTHY, "llm_token_count_prompt": 0}])
    assert result["llm_token_counts_populated"] is False


def test_one_populated_span_among_hollow_ones_passes():
    """The assertion is ANY-populated, not ALL — a single live span proves capture."""
    result = assertions_for([{**HEALTHY, "input_value": "redacted-by-litellm"}, HEALTHY])
    assert result["llm_input_content_populated"] is True


# ------------------------------------------------------------------ thinking


def test_thinking_gap_fails_when_summaries_requested_but_not_stored():
    """The ENG2-1487 defect: request asked for summaries, span stores none."""
    row = {
        **HEALTHY,
        "raw_attributes": (
            '{"llm": {"invocation_parameters": '
            '"{\\"thinking\\": {\\"type\\": \\"adaptive\\", \\"display\\": \\"summarized\\"}}"}}'
        ),
    }
    result = assertions_for([row])
    assert result["thinking_content_captured"] is False


def test_thinking_captured_passes_escaped_json_form():
    row = {
        **HEALTHY,
        "raw_attributes": (
            '{"invocation_parameters": "{\\"display\\": \\"summarized\\"}", '
            '"output": "[{\\"type\\": \\"thinking\\", '
            '\\"thinking\\": \\"Let me work through this\\", \\"signature\\": \\"abc\\"}]"}'
        ),
    }
    result = assertions_for([row])
    assert result["thinking_content_captured"] is True


def test_thinking_captured_passes_dict_repr_form():
    """Flattened arize-phoenix columns stringify as python dicts (single quotes)."""
    row = {
        "span_kind": "LLM",
        "attributes.input.value": "hello",
        "attributes.llm.invocation_parameters": "{'thinking': {'display': 'summarized'}}",
        "attributes.usage_object": {
            "cache_read_input_tokens": 5000,
            "content": [{"type": "thinking", "thinking": "Consider the", "signature": "xyz"}],
        },
    }
    result = assertions_for([row])
    assert result["thinking_content_captured"] is True


def test_request_param_object_form_is_not_content():
    """`"thinking": {…}` in request params must not satisfy the content check."""
    row = {
        **HEALTHY,
        "raw_attributes": (
            '{"invocation_parameters": "{\\"thinking\\": {\\"type\\": \\"adaptive\\", '
            '\\"display\\": \\"summarized\\"}}", "output": "the answer is 4"}'
        ),
    }
    result = assertions_for([row])
    assert result["thinking_content_captured"] is False


def test_thinking_assertion_inert_without_summarized_request():
    """Today's fleet shape: adaptive + display omitted -> nothing to capture, no gate."""
    row = {
        **HEALTHY,
        "raw_attributes": (
            '{"invocation_parameters": "{\\"thinking\\": {\\"type\\": \\"adaptive\\"}}", '
            '"usage_object": {"cache_read_input_tokens": 174159}}'
        ),
    }
    result = assertions_for([row])
    assert "thinking_content_captured" not in result
    assert result["cache_token_breakdown_populated"] is True


# --------------------------------------------------------------------- cache


def test_all_zero_cache_tokens_fail():
    row = {
        **HEALTHY,
        "raw_attributes": (
            '{"usage_object": {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}'
        ),
    }
    result = assertions_for([row])
    assert result["cache_token_breakdown_populated"] is False


def test_cache_assertion_inert_when_fields_absent():
    result = assertions_for([{**HEALTHY, "raw_attributes": "{}"}])
    assert "cache_token_breakdown_populated" not in result


def test_cache_matches_dict_repr_cells():
    row = {
        "span_kind": "LLM",
        "attributes.input.value": "hello",
        "attributes.usage_object": {"cache_creation_input_tokens": 53454},
    }
    result = assertions_for([row])
    assert result["cache_token_breakdown_populated"] is True


# ------------------------------------------------------------ gating / shape


def test_no_llm_rows_yields_no_assertions():
    result = assertions_for([{"span_kind": "TOOL", "input_value": "x"}])
    assert result == {}


def test_missing_columns_yield_no_assertions_but_no_crash():
    result = assertions_for([{"span_kind": "LLM"}])
    assert result == {}


def test_flattened_phoenix_columns_are_recognized():
    """The arize-phoenix client returns attributes.* columns, not our schema names."""
    row = {
        "span_kind": "LLM",
        "attributes.input.value": "What is 2+2?",
        "attributes.llm.output_messages": [{"role": "assistant", "content": "4"}],
        "attributes.llm.token_count.prompt": 812.0,
    }
    result = assertions_for([row])
    assert result["llm_input_content_populated"] is True
    assert result["llm_output_content_populated"] is True
    assert result["llm_token_counts_populated"] is True


def test_kind_column_variant_is_honored():
    """Arize dataframes use `kind` instead of `span_kind`."""
    result = assertions_for([{**{k: v for k, v in HEALTHY.items() if k != "span_kind"}, "kind": "LLM"}])
    assert result["llm_input_content_populated"] is True


def test_prose_summarized_does_not_arm_the_gate():
    """The word 'summarized' in system-prompt/tool text (which rides inside
    invocation_parameters) must not arm the gate — only the display PARAMETER.
    Live testbed run 20260814-121204 false-armed exactly this way."""
    row = {
        **HEALTHY,
        "raw_attributes": (
            '{"invocation_parameters": "{\\"system\\": \\"context is summarized when '
            'long\\", \\"thinking\\": {\\"type\\": \\"adaptive\\", '
            '\\"display\\": \\"omitted\\"}}"}'
        ),
    }
    result = assertions_for([row])
    assert "thinking_content_captured" not in result


def test_normalize_phoenix_handles_packed_attributes():
    """PhoenixPostgresClient returns one packed `attributes` JSON column; the
    normalizer must populate content fields from it (it used to emit all-None —
    proven live on rollout session ce6da45d, 0/4 LLM rows populated)."""
    import json as _json

    from dev_agent_lens.core.schema import normalize_phoenix

    packed = {
        "input": {"value": "probe input"},
        "output": {"value": "probe output"},
        "llm": {
            "model_name": "claude-opus-5",
            "output_messages": [{"message": {"role": "assistant", "content": "DONE"}}],
            "token_count": {"prompt": 316000, "completion": 55},
        },
        "openinference": {"span": {"kind": "LLM"}},
    }
    df = pd.DataFrame(
        [
            {
                "context.span_id": "abc",
                "context.trace_id": "def",
                "parent_id": None,
                "name": "litellm_request",
                "span_kind": "LLM",
                "start_time": "2026-08-14T17:19:13Z",
                "end_time": "2026-08-14T17:19:25Z",
                "status_code": "OK",
                "attributes": _json.dumps(packed),  # packed, exactly as postgres client emits
                "llm_token_count_prompt": 316000,
            }
        ]
    )
    out = normalize_phoenix(df)
    row = out.iloc[0]
    assert row["input_value"] == "probe input"
    assert row["output_value"] == "probe output"
    assert row["llm_model_name"] == "claude-opus-5"
    assert row["llm_token_count_prompt"] == 316000
    assert row["llm_token_count_completion"] == 55
    assert "DONE" in row["output_messages"]
    # and the content assertions pass over the normalized frame
    assert assertions_for(out.to_dict("records"))["llm_input_content_populated"] is True


def test_read_roundtrip_nonce_assertion():
    """Nonce present anywhere in the spans -> True; absent -> False; no nonce set -> inert."""
    o = make_orchestrator()
    df_hit = pd.DataFrame([{**HEALTHY, "output_messages": "PIPELINE_TEST_COMPLETE NONCE=cafe0123beef"}])
    df_miss = pd.DataFrame([HEALTHY])

    o.nonce = "cafe0123beef"
    assert o._content_assertions(df_hit)["read_content_roundtrip"] is True
    assert o._content_assertions(df_miss)["read_content_roundtrip"] is False

    o.nonce = None
    assert "read_content_roundtrip" not in o._content_assertions(df_hit)
