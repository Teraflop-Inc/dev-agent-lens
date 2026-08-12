#!/usr/bin/env python3
"""Tests for scripts/capture_health.py (ENG2-1510).

The verdict logic is pure, so it is tested without a database. The cases that
matter are not "does it add up" -- they encode the two specific ways this check
could be built wrong, both of which happened for real during the ENG2-1510
investigation:

1. A denominator that includes spans with no input field turns a TOTAL blackout
   into an apparent partial one (98.6% -> 66%), which is what made the outage
   look like it had a surviving-content population and delayed the diagnosis.
2. A threshold that only alarms on growth cannot see this failure at all --
   that is span_size_sentinel.py, which scored the outage as an improvement and
   stayed silent for nine days.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from capture_health import _HAS_INPUT, _IS_REDACTED, _SQL, evaluate  # noqa: E402

THRESHOLDS = {"max_redacted_pct": 5.0, "min_median_chars": 100}

# Real numbers pulled from phoenix.spans on 2026-08-12, mid-outage. Kept literal
# so this test fails if the check ever stops recognising the outage it exists for.
OUTAGE_ROWS = [
    ("litellm_request", 2475, 2408, 19),
    ("Claude_Code_Internal_Prompt_0", 2409, 2409, 19),
]

HEALTHY_ROWS = [
    ("litellm_request", 2475, 3, 581),
    ("Claude_Code_Internal_Prompt_0", 2409, 0, 505),
]


def test_healthy_capture_passes():
    code, msgs = evaluate(HEALTHY_ROWS, **THRESHOLDS)
    assert code == 0, msgs
    assert any("healthy" in m for m in msgs)


def test_the_real_outage_is_caught():
    """The regression case. These are the actual 2026-08-03..12 numbers."""
    code, msgs = evaluate(OUTAGE_ROWS, **THRESHOLDS)
    assert code == 1
    joined = "\n".join(msgs)
    assert "BREACH" in joined
    # Both signals should fire: the rate AND the collapsed median.
    assert "redacted" in joined
    assert "median input length" in joined


def test_empty_window_is_inconclusive_not_healthy():
    """No spans must never read as 'healthy' -- a dead pipeline is not a pass."""
    code, msgs = evaluate([], **THRESHOLDS)
    assert code == 2
    assert "inconclusive" in msgs[0]


def test_sql_counts_only_input_bearing_spans():
    """Structural guard for lesson #1 in the module docstring.

    Claude_Code_Final_Output_0 is an output span with no input.value field. It
    can never carry the redaction marker, so counting it scores it as "not
    redacted" and dilutes the rate. If this filter is ever dropped, a total
    blackout will report as roughly two-thirds and look partial.
    """
    assert _HAS_INPUT in _SQL


def test_including_a_no_input_span_type_would_mask_the_breach():
    """Demonstrates the stakes of the filter above, at the verdict level.

    Same outage, but with the output span type folded into the denominator as
    'not redacted'. The rate drops below a naively-chosen threshold and the
    check would go green during a total content blackout.
    """
    diluted = [*OUTAGE_ROWS, ("Claude_Code_Final_Output_0", 2409, 0, 4096)]
    code, _ = evaluate(diluted, max_redacted_pct=70.0, min_median_chars=1)
    assert code == 0, "precondition: dilution hides the breach at a loose threshold"

    code, _ = evaluate(OUTAGE_ROWS, max_redacted_pct=70.0, min_median_chars=1)
    assert code == 1, "undiluted, the same thresholds still catch it"


def test_partial_regression_on_one_span_type_is_caught_by_median():
    """A regression that hits only one span type keeps the overall rate low.

    The worst-median rule is what catches it -- an aggregate-only check would
    pass this.
    """
    rows = [
        ("litellm_request", 5000, 0, 581),
        ("Claude_Code_Internal_Prompt_0", 200, 200, 19),
    ]
    code, msgs = evaluate(rows, **THRESHOLDS)
    assert code == 1, msgs
    assert any("median input length" in m for m in msgs)


def test_redaction_predicate_is_exact_not_substring():
    """Lesson #3, found during live post-deploy verification on 2026-08-12.

    litellm replaces the WHOLE value when it redacts -- all 9,308 redacted spans
    in the outage had input.value exactly equal to the 19-char marker, one
    distinct value.

    A `LIKE '%marker%'` predicate also matches any span whose content merely
    MENTIONS the marker, which includes every trace of an engineer debugging
    this outage. In the live check that inflated a true 0.00% to 6.8% and
    failed the run -- the "redacted" spans were 616-3613 chars of an operator's
    own session discussing the bug.

    Crying wolf while someone investigates a capture problem is the one time
    this check must be trustworthy, so the predicate stays exact.
    """
    assert "LIKE" not in _IS_REDACTED.upper(), (
        "substring matching re-introduces the 2026-08-12 false positive"
    )
    assert "= 'redacted-by-litellm'" in _IS_REDACTED
