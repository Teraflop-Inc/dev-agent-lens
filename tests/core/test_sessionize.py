"""Tests for working-session segmentation.

The headline case is the real one from ENG2-1393: a single Claude Code
`session_id` that was resumed daily for 28 days. Grouping by it yields one
"session"; segmenting on idle gaps yields the actual days of work.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from dev_agent_lens.core.sessionize import segment, summarize

T0 = datetime(2026, 6, 15, 9, 0)


def _spans(offsets_minutes, *, session_id=None, tokens=100):
    """Build a span frame from minute-offsets since T0."""
    rows = [
        {
            "start_time": T0 + timedelta(minutes=m),
            "tokens": tokens,
            **({"session_id": session_id} if session_id else {}),
        }
        for m in offsets_minutes
    ]
    return pd.DataFrame(rows)


class TestSegment:
    def test_contiguous_work_is_one_block(self):
        df = segment(_spans([0, 5, 10, 15]))
        assert df["work_block"].nunique() == 1

    def test_a_long_gap_starts_a_new_block(self):
        # 0..10 then a 90-minute silence, then more work.
        df = segment(_spans([0, 5, 10, 100, 105]))
        assert df["work_block"].nunique() == 2
        assert list(df["work_block"]) == [1, 1, 1, 2, 2]

    def test_gap_exactly_at_threshold_does_not_split(self):
        # 30 minutes is "still working"; the split is on strictly greater.
        df = segment(_spans([0, 30]))
        assert df["work_block"].nunique() == 1

    def test_gap_just_over_threshold_splits(self):
        df = segment(_spans([0, 31]))
        assert df["work_block"].nunique() == 2

    def test_idle_gap_is_tunable(self):
        df = segment(_spans([0, 45]), idle_gap=timedelta(hours=2))
        assert df["work_block"].nunique() == 1

    def test_unsorted_input_is_sorted_first(self):
        df = segment(_spans([100, 0, 5]))
        assert list(df["start_time"]) == sorted(df["start_time"])
        assert list(df["work_block"]) == [1, 1, 2]

    def test_empty_input_yields_empty_frame_with_column(self):
        df = segment(pd.DataFrame(columns=["start_time"]))
        assert df.empty
        assert "work_block" in df.columns

    def test_missing_time_column_raises(self):
        with pytest.raises(KeyError, match="start_time"):
            segment(pd.DataFrame({"nope": [1]}))

    def test_the_28_day_resumed_thread(self, caplog):
        """The ENG2-1393 defect, in miniature.

        One session_id, resumed each morning for five days. Grouping by
        session_id says "1 session". The truth is five days of work.
        """
        offsets = []
        for day in range(5):
            morning = day * 24 * 60
            offsets += [morning, morning + 10, morning + 20, morning + 30]

        df = _spans(offsets, session_id="61018e79-37f7-4f70-b4c9-3cf65fba6f5b")

        assert df["session_id"].nunique() == 1  # what the raw data claims
        out = segment(df)
        assert out["work_block"].nunique() == 5  # what actually happened

        # And the caller is warned not to trust session_id.
        assert any("not a working session" in r.message for r in caplog.records)


class TestSummarize:
    def test_summarizes_each_block(self):
        sessions = summarize(_spans([0, 5, 10, 15, 100, 105, 110, 115]))
        assert len(sessions) == 2
        assert sessions[0].spans == 4
        assert sessions[0].minutes == 15.0
        assert sessions[0].tokens == 400

    def test_drops_background_chatter_blocks(self):
        # A 4-span work block, plus a lone stray span 5 hours later.
        sessions = summarize(_spans([0, 5, 10, 15, 300]))
        assert len(sessions) == 1
        assert sessions[0].spans == 4

    def test_min_spans_is_tunable(self):
        sessions = summarize(_spans([0, 5, 10, 15, 300]), min_spans=1)
        assert len(sessions) == 2

    def test_empty_input(self):
        assert summarize(pd.DataFrame(columns=["start_time"])) == []

    def test_tolerates_missing_token_column(self):
        df = pd.DataFrame({"start_time": [T0, T0 + timedelta(minutes=1)] * 2})
        sessions = summarize(df, min_spans=1)
        assert sessions[0].tokens == 0
