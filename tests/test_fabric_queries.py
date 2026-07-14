"""Unit tests for the fabric convenience layer (ENG2-1313).

The heavy downstream (parquet query, chain building, markdown rendering) is smoke-
verified against real data separately; here we mock it so the WRAPPER logic —
source fan-out, filters, the list→reconstruct→export pipeline, and atomic writes —
is exercised deterministically without a parquet corpus.
"""

from unittest.mock import patch

from dev_agent_lens.fabric import queries

# A single known session shaped like query_sessions() output.
_SESSION = {
    "session_id": "test-sess-1",
    "spans": [
        {
            "input_value": "look at meeting 11111111-1111-1111-1111-111111111111 for ENG2-999",
            "output_value": "on it",
            "name": "llm",
        }
    ],
    "span_count": 1,
    "start_time": "2026-06-01T10:00:00",
    "end_time": "2026-06-01T10:05:00",
}


class _FakeExport:
    main_content = "# Session: test-sess-1\n\nlook at meeting …\n"
    subagent_files: dict = {}
    tool_result_files: dict = {}


def test_list_reconstruct_export_pipeline(tmp_path):
    """The headline pipeline: list a session, reconstruct it, bulk-export it atomically."""
    with (
        patch.object(queries, "_query", return_value=[dict(_SESSION)]),
        patch(
            "dev_agent_lens.export.markdown_litellm.export_chain_to_unified_markdown",
            return_value=_FakeExport(),
        ),
        patch("dev_agent_lens.analysis.chains.get_chain_for_session", return_value=object()),
    ):
        # 1. list
        sessions = queries.list_sessions(source="x")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "test-sess-1"

        # 2. reconstruct
        export = queries.reconstruct_session("test-sess-1", source="x")
        assert export is not None
        assert export.main_content.startswith("# Session: test-sess-1")

        # 3. export (bulk, atomic)
        written = queries.export_conversations(source="x", output_dir=tmp_path)
        assert len(written) == 1
        out = written[0]
        assert out.exists()
        assert out.read_text(encoding="utf-8") == _FakeExport.main_content
        # atomic writer leaves no partial temp file behind
        assert not list(tmp_path.glob(".dal-*.tmp"))


def test_list_sessions_min_spans_filter():
    """min_spans drops sessions below the threshold."""
    with patch.object(queries, "_query", return_value=[dict(_SESSION)]):
        assert len(queries.list_sessions(min_spans=1)) == 1
        assert queries.list_sessions(min_spans=2) == []


def test_list_sessions_tools_filter():
    """tools keeps only sessions whose spans reference ALL named tools."""
    s = dict(_SESSION)
    s["spans"] = [{"input_value": "used mcp__claude_ai_Linear here", "output_value": ""}]
    with patch.object(queries, "_query", return_value=[s]):
        assert len(queries.list_sessions(tools=["mcp__claude_ai_Linear"])) == 1
        assert queries.list_sessions(tools=["mcp__claude_ai_Notion"]) == []


def test_reconstruct_missing_session_returns_none():
    with patch.object(queries, "_query", return_value=[]):
        assert queries.reconstruct_session("nope") is None


def test_get_session_context_extracts_entities():
    """session-context pulls meeting UUIDs + ticket ids from conversation content."""
    metrics = type("M", (), {"token_count_total": 42, "duration_minutes": 5.0})()
    with (
        patch.object(queries, "_query", return_value=[dict(_SESSION)]),
        patch("dev_agent_lens.analysis.sessions.session_metrics", return_value=metrics),
    ):
        ctx = queries.get_session_context("test-sess-1")
        assert ctx.token_count == 42
        assert ctx.duration_minutes == 5.0
        assert "ENG2-999" in ctx.ticket_ids
        assert "11111111-1111-1111-1111-111111111111" in ctx.meeting_ids
        assert ctx.to_dict()["session_id"] == "test-sess-1"
