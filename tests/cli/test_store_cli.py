"""`dal store status` / `dal store query`: the read side of the span store.

Runs against a local directory store; the S3 backends are covered live by
`scripts/verify_spanstore.py`. What can regress silently here is the CLI contract: which
datasets status lists, what query prints, and how it fails.
"""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pytest
from click.testing import CliRunner

from dev_agent_lens.cli.main import main
from dev_agent_lens.storage.spanstore import open_store


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "context.span_id": ["a", "b", "c"],
            "context.trace_id": ["t", "t", "u"],
            "parent_id": [None, "a", None],
            "name": ["litellm_request", "Bash", "litellm_request"],
            "span_kind": ["LLM", "TOOL", "LLM"],
            "start_time": pd.to_datetime(
                ["2026-09-01T10:00:00Z", "2026-09-01T10:00:01Z", "2026-09-02T09:00:00Z"]
            ),
            "end_time": pd.to_datetime(
                ["2026-09-01T10:00:01Z", "2026-09-01T10:00:02Z", "2026-09-02T09:00:03Z"]
            ),
            "status_code": ["OK", "OK", "ERROR"],
            "attributes": [
                '{"llm":{"model_name":"claude-x"},"session":{"id":"s1"}}',
                "{}",
                '{"llm":{"model_name":"claude-y"}}',
            ],
            "events": ["[]"] * 3,
            "llm_token_count_prompt": [10, 0, 7],
            "llm_token_count_completion": [2, 0, 1],
        }
    )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def chosen_store(runner, monkeypatch, tmp_path):
    """A chosen local store holding three raw spans over two days."""
    monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path / "data"))
    monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
    uri = f"file://{tmp_path}/store"
    r = runner.invoke(main, ["store", "use", uri])
    assert r.exit_code == 0, r.output
    s = open_store(uri)
    s.ensure()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    assert s.append_frame(con, _frame(), "spans_raw") == 3
    return s


class TestStoreStatus:
    def test_lists_the_fixed_datasets_and_anything_else_present(self, runner, chosen_store):
        con = duckdb.connect()
        # a dataset no layout writes (what export-parquet lands) must still show up
        chosen_store.append_frame(con, _frame(), "export/src/spans")
        r = runner.invoke(main, ["store", "status"])
        assert r.exit_code == 0, r.output
        lines = [ln.strip() for ln in r.output.splitlines()]
        assert any(
            ln.startswith("spans_raw") and "3 rows" in ln and "2 files" in ln for ln in lines
        )
        assert any(ln.startswith("spans_typed") and ln.endswith("empty") for ln in lines)
        assert any(ln.startswith("blobs_typed") and ln.endswith("empty") for ln in lines)
        assert any(ln.startswith("export/src/spans") and "3 rows" in ln for ln in lines)

    def test_time_range_is_printed_in_utc(self, runner, chosen_store):
        r = runner.invoke(main, ["store", "status"])
        assert "2026-09-01 10:00 .. 2026-09-02 09:00" in r.output


