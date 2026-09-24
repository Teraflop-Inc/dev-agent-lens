"""`dal query-spans` behind the CLI: one filter/aggregate implementation over both homes
spans have (export-parquet files, and the span store through either layout).

The command had imported two functions that never existed since 2026-01, so nothing here
had run before; these are the first tests of its behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pandas as pd
import pytest

from dev_agent_lens.query.spans_query import (
    open_export,
    open_store_spans,
    spans_list,
    spans_stats,
)
from dev_agent_lens.storage.layouts import get_layout
from dev_agent_lens.storage.spanstore import open_store

EUID = json.dumps({"account_uuid": "acct-1", "session_id": "sess-1111-2222"})


def _raw_frame() -> pd.DataFrame:
    """Four native-shape spans across two traces; only one span carries the identity."""
    return pd.DataFrame(
        {
            "context.span_id": ["a", "b", "c", "d"],
            "context.trace_id": ["t", "t", "u", "u"],
            "parent_id": [None, "a", None, "c"],
            "name": [
                "litellm_request",
                "Claude_Code_Tool_Bash",
                "litellm_request",
                "Claude_Code_Tool_Read",
            ],
            "span_kind": ["LLM", "TOOL", "LLM", "TOOL"],
            "start_time": pd.to_datetime(
                [
                    "2026-09-01T10:00:00Z",
                    "2026-09-01T10:00:01Z",
                    "2026-09-02T09:00:00Z",
                    "2026-09-02T09:00:01Z",
                ]
            ),
            "end_time": pd.to_datetime(
                [
                    "2026-09-01T10:00:01Z",
                    "2026-09-01T10:00:02Z",
                    "2026-09-02T09:00:03Z",
                    "2026-09-02T09:00:04Z",
                ]
            ),
            "status_code": ["OK", "OK", "ERROR", "OK"],
            "status_message": [""] * 4,
            "attributes": [
                json.dumps(
                    {
                        "llm": {"model_name": "claude-opus-5"},
                        "metadata": {"user_api_key_end_user_id": EUID},
                    }
                ),
                "{}",
                json.dumps({"llm": {"model_name": "claude-sonnet-5"}}),
                "{}",
            ],
            "events": ["[]"] * 4,
            "cumulative_error_count": [0] * 4,
            "cumulative_llm_token_count_prompt": [1] * 4,
            "cumulative_llm_token_count_completion": [1] * 4,
            "llm_token_count_prompt": [10, 0, 7, 0],
            "llm_token_count_completion": [2, 0, 1, 0],
        }
    )


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("SET TimeZone='UTC'")
    return c


@pytest.fixture
def raw_store(tmp_path, con):
    s = open_store(f"file://{tmp_path}/store")
    s.ensure()
    assert s.append_frame(con, _raw_frame(), "spans_raw") == 4
    return s


@pytest.fixture
def export_dir(tmp_path):
    """The export-parquet shape: flattened rows, hive-partitioned by source and week."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [
        {
            "session_id": "sess-1111-2222",
            "span_id": "a",
            "trace_id": "t",
            "name": "litellm_request",
            "status_code": "OK",
            "start_time": datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            "llm_model_name": "claude-opus-5",
        },
        {
            "session_id": "sess-1111-2222",
            "span_id": "b",
            "trace_id": "t",
            "name": "Claude_Code_Tool_Bash",
            "status_code": "OK",
            "start_time": datetime(2026, 9, 1, 10, 0, 1, tzinfo=timezone.utc),
            "llm_model_name": None,
        },
        {
            "session_id": None,
            "span_id": "c",
            "trace_id": "u",
            "name": "litellm_request",
            "status_code": "ERROR",
            "start_time": datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
            "llm_model_name": "claude-sonnet-5",
        },
    ]
    d = tmp_path / "parquet" / "spans" / "source=src" / "week=2026-W36"
    d.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), d / "part-00000.parquet")
    return tmp_path / "parquet" / "spans" / "source=src"


class TestExportShape:
    def test_stats_and_list(self, con, export_dir):
        rel = open_export(con, export_dir)
        st = spans_stats(con, rel)
        assert (st["total_spans"], st["session_count"]) == (3, 1)
        assert st["status_code_counts"] == {"OK": 2, "ERROR": 1}
        assert st["top_models"] == {"(none)": 1, "claude-opus-5": 1, "claude-sonnet-5": 1}
        rows = spans_list(con, rel, limit=10)
        assert [r["span_id"] for r in rows] == ["c", "b", "a"]  # newest first

    def test_filters_combine(self, con, export_dir):
        rel = open_export(con, export_dir)
        rows = spans_list(con, rel, session_id="sess-1111-2222", name_pattern="tool")
        assert [r["span_id"] for r in rows] == ["b"]
        assert spans_stats(con, rel, model_name="SONNET")["total_spans"] == 1

    def test_single_legacy_file_also_works(self, con, tmp_path, export_dir):
        import pyarrow.parquet as pq

        f = tmp_path / "src_spans.parquet"
        pq.write_table(pq.read_table(next(export_dir.rglob("*.parquet"))), f)
        assert spans_stats(con, open_export(con, f))["total_spans"] == 3

    def test_missing_path_is_a_file_not_found(self, con, tmp_path):
        with pytest.raises(FileNotFoundError):
            open_export(con, tmp_path / "nope")


class TestStoreRawLayout:
    def test_session_is_propagated_across_the_trace(self, con, raw_store):
        """Only span `a` carries the end-user metadata; `b` in the same trace must still
        answer to the session filter, as the typed layout's identity table would."""
        rel = open_store_spans(con, raw_store, "raw")
        rows = spans_list(con, rel, session_id="sess-1111-2222")
        assert sorted(r["span_id"] for r in rows) == ["a", "b"]
        st = spans_stats(con, rel)
        # trace `u` carries no identity, so it counts as its own session, keyed by trace_id
        assert (st["total_spans"], st["session_count"]) == (4, 2)
        assert [r["span_id"] for r in spans_list(con, rel, session_id="u")] == ["d", "c"]
        assert st["top_models"] == {"(none)": 2, "claude-opus-5": 1, "claude-sonnet-5": 1}

    def test_status_and_model_filters(self, con, raw_store):
        rel = open_store_spans(con, raw_store, "raw")
        assert [r["span_id"] for r in spans_list(con, rel, status_code="ERROR")] == ["c"]
        assert spans_stats(con, rel, model_name="opus")["status_code_counts"] == {"OK": 1}

    def test_empty_store_is_a_file_not_found(self, con, tmp_path):
        with pytest.raises(FileNotFoundError, match="holds no spans_raw"):
            open_store_spans(con, open_store(f"file://{tmp_path}/empty"), "raw")

    def test_filter_values_are_bound_not_spliced(self, con, raw_store):
        rel = open_store_spans(con, raw_store, "raw")
        assert spans_list(con, rel, session_id="x' OR 1=1 --") == []
        assert spans_stats(con, rel, name_pattern="%' OR '1'='1")["total_spans"] == 0


class TestStoreTypedLayout:
    def test_typed_columns_are_used_directly(self, con, raw_store):
        get_layout("typed").build(con, raw_store.read_glob("spans_raw"), raw_store, zstd_level=1)
        rel = open_store_spans(con, raw_store, "typed")
        assert rel.model == "model_name"
        st = spans_stats(con, rel)
        assert (st["total_spans"], st["session_count"]) == (4, 2)
        assert [r["span_id"] for r in spans_list(con, rel, session_id="u")] == ["d", "c"]
        rows = spans_list(con, rel, session_id="sess-1111-2222")
        assert [r["span_id"] for r in rows] == ["b", "a"]
