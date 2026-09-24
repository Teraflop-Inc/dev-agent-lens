"""Tests for Parquet export functionality."""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from dev_agent_lens.export.parquet import (
    ParquetExporter,
    _extract_session_metadata,
    _flatten_span,
    _parse_datetime,
    _safe_int,
    export_to_parquet,
    iter_sessions_from_jsonl,
)


class TestParseDatetime:
    """Tests for _parse_datetime function."""

    def test_parses_iso_format_with_tz(self):
        result = _parse_datetime("2025-01-01T12:00:00.000000+00:00")
        assert result is not None
        assert result.year == 2025
        assert result.month == 1
        assert result.hour == 12

    def test_parses_iso_format_without_tz(self):
        result = _parse_datetime("2025-01-01T12:00:00.000000")
        assert result is not None
        assert result.year == 2025

    def test_parses_space_separated(self):
        result = _parse_datetime("2025-01-01 12:00:00.000000")
        assert result is not None
        assert result.year == 2025

    def test_handles_none(self):
        assert _parse_datetime(None) is None

    def test_handles_datetime_object(self):
        dt = datetime(2025, 1, 1, 12, 0, 0)
        result = _parse_datetime(dt)
        assert result == dt

    def test_handles_invalid_string(self):
        result = _parse_datetime("not a date")
        assert result is None


class TestSafeInt:
    """Tests for _safe_int function."""

    def test_converts_int(self):
        assert _safe_int(42) == 42

    def test_converts_float(self):
        assert _safe_int(42.7) == 42

    def test_converts_string(self):
        assert _safe_int("42") == 42

    def test_handles_none(self):
        assert _safe_int(None) is None

    def test_handles_invalid_string(self):
        assert _safe_int("not a number") is None


class TestFlattenSpan:
    """Tests for _flatten_span function."""

    def test_flattens_basic_span(self):
        span = {
            "span_id": "sp123",
            "trace_id": "tr456",
            "name": "test_span",
            "span_kind": "LLM",
            "status_code": "OK",
            "input_value": "hello",
            "output_value": "world",
            "llm_model_name": "claude-3",
            "llm_token_count_prompt": 100,
            "llm_token_count_completion": 50,
            "llm_token_count_total": 150,
            "raw_attributes": {"key": "value"},
        }
        result = _flatten_span(span, "session123", "phoenix-alex")

        assert result["session_id"] == "session123"
        assert result["source"] == "phoenix-alex"
        assert result["span_id"] == "sp123"
        assert result["trace_id"] == "tr456"
        assert result["name"] == "test_span"
        assert result["llm_model_name"] == "claude-3"
        assert result["llm_token_count_prompt"] == 100
        assert result["raw_attributes_json"] == '{"key": "value"}'

    def test_handles_missing_fields(self):
        span = {"span_id": "sp123"}
        result = _flatten_span(span, "session123", "source")

        assert result["span_id"] == "sp123"
        assert result["trace_id"] is None
        assert result["llm_model_name"] is None
        assert result["raw_attributes_json"] is None

    def test_persists_user_attribution(self):
        span = {
            "span_id": "sp123",
            "user_id": "abc123def",
            "account_id": "11111111-1111-1111-1111-111111111111",
        }
        result = _flatten_span(span, "session123", "phoenix-alex")

        assert result["user_id"] == "abc123def"
        assert result["account_id"] == "11111111-1111-1111-1111-111111111111"

    def test_user_attribution_defaults_to_none(self):
        span = {"span_id": "sp123"}
        result = _flatten_span(span, "session123", "source")

        assert result["user_id"] is None
        assert result["account_id"] is None

    def test_parses_timestamps(self):
        span = {
            "span_id": "sp123",
            "start_time": "2025-01-01T12:00:00.000000",
            "end_time": "2025-01-01T12:01:00.000000",
        }
        result = _flatten_span(span, "session123", "source")

        assert result["start_time"] is not None
        assert result["end_time"] is not None


