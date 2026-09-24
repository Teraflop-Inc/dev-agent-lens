"""Drift tooling, on a synthetic fingerprint series that contains every case we care about,
including the one that fooled the ad-hoc scripts."""

from __future__ import annotations

import json

import pytest

from dev_agent_lens.drift import deep_shape
from dev_agent_lens.drift.classify import classify, coverage_classes
from dev_agent_lens.drift.detector import backtest, build_contract, check
from dev_agent_lens.drift.fingerprints import load

TOTAL = 1000


def _day(day, paths):
    """paths: list of (path, jtype, rows). Adds an anchor path carrying the day's total."""
    entries = [{"path": "anchor", "jtype": "string", "n": TOTAL, "rows": TOTAL}]
    entries += [{"path": p, "jtype": j, "n": r, "rows": r} for p, j, r in paths]
    return {"day": day, "paths": entries}


@pytest.fixture
def series(tmp_path):
    """Ten live days plus a partial eleventh.

    - llm.model_name: stable, string, 100%.
    - metadata.user_api_key_end_user_id: string on ~98% of rows AND null on ~2% EVERY day.
      This is the two-entry case: a loader that does not sum reads the 2% and calls it a
      collapse. The database says 98%.
    - llm.token_count.prompt: string days 1-5, number days 6-10 -> TYPEFLIP.
    - metadata.usage_object.server_tool_use: 0.3% coverage -> SPARSE, not drift.
    - claude.cwd: appears day 6 -> APPEAR; present through day 10.
    - input.value: present days 1-10 except day 4 -> GAP.
    - output.value: present days 1-7 only -> VANISH.
    - extra.poly: string and number on the same days -> POLYMORPH.
    - extra.gone: untyped, present days 1-6 only -> VANISH on an UNTYPED path (WARN).
    - llm.model_name coverage: 100% days 1-5, then 30% -> COVERAGE_DROP after a 1-5 baseline.
    """
    lines = []
    for i in range(1, 12):
        day = f"2026-06-{i:02d}"
        paths = [
            ("llm.model_name", "string", TOTAL if i <= 5 else 300),
            ("metadata.user_api_key_end_user_id", "string", 980),
            ("metadata.user_api_key_end_user_id", "null", 20),
            ("llm.token_count.prompt", "string" if i <= 5 else "number", TOTAL),
            ("metadata.usage_object.server_tool_use", "object", 3),
            ("extra.poly", "string", 500),
            ("extra.poly", "number", 500),
        ]
        if i >= 6:
            paths.append(("claude.cwd", "string", 300))
        if i != 4:
            paths.append(("input.value", "string", 900))
        if i <= 7:
            paths.append(("output.value", "string", 850))
        if i <= 6:
            paths.append(("extra.gone", "string", 400))
        rec = _day(day, paths)
        if i == 11:  # partial last day
            for e in rec["paths"]:
                e["rows"] = e["n"] = max(1, e["rows"] // 100)
        lines.append(json.dumps(rec))
    f = tmp_path / "fp.jsonl"
    f.write_text("\n".join(lines) + "\n")
    return f


class TestLoader:
    def test_rows_are_summed_across_jsonb_types(self, series):
        days = load(series)
        pd_ = days["2026-06-03"].paths["metadata.user_api_key_end_user_id"]
        assert pd_.rows == 1000 and pd_.types == {"string", "null"}
        assert pd_.cov == pytest.approx(1.0)
        # the bug: reading either single entry gives 0.98 or 0.02, never 1.0

    def test_thin_day_is_not_live_even_when_kept(self, series):
        """Day 11 has 10 rows < THIN_DAY_ROWS, so it is excluded on its own merits."""
        from dev_agent_lens.drift.fingerprints import live_days

        assert "2026-06-11" not in live_days(load(series), drop_last=False)

    def test_full_last_day_is_dropped_by_default(self, tmp_path):
        """drop_last removes the final day regardless of size: a sweep mid-day is partial."""
        from dev_agent_lens.drift.fingerprints import live_days

        lines = [
            json.dumps(_day(f"2026-08-{i:02d}", [("llm.model_name", "string", TOTAL)]))
            for i in (1, 2, 3)
        ]
        f = tmp_path / "full.jsonl"
        f.write_text("\n".join(lines) + "\n")
        assert live_days(load(f)) == ["2026-08-01", "2026-08-02"]
        assert live_days(load(f), drop_last=False) == ["2026-08-01", "2026-08-02", "2026-08-03"]


class TestClassify:
    def test_event_kinds(self, series):
        ev = {(e.kind, e.path) for e in classify(load(series))}
        assert ("TYPEFLIP", "llm.token_count.prompt") in ev
        assert ("POLYMORPH", "extra.poly") in ev
        assert ("APPEAR", "claude.cwd") in ev
        assert ("GAP", "input.value") in ev
        assert ("VANISH", "output.value") in ev
        # the two-type identity path must NOT be flagged as anything
        assert not any(p == "metadata.user_api_key_end_user_id" for _, p in ev)

    def test_typeflip_is_not_polymorph(self, series):
        kinds = {e.kind for e in classify(load(series)) if e.path == "llm.token_count.prompt"}
        assert kinds == {"TYPEFLIP"}

    def test_coverage_classes(self, series):
        cc = coverage_classes(load(series))
        assert cc["llm.model_name"][0] == "STABLE"
        assert cc["metadata.user_api_key_end_user_id"][0] == "STABLE"  # summed, not collapsed
        assert cc["metadata.usage_object.server_tool_use"][0] == "SPARSE"
        assert cc["output.value"][0] == "GAPPY"


class TestDetector:
    def test_disappeared_retyped_and_coverage_drop(self, series):
        days = load(series)
        contract = build_contract(days, "2026-06-01", "2026-06-05")
        f6 = {(k, p) for _, k, p, _ in check(days["2026-06-06"], contract)}
        assert ("RETYPED", "llm.token_count.prompt") in f6
        assert ("NEW_PATH", "claude.cwd") in f6
        f8 = {(k, p) for _, k, p, _ in check(days["2026-06-08"], contract)}
        assert ("DISAPPEARED", "output.value") in f8
        assert ("COVERAGE_DROP", "llm.model_name") in f8  # 100% -> 30% against a 1-5 baseline

    def test_typed_paths_are_errors_untyped_are_warnings(self, series):
        days = load(series)
        contract = build_contract(days, "2026-06-01", "2026-06-05")
        sev = {p: s for s, k, p, _ in check(days["2026-06-08"], contract) if k == "DISAPPEARED"}
        assert sev["output.value"] == "ERROR"  # typed
        assert sev["extra.gone"] == "WARN"  # untyped: lands in overflow, still worth knowing

    def test_backtest_only_covers_days_after_baseline(self, series):
        days = load(series)
        out = backtest(days, build_contract(days, "2026-06-01", "2026-06-05"))
        assert min(out) > "2026-06-05"


class TestDeepShape:
    def test_shapes(self):
        assert deep_shape.shape(None) == "absent"
        assert deep_shape.shape("abc") == "bare-scalar"
        assert deep_shape.shape("{not json") == "unparseable"
        assert deep_shape.shape('{"b":1,"a":2}') == "json-object{a,b}"

    def test_the_real_august_transition_fires_error(self):
        july = {"unparseable": 0.898, "json-object{max_tokens,stream}": 0.093}
        aug = {
            "json-object{max_tokens,stream,thinking}": 0.96,
            "json-object{max_tokens,stream}": 0.035,
        }
        kinds = {(s, k) for s, k, _ in deep_shape.compare(july, aug)}
        assert ("ERROR", "SHAPE_SHIFT") in kinds


class TestNullFloodIsLoud:
    """A typed column that goes all-NULL is the scenario the ticket exists to make loud.

    Found by review 2026-09-04: `real_types()` strips 'null' so RETYPED never fires, and
    summing rows across ALL jsonb types (the fix for the earlier false collapse) counts null
    rows as coverage, so COVERAGE_DROP never fires either. Both tools returned nothing for
    5 string days followed by 6 null-only days. These tests fail until that is fixed.
    """

    @pytest.fixture
    def null_flood(self, tmp_path):
        lines = []
        for i in range(1, 12):
            j = "string" if i <= 5 else "null"
            lines.append(
                json.dumps(
                    _day(
                        f"2026-07-{i:02d}",
                        [("llm.model_name", j, TOTAL), ("input.value", "string", 900)],
                    )
                )
            )
        f = tmp_path / "nf.jsonl"
        f.write_text("\n".join(lines) + "\n")
        return f

    def test_loader_tracks_non_null_coverage_separately(self, null_flood):
        days = load(null_flood)
        pd_ = days["2026-07-08"].paths["llm.model_name"]
        assert pd_.rows == TOTAL  # presence: still summed across types
        assert pd_.cov_nonnull == pytest.approx(0.0)  # population: nothing readable

    def test_classify_emits_nulled_for_a_typed_path(self, null_flood):
        ev = {(e.kind, e.path) for e in classify(load(null_flood))}
        assert ("NULLED", "llm.model_name") in ev

    def test_detector_fires_error_on_null_flood(self, null_flood):
        days = load(null_flood)
        contract = build_contract(days, "2026-07-01", "2026-07-05")
        f = {(s, k, p) for s, k, p, _ in check(days["2026-07-08"], contract)}
        assert ("ERROR", "COVERAGE_DROP", "llm.model_name") in f or (
            "ERROR",
            "NULLED",
            "llm.model_name",
        ) in f