class TestStoreQuery:
    def test_table_output_reads_the_raw_layout(self, runner, chosen_store):
        r = runner.invoke(
            main, ["store", "query", "SELECT day, count(*) AS n FROM spans GROUP BY 1 ORDER BY 1"]
        )
        assert r.exit_code == 0, r.output
        assert "2026-09-01  2" in r.output and "2026-09-02  1" in r.output
        assert "2 row(s)" in r.output and "[raw layout" in r.output

    def test_json_output_is_records(self, runner, chosen_store):
        r = runner.invoke(
            main,
            [
                "store",
                "query",
                "--format",
                "json",
                "SELECT count(*) AS n FROM spans WHERE status_code='ERROR'",
            ],
        )
        assert r.exit_code == 0, r.output
        assert json.loads(r.output) == [{"n": 1}]

    def test_csv_output_from_stdin(self, runner, chosen_store):
        r = runner.invoke(
            main,
            ["store", "query", "-f", "-", "--format", "csv"],
            input="SELECT name, count(*) AS c FROM spans GROUP BY 1 ORDER BY 1;\n",
        )
        assert r.exit_code == 0, r.output
        assert r.output.splitlines() == ["name,c", "Bash,1", "litellm_request,2"]

    def test_limit_truncates_the_table_and_says_so(self, runner, chosen_store):
        r = runner.invoke(
            main, ["store", "query", "--limit", "2", "SELECT span_id FROM spans ORDER BY 1"]
        )
        assert r.exit_code == 0, r.output
        assert "1 more row(s)" in r.output
        assert r.output.count("\n") >= 4

    def test_bad_sql_is_a_clean_error(self, runner, chosen_store):
        r = runner.invoke(main, ["store", "query", "SELECT nope FROM spans"])
        assert r.exit_code == 1
        assert "BinderException" in r.output and "Traceback" not in r.output

    def test_sql_must_come_from_exactly_one_place(self, runner, chosen_store):
        assert runner.invoke(main, ["store", "query"]).exit_code == 2
        both = runner.invoke(main, ["store", "query", "SELECT 1", "-f", "-"], input="SELECT 1")
        assert both.exit_code == 2

    def test_empty_store_says_what_to_run(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        runner.invoke(main, ["store", "use", f"file://{tmp_path}/empty"])
        r = runner.invoke(main, ["store", "query", "SELECT 1"])
        assert r.exit_code == 1
        assert "holds no spans_raw data yet" in r.output and "dal sync" in r.output

    def test_no_layout_chosen_and_typed_exists_reads_typed(self, runner, chosen_store, monkeypatch):
        # A fresh install sets no layout anywhere (the runbook does not). Once the typed
        # layout has been built, `spans` should be the typed view, not the raw one.
        monkeypatch.delenv("DAL_SPAN_LAYOUT", raising=False)
        glob = chosen_store.read_glob("spans_raw")
        r = runner.invoke(
            main, ["store", "verify", "--from-parquet", glob], env={"DAL_SPAN_LAYOUT": "typed"}
        )
        assert r.exit_code == 0, r.output
        r = runner.invoke(
            main, ["store", "query", "SELECT count(*) AS n FROM spans WHERE model_name IS NOT NULL"]
        )
        assert r.exit_code == 0, r.output
        assert "[typed layout" in r.output
        # An explicit choice still wins.
        r = runner.invoke(main, ["store", "query", "--layout", "raw", "SELECT count(*) FROM spans"])
        assert r.exit_code == 0 and "[raw layout" in r.output

    def test_typed_layout_override_without_typed_data(self, runner, chosen_store):
        r = runner.invoke(main, ["store", "query", "--layout", "typed", "SELECT 1"])
        assert r.exit_code == 1
        assert "holds no spans_typed data yet" in r.output and "verify --from-parquet" in r.output


class TestQuerySpans:
    """`dal query-spans`: dead since January (it imported functions that never existed);
    now reads export-parquet files or the store."""

    def test_from_store_stats(self, runner, chosen_store):
        r = runner.invoke(main, ["query-spans", "--from-store", "--stats"])
        assert r.exit_code == 0, r.output
        assert "Total spans: 3" in r.output and "[raw]" in r.output
        assert "ERROR: 1" in r.output and "claude-x: 1" in r.output

    def test_from_store_list_and_json(self, runner, chosen_store):
        r = runner.invoke(
            main, ["query-spans", "--from-store", "--status-code", "ERROR", "--format", "json"]
        )
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["total_spans"] == 1 and out["spans"][0]["span_id"] == "c"
        r = runner.invoke(main, ["query-spans", "--from-store", "--name", "bash"])
        assert r.exit_code == 0 and "Found 1 spans" in r.output and "Bash" in r.output

    def test_from_store_empty_store_is_a_clean_error(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        runner.invoke(main, ["store", "use", f"file://{tmp_path}/empty"])
        r = runner.invoke(main, ["query-spans", "--from-store"])
        assert r.exit_code == 1 and "holds no spans_raw" in r.output

    def test_source_reads_export_parquet_files(self, runner, monkeypatch, tmp_path):
        import pyarrow as pa
        import pyarrow.parquet as pq

        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path))
        d = tmp_path / "parquet" / "spans" / "source=src" / "week=2026-W36"
        d.mkdir(parents=True)
        pq.write_table(
            pa.Table.from_pylist(
                [
                    {
                        "session_id": "s",
                        "span_id": "a",
                        "name": "litellm_request",
                        "status_code": "OK",
                        "start_time": "2026-09-01 10:00:00",
                        "llm_model_name": "m",
                    }
                ]
            ),
            d / "part-00000.parquet",
        )
        r = runner.invoke(main, ["query-spans", "--source", "src", "--stats"])
        assert r.exit_code == 0, r.output
        assert "Total spans: 1" in r.output and "source=src" in r.output
        r = runner.invoke(main, ["query-spans", "--source", "nope"])
        assert r.exit_code == 1 and "Available sources:" in r.output and "- src" in r.output

    def test_usage_errors(self, runner, chosen_store):
        assert runner.invoke(main, ["query-spans"]).exit_code == 2
        r = runner.invoke(main, ["query-spans", "--source", "x", "--layout", "typed"])
        assert r.exit_code == 2 and "--from-store" in r.output