class TestExtractSessionMetadata:
    """Tests for _extract_session_metadata function."""

    def test_extracts_basic_metadata(self):
        session = {
            "session_id": "s123",
            "spans": [
                {
                    "span_id": "sp1",
                    "start_time": "2025-01-01T12:00:00.000000",
                    "end_time": "2025-01-01T12:01:00.000000",
                    "llm_model_name": "claude-3",
                    "llm_token_count_prompt": 100,
                    "llm_token_count_completion": 50,
                    "llm_token_count_total": 150,
                    "status_code": "OK",
                },
                {
                    "span_id": "sp2",
                    "start_time": "2025-01-01T12:01:00.000000",
                    "end_time": "2025-01-01T12:02:00.000000",
                    "llm_model_name": "claude-3",
                    "llm_token_count_prompt": 200,
                    "llm_token_count_completion": 100,
                    "llm_token_count_total": 300,
                    "status_code": "OK",
                },
            ]
        }
        result = _extract_session_metadata(session, "phoenix-alex")

        assert result["session_id"] == "s123"
        assert result["source"] == "phoenix-alex"
        assert result["span_count"] == 2
        assert result["total_prompt_tokens"] == 300
        assert result["total_completion_tokens"] == 150
        assert result["total_tokens"] == 450
        assert result["models_used"] == "claude-3"
        assert result["has_errors"] is False

    def test_detects_errors(self):
        session = {
            "session_id": "s123",
            "spans": [
                {"span_id": "sp1", "status_code": "OK"},
                {"span_id": "sp2", "status_code": "ERROR"},
            ]
        }
        result = _extract_session_metadata(session, "source")
        assert result["has_errors"] is True

    def test_handles_multiple_models(self):
        session = {
            "session_id": "s123",
            "spans": [
                {"span_id": "sp1", "llm_model_name": "claude-3"},
                {"span_id": "sp2", "llm_model_name": "gpt-4"},
            ]
        }
        result = _extract_session_metadata(session, "source")
        assert "claude-3" in result["models_used"]
        assert "gpt-4" in result["models_used"]

    def test_handles_empty_spans(self):
        session = {"session_id": "s123", "spans": []}
        result = _extract_session_metadata(session, "source")
        assert result["span_count"] == 0
        assert result["first_span_time"] is None
        assert result["last_span_time"] is None


class TestIterSessionsFromJsonl:
    """Tests for iter_sessions_from_jsonl function."""

    def test_iterates_sessions(self):
        sessions = [
            {"session_id": "s1", "spans": []},
            {"session_id": "s2", "spans": []},
            {"session_id": "s3", "spans": []},
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for session in sessions:
                f.write(json.dumps(session) + "\n")
            path = f.name

        try:
            result = list(iter_sessions_from_jsonl(path))
            assert len(result) == 3
            assert result[0]["session_id"] == "s1"
            assert result[2]["session_id"] == "s3"
        finally:
            Path(path).unlink()

    def test_skips_empty_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"session_id": "s1"}\n')
            f.write('\n')
            f.write('{"session_id": "s2"}\n')
            f.write('   \n')
            f.write('{"session_id": "s3"}\n')
            path = f.name

        try:
            result = list(iter_sessions_from_jsonl(path))
            assert len(result) == 3
        finally:
            Path(path).unlink()


