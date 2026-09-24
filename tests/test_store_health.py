"""store_health mirrors AIT capture_health: same thresholds, same exit codes.

Loaded from scripts/ by path; the scripts are not a package on purpose.
"""

from __future__ import annotations

import importlib.util
import pathlib

import duckdb
import pandas as pd
import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "store_health.py"
_spec = importlib.util.spec_from_file_location("store_health", _SCRIPT)
sh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sh)

THRESH = dict(max_redacted_pct=5.0, min_p95_chars=100, min_samples=30)


def test_no_spans_is_inconclusive_not_healthy():
    rc, msgs = sh.evaluate([], **THRESH)
    assert rc == sh.EXIT_INCONCLUSIVE and "inconclusive" in msgs[0]


def test_healthy_when_nothing_is_redacted_and_p95_is_long():
    rows = [("litellm_request", 200, 0, 1500, 9000), ("Claude_Code_Tool_Bash", 50, 0, 300, 1100)]
    rc, msgs = sh.evaluate(rows, **THRESH)
    assert rc == sh.EXIT_HEALTHY and msgs[-1].startswith("healthy: 0.0% redacted, 250")


def test_redaction_above_threshold_breaches():
    rows = [("litellm_request", 200, 20, 19, 19)]
    rc, msgs = sh.evaluate(rows, **THRESH)
    assert rc == sh.EXIT_BREACH and any(
        "10.0% of input-bearing spans are redacted" in m for m in msgs
    )


def test_short_p95_breaches_only_with_enough_samples():
    tiny = [("probe", 5, 0, 12, 12)]
    assert sh.evaluate(tiny, **THRESH)[0] == sh.EXIT_HEALTHY  # 5 < min_samples: cannot vote
    voting = [("probe", 40, 0, 12, 40)]
    rc, msgs = sh.evaluate(voting, **THRESH)
    assert rc == sh.EXIT_BREACH and any("p95 input length 40" in m for m in msgs)


def test_query_reads_the_raw_store_and_matches_the_marker_exactly(tmp_path):
    from dev_agent_lens.storage.spanstore import open_store

    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    now = pd.Timestamp.now(tz="UTC")
    df = pd.DataFrame(
        {
            "span_id": ["a", "b", "c", "d"],
            "trace_id": ["t"] * 4,
            "name": ["litellm_request"] * 3 + ["Claude_Code_Tool_Bash"],
            "start_time": [now - pd.Timedelta(minutes=5)] * 4,
            "attributes": [
                '{"input":{"value":"redacted-by-litellm"}}',
                '{"input":{"value":"we talked about redacted-by-litellm in passing"}}',
                '{"input":{"value":"' + "x" * 500 + '"}}',
                '{"output":{"value":"no input field"}}',
            ],
        }
    )
    store.append_frame(con, df, "spans_raw")
    rows = sh._query(store.uri, now - pd.Timedelta(hours=1), sh.REDACTION_MARKER)
    by_name = {r[0]: r for r in rows}
    assert by_name["litellm_request"][1] == 3  # with_input
    assert by_name["litellm_request"][2] == 1  # exact marker only; the mention does not count
    assert "Claude_Code_Tool_Bash" not in by_name  # no input value, does not participate


@pytest.mark.parametrize("argv", [["--hours", "1"], ["--since", "2026-09-01T00:00:00Z"]])
def test_main_returns_operational_on_an_unreachable_store(argv, tmp_path):
    assert (
        sh.main(["--store", "s3://nope/none?endpoint=127.0.0.1:1&tls=0", *argv])
        == sh.EXIT_OPERATIONAL
    )
