#!/usr/bin/env python3
"""Tests for scripts/span_size_sentinel.py (ENG2-1476).

This sentinel shipped 2026-08-04 with NO tests, and the ENG2-1510 postmortem
found that gap mattered: the check was deployed and green throughout a nine-day
total content blackout, because the outage made spans SMALLER and this check
only fires when they get bigger.

So the load-bearing test here is not "does the arithmetic work" — it is
test_cannot_detect_content_loss, which pins that blind spot in place as a
documented, asserted property. Anyone who later assumes this check covers
capture health has a failing test to read.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from span_size_sentinel import evaluate  # noqa: E402

KB = 1024
THRESHOLDS = {"avg_kb_threshold": 10.0, "p95_kb_threshold": 50.0}


def test_healthy_sizes_pass():
    code, msgs = evaluate(500, 2 * KB, 4 * KB, 8 * KB, **THRESHOLDS)
    assert code == 0
    assert "healthy" in msgs[-1]


def test_avg_breach_fails():
    code, msgs = evaluate(500, 20 * KB, 30 * KB, 40 * KB, **THRESHOLDS)
    assert code == 1
    assert any("SIZE BREACH" in m for m in msgs)


def test_p95_breach_fails_even_when_avg_is_fine():
    """The bloat pattern is a few whales, not a uniform shift — avg alone hides it."""
    code, msgs = evaluate(500, 3 * KB, 80 * KB, 900 * KB, **THRESHOLDS)
    assert code == 1
    assert any("SIZE BREACH" in m for m in msgs)


def test_empty_window_is_inconclusive_not_healthy():
    """A dead pipeline emits no spans. That must not read as a pass."""
    code, msgs = evaluate(0, None, None, None, **THRESHOLDS)
    assert code == 2
    assert "inconclusive" in msgs[0]


def test_cannot_detect_content_loss():
    """THE POINT OF THIS FILE. Pins the blind spot found by ENG2-1510.

    During the 2026-08-03..12 outage every request body was replaced by a
    19-character marker. Spans went from ~70KB to under 1KB and this sentinel
    reported healthy for nine consecutive days, because shrinking is what it is
    built to approve of.

    This asserts that behaviour deliberately rather than pretending otherwise:
    the check is directional by design, and scripts/capture_health.py is the
    inverse. Both must be green. If someone ever makes this check fail on tiny
    spans, they will break this test and should read the docstring before
    "fixing" it — a small span is normal, an EMPTY one is not, and telling those
    apart needs content inspection, which lives in the other script.
    """
    # Real shape of the outage: ~900-byte spans carrying only a redaction marker.
    code, msgs = evaluate(2475, 900, 1100, 1300, **THRESHOLDS)
    assert code == 0, "size sentinel is expected to pass here — that is the blind spot"
    assert "healthy" in msgs[-1]
