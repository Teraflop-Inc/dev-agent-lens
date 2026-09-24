"""oxen_import: the 2025 flat shape becomes the store's raw shape, with provenance."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib

import duckdb
import pandas as pd

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "oxen_import.py"
_spec = importlib.util.spec_from_file_location("oxen_import", _SCRIPT)
ox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ox)

KW = dict(
    source="src",
    commit="abc123",
    path="parquet/spans/source=src/week=2025-W25/part-00000.parquet",
    now="2026-09-11T00:00:00+00:00",
)


def test_dotted_keys_nest_and_flat_columns_fold_in_only_when_missing():
    r = {
        "span_id": "a",
        "trace_id": "t",
        "parent_id": "",
        "name": "litellm_request",
        "span_kind": "",
        "start_time": dt.datetime(2025, 6, 21, 4, 35),
        "end_time": None,
        "status_code": "OK",
        "input_value": "hello",
        "output_value": "",
        "input_messages": None,
        "output_messages": None,
        "llm_model_name": "claude-x",
        "session_id": "s1",
        "llm_token_count_prompt": 10,
        "llm_token_count_completion": 2,
        "raw_attributes_json": json.dumps(
            {
                "attributes.llm.model_name": "from-raw",
                "attributes.llm.request.max_tokens": 8,
                "latency_ms": 5.0,
            }
        ),
    }
    row = ox.row_to_raw(r, **KW)
    a = json.loads(row["attributes"])
    assert a["llm"]["model_name"] == "from-raw"  # raw wins over the flat column
    assert a["llm"]["request"]["max_tokens"] == 8 and a["latency_ms"] == 5.0
    assert a["input"]["value"] == "hello" and a["session"]["id"] == "s1"
    assert "output" not in a  # empty flat value is not folded in
    assert row["parent_id"] is None and row["span_kind"] is None
    assert (
        row["source"] == "oxen/src"
        and row["oxen_commit"] == "abc123"
        and row["oxen_path"].endswith("part-00000.parquet")
    )
    assert "_import" not in a


def test_week_unknown_rows_recover_time_from_the_raw_object():
    r = {
        "span_id": "b",
        "trace_id": "t",
        "name": "litellm_request",
        "start_time": None,
        "end_time": None,
        "raw_attributes_json": json.dumps(
            {"time": "2025-07-02 15:50:58.865000+00:00", "latency_ms": 1500.0}
        ),
    }
    row = ox.row_to_raw(r, **KW)
    assert row["start_time"] == dt.datetime(2025, 7, 2, 15, 50, 58, 865000, tzinfo=dt.timezone.utc)
    assert row["end_time"] == row["start_time"] + dt.timedelta(milliseconds=1500)
    assert json.loads(row["attributes"])["_import"] == {
        "start_time_from": "raw.time",
        "end_time_from": "raw.time + latency_ms",
    }


def test_a_row_with_no_time_anywhere_is_unparseable():
    assert (
        ox.row_to_raw({"span_id": "c", "start_time": None, "raw_attributes_json": "{}"}, **KW)
        is None
    )


def _write_oxen_file(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)  # noqa: F841 - DuckDB reads it by name below
    duckdb.sql("SELECT * FROM df").write_parquet(str(path))


def test_import_source_reconciles_read_landed_duplicates_and_bad(tmp_path):
    from dev_agent_lens.storage.spanstore import open_store

    work = tmp_path / "work"
    base = work / "parquet" / "spans" / "source=src"
    t = dt.datetime(2025, 6, 21, 4, 35)
    rows = [
        {
            "span_id": "a",
            "trace_id": "t",
            "name": "n",
            "start_time": t,
            "raw_attributes_json": "{}",
        },
        {
            "span_id": "b",
            "trace_id": "t",
            "name": "n",
            "start_time": t,
            "raw_attributes_json": "{}",
        },
        {
            "span_id": "a",
            "trace_id": "t",
            "name": "n",
            "start_time": t,
            "raw_attributes_json": "{}",
        },  # Oxen duplicate
    ]
    _write_oxen_file(base / "week=2025-W25" / "part-00000.parquet", rows)
    _write_oxen_file(
        base / "week=unknown" / "part-00000.parquet",
        [
            {
                "span_id": "c",
                "trace_id": "t",
                "name": "n",
                "start_time": None,
                "raw_attributes_json": json.dumps({"time": "2025-07-01 00:00:00+00:00"}),
            },
            {
                "span_id": "d",
                "trace_id": "t",
                "name": "n",
                "start_time": None,
                "raw_attributes_json": "{}",
            },
        ],
    )
    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    rep = ox.import_source(str(work), "src", store=store, con=con, commit="abc", batch=2)
    assert (rep["rows_read"], rep["rows_landed"], rep["duplicate_ids_in_oxen"]) == (5, 3, 1)
    assert rep["unparseable"] == [
        {
            "span_id": "d",
            "path": "parquet/spans/source=src/week=unknown/part-00000.parquet",
            "why": "no start_time",
        }
    ]
    n, srcs = con.execute(
        f"SELECT count(*), count(DISTINCT source) FROM read_parquet('{store.read_glob('spans_raw')}', hive_partitioning=true, union_by_name=true)"  # noqa: E501
    ).fetchone()
    assert (n, srcs) == (3, 1)
    # A second import lands nothing twice.
    rep2 = ox.import_source(str(work), "src", store=store, con=con, commit="abc", batch=2)
    assert (rep2["rows_landed"], rep2["rows_already_present"]) == (0, 3)