class TestParquetExporter:
    """Tests for ParquetExporter class."""

    def test_export_source(self):
        pytest.importorskip("pyarrow")

        sessions = [
            {
                "session_id": "s1",
                "spans": [
                    {
                        "span_id": "sp1",
                        "trace_id": "tr1",
                        "name": "test",
                        "start_time": "2025-01-01T12:00:00.000000",
                        "end_time": "2025-01-01T12:01:00.000000",
                        "status_code": "OK",
                        "llm_model_name": "claude-3",
                        "llm_token_count_total": 100,
                        "raw_attributes": {
                            "context.span_id": "sp1",  # duplicate
                            "unique": "value",
                        }
                    }
                ]
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            output_dir = Path(tmpdir) / "output"

            with open(input_path, "w") as f:
                for session in sessions:
                    f.write(json.dumps(session) + "\n")

            exporter = ParquetExporter(
                compression="snappy",
                dedupe=True,
                strip_nulls=True,
            )
            stats = exporter.export_source(
                source="test-source",
                input_path=input_path,
                output_dir=output_dir,
            )

            assert stats["sessions"] == 1
            assert stats["spans"] == 1
            assert Path(stats["sessions_path"]).exists()
            assert Path(stats["spans_path"]).exists()
            # Note: For very small files, Parquet metadata overhead may exceed savings
            # This is expected - savings are realized at scale
            assert "savings_bytes" in stats

    def test_export_with_progress_callback(self):
        pytest.importorskip("pyarrow")

        sessions = [{"session_id": f"s{i}", "spans": []} for i in range(5)]
        callback_calls = []

        def callback(n):
            callback_calls.append(n)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            output_dir = Path(tmpdir) / "output"

            with open(input_path, "w") as f:
                for session in sessions:
                    f.write(json.dumps(session) + "\n")

            exporter = ParquetExporter()
            exporter.export_source(
                source="test",
                input_path=input_path,
                output_dir=output_dir,
                progress_callback=callback,
            )

            assert len(callback_calls) == 5

    def test_export_without_dedupe(self):
        pytest.importorskip("pyarrow")

        sessions = [
            {
                "session_id": "s1",
                "spans": [
                    {
                        "span_id": "sp1",
                        "raw_attributes": {
                            "context.span_id": "sp1",
                            "empty": None,
                        }
                    }
                ]
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            output_dir = Path(tmpdir) / "output"

            with open(input_path, "w") as f:
                for session in sessions:
                    f.write(json.dumps(session) + "\n")

            exporter = ParquetExporter(dedupe=False, strip_nulls=False)
            stats = exporter.export_source(
                source="test",
                input_path=input_path,
                output_dir=output_dir,
            )

            # Should still create files
            assert Path(stats["sessions_path"]).exists()
            assert Path(stats["spans_path"]).exists()


class TestExportToParquet:
    """Tests for export_to_parquet convenience function."""

    def test_exports_successfully(self):
        pytest.importorskip("pyarrow")

        sessions = [
            {"session_id": "s1", "spans": [{"span_id": "sp1"}]},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            output_dir = Path(tmpdir) / "output"

            with open(input_path, "w") as f:
                for session in sessions:
                    f.write(json.dumps(session) + "\n")

            stats = export_to_parquet(
                source="test",
                input_path=input_path,
                output_dir=output_dir,
            )

            assert stats["sessions"] == 1
            assert stats["spans"] == 1
            assert Path(stats["sessions_path"]).exists()


class TestParquetExporterAppend:
    """Tests for ParquetExporter.append_to_existing method."""

    def test_append_to_existing(self):
        pytest.importorskip("pyarrow")
        import pyarrow.parquet as pq

        # Create initial export
        initial_sessions = [
            {"session_id": "s1", "spans": [{"span_id": "sp1"}]},
            {"session_id": "s2", "spans": [{"span_id": "sp2"}]},
        ]

        new_sessions = [
            {"session_id": "s3", "spans": [{"span_id": "sp3"}]},
            {"session_id": "s1", "spans": [{"span_id": "sp1"}]},  # duplicate
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            initial_path = Path(tmpdir) / "initial.jsonl"
            new_path = Path(tmpdir) / "new.jsonl"
            output_dir = Path(tmpdir) / "output"

            # Write initial
            with open(initial_path, "w") as f:
                for session in initial_sessions:
                    f.write(json.dumps(session) + "\n")

            # Export initial
            exporter = ParquetExporter()
            exporter.export_source("test", initial_path, output_dir)

            # Write new
            with open(new_path, "w") as f:
                for session in new_sessions:
                    f.write(json.dumps(session) + "\n")

            # Append
            stats = exporter.append_to_existing(
                source="test",
                input_path=new_path,
                existing_sessions_path=output_dir / "test_sessions.parquet",
                existing_spans_path=output_dir / "test_spans.parquet",
            )

            assert stats["sessions_added"] == 1  # s3 only
            assert stats["sessions_skipped"] == 1  # s1 duplicate
            assert stats["total_sessions"] == 3

            # Verify final count
            sessions_table = pq.read_table(output_dir / "test_sessions.parquet")
            assert len(sessions_table) == 3


SESSION = {
    "tA": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
    "tB": "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb",
    "tU": "uuuuuuuu-1111-4111-8111-uuuuuuuuuuuu",
}


class TestExportStoreToPartitioned:
    """`dal export-parquet --from-store`: the span store's native rows go through the
    same unifier and flattener as a raw sync file."""

    @staticmethod
    def _frame(ids, day, trace, *, with_identity=True):
        import json

        import pandas as pd

        n = len(ids)
        # Claude Code identity: the export groups by a UUID-shaped session id only
        euid = json.dumps({"account_uuid": "acct-1", "session_id": SESSION[trace]})
        first = {"llm": {"model_name": "claude-opus-5",
                         "token_count": {"prompt": 10, "completion": 2}},
                 "input": {"value": "hi"}}
        if with_identity:
            first["metadata"] = {"user_api_key_end_user_id": euid}
        return pd.DataFrame({
            "context.span_id": ids, "context.trace_id": [trace] * n,
            "parent_id": [None] + ids[:-1],
            "name": ["litellm_request"] + ["Claude_Code_Tool_Bash"] * (n - 1),
            "span_kind": ["LLM"] + ["TOOL"] * (n - 1),
            "start_time": pd.to_datetime([f"{day}T10:00:{i:02d}Z" for i in range(n)]),
            "end_time": pd.to_datetime([f"{day}T10:00:{i + 1:02d}Z" for i in range(n)]),
            "status_code": ["OK"] * n, "status_message": [""] * n,
            "attributes": [json.dumps(first)] + ["{}"] * (n - 1),
            "events": ["[]"] * n,
            "llm_token_count_prompt": [10] + [0] * (n - 1),
            "llm_token_count_completion": [2] + [0] * (n - 1),
        })

    @pytest.fixture
    def store(self, tmp_path):
        import duckdb

        from dev_agent_lens.storage.spanstore import open_store

        s = open_store(f"file://{tmp_path}/store")
        s.ensure()
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        s.append_frame(con, self._frame(["a1", "a2"], "2026-09-01", "tA"), "spans_raw",
                       source="srcA")
        s.append_frame(con, self._frame(["b1"], "2026-09-01", "tB"), "spans_raw", source="srcB")
        s.append_frame(con, self._frame(["u1", "u2"], "2026-09-02", "tU", with_identity=False),
                       "spans_raw")   # landed before sync stamped a source
        return s

    def test_exports_the_source_plus_untagged_rows_in_the_raw_file_shape(self, store, tmp_path):
        import pyarrow.parquet as pq

        out = tmp_path / "parquet"
        stats = ParquetExporter().export_store_to_partitioned("srcA", store, out)
        assert (stats["spans"], stats["sessions"], stats["untagged_rows"]) == (4, 2, 2)
        rows = pq.read_table(out / "spans" / "source=srcA").to_pylist()
        assert sorted(r["span_id"] for r in rows) == ["a1", "a2", "u1", "u2"]   # b1 is srcB
        a1 = next(r for r in rows if r["span_id"] == "a1")
        # the packed `attributes` were read the way a direct Postgres sync reads them
        assert a1["llm_model_name"] == "claude-opus-5" and a1["input_value"] == "hi"
        assert a1["llm_token_count_prompt"] == 10 and a1["session_id"] == SESSION["tA"]
        a2 = next(r for r in rows if r["span_id"] == "a2")
        assert a2["session_id"] == SESSION["tA"]      # propagated from its sibling
        assert a1["start_time"].isoformat().startswith("2026-09-01T10:00:00")
        sessions = pq.read_table(out / "sessions" / "source=srcA.parquet").to_pylist()
        assert {s["session_id"] for s in sessions} == {SESSION["tA"], "tU"}   # tU: no identity

    def test_second_run_dedups_against_the_first(self, store, tmp_path):
        out = tmp_path / "parquet"
        exp = ParquetExporter()
        exp.export_store_to_partitioned("srcA", store, out)
        again = exp.export_store_to_partitioned("srcA", store, out)
        assert (again["spans"], again["spans_skipped"]) == (0, 4)

    def test_progress_is_one_call_per_day(self, store, tmp_path):
        seen = []
        ParquetExporter().export_store_to_partitioned(
            "srcA", store, tmp_path / "p", progress_callback=lambda n, t: seen.append((n, t)))
        assert seen == [(1, 2), (2, 2)]

    def test_empty_store_is_a_file_not_found(self, tmp_path):
        from dev_agent_lens.storage.spanstore import open_store

        with pytest.raises(FileNotFoundError, match="holds no spans_raw"):
            ParquetExporter().export_store_to_partitioned(
                "x", open_store(f"file://{tmp_path}/e"), tmp_path)