class TestExportParquetAndTheStore:
    """`dal export-parquet` reads the store with --from-store and lands its output in it."""

    def test_from_store_exports_and_lands(self, runner, chosen_store, tmp_path):
        r = runner.invoke(main, ["export-parquet", "--source", "src", "--from-store"])
        assert r.exit_code == 0, r.output
        assert "Spans: 3" in r.output and "carry no source tag" in r.output
        assert "-> span store: export/src/spans 3 rows" in r.output
        assert "-> span store: export/src/sessions" in r.output
        assert sorted(chosen_store.list_datasets()) == [
            "export/src/sessions",
            "export/src/spans",
            "spans_raw",
        ]
        # and the export files are what query-spans --source reads
        r = runner.invoke(main, ["query-spans", "--source", "src", "--stats"])
        assert r.exit_code == 0 and "Total spans: 3" in r.output

    def test_from_raw_lands_too(self, runner, chosen_store, tmp_path):
        raw = tmp_path / "data" / "raw" / "rawsrc"
        raw.mkdir(parents=True)
        rows = [
            {
                "span_id": "r1",
                "trace_id": "t",
                "parent_id": None,
                "name": "litellm_request",
                "span_kind": "LLM",
                "start_time": "2026-09-01T10:00:00+00:00",
                "end_time": "2026-09-01T10:00:01+00:00",
                "status_code": "OK",
                "llm_model_name": "m",
                "raw_attributes": {},
                "backend": "phoenix",
            }
        ]
        (raw / "sync_20260901.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
        r = runner.invoke(main, ["export-parquet", "--source", "rawsrc", "--from-raw"])
        assert r.exit_code == 0, r.output
        assert "-> span store: export/rawsrc/spans 1 rows" in r.output
        assert "export/rawsrc/spans" in chosen_store.list_datasets()

    def test_without_a_chosen_store_nothing_is_landed(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("DAL_DATA_PATH", str(tmp_path / "data"))
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        raw = tmp_path / "data" / "raw" / "s"
        raw.mkdir(parents=True)
        (raw / "sync_1.jsonl").write_text(
            json.dumps(
                {
                    "span_id": "r1",
                    "trace_id": "t",
                    "name": "n",
                    "start_time": "2026-09-01T10:00:00+00:00",
                    "raw_attributes": {},
                }
            )
            + "\n"
        )
        r = runner.invoke(main, ["export-parquet", "--source", "s", "--from-raw"])
        assert r.exit_code == 0, r.output
        assert "span store" not in r.output

    def test_flag_conflicts_and_empty_store(self, runner, monkeypatch, tmp_path):
        monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
        runner.invoke(main, ["store", "use", f"file://{tmp_path}/empty"])
        both = runner.invoke(
            main, ["export-parquet", "--source", "s", "--from-store", "--from-raw"]
        )
        assert both.exit_code == 2
        flat = runner.invoke(
            main, ["export-parquet", "--source", "s", "--from-store", "--no-partitioned"]
        )
        assert flat.exit_code == 2
        empty = runner.invoke(main, ["export-parquet", "--source", "s", "--from-store"])
        assert empty.exit_code == 1 and "holds no spans_raw" in empty.output
