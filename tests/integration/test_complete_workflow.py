"""
Integration Tests for Complete Workflow

These tests verify that all components work together correctly:
1. Schema normalization (Phoenix and Arize)
2. Session ID extraction
3. Storage (OxenStore)
4. Session unification
5. State tracking
6. CLI commands

These tests use realistic data structures to catch integration issues.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from dev_agent_lens.core.schema import normalize_arize, normalize_phoenix
from dev_agent_lens.core.session import extract_session_id_from_span
from dev_agent_lens.core.state import SyncState
from dev_agent_lens.core.unify import list_sessions, unify_sessions
from dev_agent_lens.storage.oxen_store import OxenStore


class TestCompleteWorkflow:
    """End-to-end workflow tests."""

    def test_phoenix_to_storage_flow(self, tmp_path):
        """
        Complete workflow: Phoenix data -> normalize -> store -> unify -> read.

        This tests the full pipeline as it would be used in production.
        """
        # Step 1: Simulate Phoenix span data (raw format from Phoenix API)
        phoenix_spans = pd.DataFrame([
            {
                "context.span_id": "span-001",
                "context.trace_id": "trace-001",
                "parent_id": None,
                "name": "AgentExecutor",
                "attributes.openinference.span.kind": "CHAIN",
                "start_time": 1704110400000,  # Unix ms
                "end_time": 1704110460000,
                "status_code": "OK",
                "attributes.input.value": "User question here",
                "attributes.output.value": "Agent response here",
                "metadata": {"user_id": "test_session_abc123"},
            },
            {
                "context.span_id": "span-002",
                "context.trace_id": "trace-001",
                "parent_id": "span-001",
                "name": "ChatOpenAI",
                "attributes.openinference.span.kind": "LLM",
                "start_time": 1704110405000,
                "end_time": 1704110455000,
                "status_code": "OK",
                "attributes.llm.model_name": "gpt-4",
                "attributes.llm.token_count.prompt": 150,
                "attributes.llm.token_count.completion": 200,
                "attributes.llm.token_count.total": 350,
                "metadata": {"user_id": "test_session_abc123"},
            },
        ])

        # Step 2: Normalize the data
        normalized = normalize_phoenix(phoenix_spans)

        # Verify normalization
        assert len(normalized) == 2
        assert "span_id" in normalized.columns
        assert "backend" in normalized.columns
        assert normalized.iloc[0]["backend"] == "phoenix"

        # Step 3: Extract session IDs
        for _, row in normalized.iterrows():
            span_dict = row.to_dict()
            session_id = extract_session_id_from_span(span_dict)
            assert session_id == "abc123", f"Expected session_id abc123, got {session_id}"

        # Step 4: Store in OxenStore
        store = OxenStore(data_path=tmp_path)
        raw_file = store.append_spans(normalized, backend="phoenix-local")

        assert raw_file.exists()
        assert raw_file.stat().st_size > 0

        # Step 5: Read back and verify
        read_back = store.read_raw_file(raw_file)
        assert len(read_back) == 2

        # Step 6: Unify sessions
        unified, report = unify_sessions(
            normalized,
            output_file=store.sessions_dir / "sessions_test.jsonl",
            state_path=tmp_path / "state",
        )

        assert len(unified) == 2
        assert len(report.new_sessions) == 1
        assert "abc123" in report.new_sessions

        # Step 7: List sessions
        sessions = list_sessions(unified)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "abc123"
        assert sessions[0]["span_count"] == 2

    def test_arize_to_storage_flow(self, tmp_path):
        """
        Complete workflow: Arize data -> normalize -> store -> unify.
        """
        # Simulate Arize span data
        arize_spans = pd.DataFrame([
            {
                "span_id": "arize-span-001",
                "trace_id": "arize-trace-001",
                "parent_span_id": None,
                "span_name": "AgentExecutor",
                "span_kind": "CHAIN",
                "start_time": datetime(2025, 1, 1, 12, 0, 0),
                "end_time": datetime(2025, 1, 1, 12, 1, 0),
                "status": "OK",
                "input": "User input text",
                "output": "Agent output text",
                "attributes": json.dumps({
                    "user_api_key_end_user_id": "session_def456"
                }),
            },
        ])

        # Normalize
        normalized = normalize_arize(arize_spans)

        assert len(normalized) == 1
        assert normalized.iloc[0]["backend"] == "arize"

        # Store
        store = OxenStore(data_path=tmp_path)
        raw_file = store.append_spans(normalized, backend="arize-cloud")

        assert raw_file.exists()

        # Unify
        unified, report = unify_sessions(
            normalized,
            state_path=tmp_path / "state",
        )

        assert len(report.new_sessions) == 1

    def test_multiple_sync_simulation(self, tmp_path):
        """
        Simulate multiple sync operations with session continuation.
        """
        store = OxenStore(data_path=tmp_path)
        state = SyncState(data_path=tmp_path / "state")

        # First sync - 2 spans from one session
        first_sync = pd.DataFrame([
            {
                "span_id": "span-001",
                "trace_id": "trace-001",
                "name": "First span",
                "start_time": "2025-01-01T12:00:00",
                "metadata": {"user_id": "test_session_user1"},
            },
            {
                "span_id": "span-002",
                "trace_id": "trace-001",
                "name": "Second span",
                "start_time": "2025-01-01T12:01:00",
                "metadata": {"user_id": "test_session_user1"},
            },
        ])

        # Store first sync
        store.append_spans(first_sync, backend="test")
        state.set_last_sync("test", datetime.now())

        # Unify first sync
        sessions_file = store.sessions_dir / "sessions_current.jsonl"
        unified1, report1 = unify_sessions(
            first_sync,
            output_file=sessions_file,
            state_path=tmp_path / "state",
        )

        assert len(report1.new_sessions) == 1
        assert report1.total_spans_after == 2

        # Second sync - new span from same session
        second_sync = pd.DataFrame([
            {
                "span_id": "span-003",
                "trace_id": "trace-002",
                "name": "Third span (continuation)",
                "start_time": "2025-01-01T12:05:00",
                "metadata": {"user_id": "test_session_user1"},
            },
        ])

        store.append_spans(second_sync, backend="test")
        state.set_last_sync("test", datetime.now())

        # Unify with existing
        unified2, report2 = unify_sessions(
            second_sync,
            existing_file=sessions_file,
            output_file=sessions_file,
            state_path=tmp_path / "state",
        )

        # Should detect session continuation
        assert "user1" in report2.continued_sessions
        assert len(report2.new_sessions) == 0
        assert report2.total_spans_after == 3

        # Third sync - new session
        third_sync = pd.DataFrame([
            {
                "span_id": "span-004",
                "trace_id": "trace-003",
                "name": "New session span",
                "start_time": "2025-01-01T12:10:00",
                "metadata": {"user_id": "test_session_user2"},
            },
        ])

        store.append_spans(third_sync, backend="test")

        unified3, report3 = unify_sessions(
            third_sync,
            existing_file=sessions_file,
            output_file=sessions_file,
            state_path=tmp_path / "state",
        )

        # Should have both new and continued
        assert "user2" in report3.new_sessions
        assert report3.total_spans_after == 4

        # Verify final state
        sessions = list_sessions(unified3)
        assert len(sessions) == 2

    def test_deduplication_across_syncs(self, tmp_path):
        """
        Verify that duplicate spans are properly deduplicated.
        """
        store = OxenStore(data_path=tmp_path)

        # First sync with span
        first_sync = pd.DataFrame([
            {
                "span_id": "duplicate-span",
                "name": "Original version",
                "version": "v1",
                "metadata": {"user_id": "test_session_abc"},
            },
        ])

        sessions_file = store.sessions_dir / "sessions_test.jsonl"
        unified1, _ = unify_sessions(
            first_sync,
            output_file=sessions_file,
            state_path=tmp_path / "state",
        )

        # Second sync with same span_id but updated data
        second_sync = pd.DataFrame([
            {
                "span_id": "duplicate-span",
                "name": "Updated version",
                "version": "v2",
                "metadata": {"user_id": "test_session_abc"},
            },
        ])

        unified2, report = unify_sessions(
            second_sync,
            existing_file=sessions_file,
            output_file=sessions_file,
            state_path=tmp_path / "state",
        )

        # Should have deduplicated
        assert report.duplicates_removed == 1
        assert len(unified2) == 1
        assert unified2.iloc[0]["version"] == "v2"  # Should keep newer


class TestStateTrackerIntegration:
    """Tests for state tracker with storage integration."""

    def test_state_persists_across_instances(self, tmp_path):
        """State should persist when creating new SyncState instance."""
        # First instance - set state
        state1 = SyncState(data_path=tmp_path)
        sync_time = datetime(2025, 1, 1, 12, 0, 0)
        state1.set_last_sync("phoenix-local", sync_time)

        # Second instance - read state
        state2 = SyncState(data_path=tmp_path)
        retrieved = state2.get_last_sync("phoenix-local")

        assert retrieved == sync_time

    def test_state_with_storage(self, tmp_path):
        """State tracker works correctly with OxenStore."""
        state = SyncState(data_path=tmp_path)
        store = OxenStore(data_path=tmp_path)

        # Before sync
        assert state.get_last_sync("phoenix-local") is None

        # Simulate sync
        spans = pd.DataFrame([{"span_id": "test"}])
        store.append_spans(spans, backend="phoenix-local")
        state.set_last_sync("phoenix-local", datetime.now())

        # After sync
        assert state.get_last_sync("phoenix-local") is not None
        assert len(store.get_raw_files()) == 1


class TestOxenStoreIntegration:
    """Tests for OxenStore with real file operations."""

    def test_merge_creates_unified_view(self, tmp_path):
        """Merge should create a single view of all sessions."""
        store = OxenStore(data_path=tmp_path)

        # Multiple syncs with different sessions
        store.append_spans(
            [{"span_id": "s1", "session": "a"}],
            backend="test"
        )
        store.append_spans(
            [{"span_id": "s2", "session": "b"}],
            backend="test"
        )
        store.append_spans(
            [{"span_id": "s3", "session": "a"}],
            backend="test"
        )

        # Merge
        sessions_file = store.merge_sessions()

        assert sessions_file.exists()

        # Read merged file
        merged = store.get_current_sessions()

        # Should have all 3 spans
        assert len(merged) == 3

    def test_large_dataset_handling(self, tmp_path):
        """Store should handle large datasets efficiently."""
        store = OxenStore(data_path=tmp_path)

        # Create 5000 spans across 50 sessions
        spans = []
        for session_idx in range(50):
            for span_idx in range(100):
                spans.append({
                    "span_id": f"span_{session_idx}_{span_idx}",
                    "trace_id": f"trace_{session_idx}",
                    "name": f"Span {span_idx}",
                    "metadata": {"user_id": f"test_session_session{session_idx}"},
                })

        # Store
        raw_file = store.append_spans(spans, backend="test")

        assert raw_file.exists()
        assert raw_file.stat().st_size > 0

        # Read back
        read_back = store.read_raw_file(raw_file)
        assert len(read_back) == 5000

        # Unify
        unified, report = unify_sessions(
            read_back,
            state_path=tmp_path / "state",
        )

        assert report.total_spans_after == 5000
        sessions = list_sessions(unified)
        assert len(sessions) == 50


class TestCLIIntegration:
    """Tests for CLI with real components."""

    def test_dal_cli_imports(self):
        """CLI module should import without errors."""
        from dev_agent_lens.cli.main import main, sync, config, status

        assert main is not None
        assert sync is not None
        assert config is not None
        assert status is not None

    def test_dal_package_imports(self):
        """Package should export all public APIs."""
        from dev_agent_lens.clients import PhoenixClient, ArizeClient
        from dev_agent_lens.core import (
            normalize_phoenix,
            normalize_arize,
            extract_session_id,
            SyncState,
            unify_sessions,
        )
        from dev_agent_lens.storage import OxenStore

        # All imports should work
        assert PhoenixClient is not None
        assert ArizeClient is not None
        assert normalize_phoenix is not None
        assert normalize_arize is not None
        assert extract_session_id is not None
        assert SyncState is not None
        assert unify_sessions is not None
        assert OxenStore is not None


class TestRealisticDataScenarios:
    """Tests with realistic Claude Code trace patterns."""

    def test_multi_turn_conversation(self, tmp_path):
        """
        Simulate a multi-turn conversation with tool calls.
        """
        # Realistic multi-turn conversation data
        conversation_spans = pd.DataFrame([
            # Turn 1: User message
            {
                "context.span_id": "turn1-chain",
                "context.trace_id": "trace-001",
                "name": "AgentExecutor",
                "attributes.openinference.span.kind": "CHAIN",
                "start_time": 1704110400000,
                "end_time": 1704110410000,
                "attributes.input.value": "What files are in the src directory?",
                "metadata": {"user_id": "test_session_conversation1"},
            },
            # Turn 1: LLM call
            {
                "context.span_id": "turn1-llm",
                "context.trace_id": "trace-001",
                "parent_id": "turn1-chain",
                "name": "ChatOpenAI",
                "attributes.openinference.span.kind": "LLM",
                "start_time": 1704110401000,
                "end_time": 1704110405000,
                "attributes.llm.model_name": "claude-3-opus",
                "attributes.llm.token_count.total": 500,
                "metadata": {"user_id": "test_session_conversation1"},
            },
            # Turn 1: Tool call
            {
                "context.span_id": "turn1-tool",
                "context.trace_id": "trace-001",
                "parent_id": "turn1-chain",
                "name": "Bash",
                "attributes.openinference.span.kind": "TOOL",
                "start_time": 1704110406000,
                "end_time": 1704110408000,
                "attributes.input.value": "ls src/",
                "attributes.output.value": "main.py\nutils.py\n",
                "metadata": {"user_id": "test_session_conversation1"},
            },
            # Turn 2: Follow-up
            {
                "context.span_id": "turn2-chain",
                "context.trace_id": "trace-002",
                "name": "AgentExecutor",
                "attributes.openinference.span.kind": "CHAIN",
                "start_time": 1704110420000,
                "end_time": 1704110430000,
                "attributes.input.value": "Show me the contents of main.py",
                "metadata": {"user_id": "test_session_conversation1"},
            },
        ])

        # Normalize
        normalized = normalize_phoenix(conversation_spans)

        # Store and unify
        store = OxenStore(data_path=tmp_path)
        store.append_spans(normalized, backend="phoenix-local")

        unified, report = unify_sessions(
            normalized,
            state_path=tmp_path / "state",
        )

        # Verify
        assert len(report.new_sessions) == 1
        sessions = list_sessions(unified)
        assert sessions[0]["span_count"] == 4

    def test_error_recovery_scenario(self, tmp_path):
        """
        Test scenario where some spans have errors.
        """
        spans_with_errors = pd.DataFrame([
            {
                "context.span_id": "success-span",
                "context.trace_id": "trace-001",
                "name": "AgentExecutor",
                "status_code": "OK",
                "metadata": {"user_id": "session_test"},
            },
            {
                "context.span_id": "error-span",
                "context.trace_id": "trace-001",
                "name": "Bash",
                "status_code": "ERROR",
                "attributes.output.value": "Command not found",
                "metadata": {"user_id": "session_test"},
            },
        ])

        normalized = normalize_phoenix(spans_with_errors)

        store = OxenStore(data_path=tmp_path)
        raw_file = store.append_spans(normalized, backend="phoenix-local")

        # Should store both success and error spans
        read_back = store.read_raw_file(raw_file)
        assert len(read_back) == 2

        # Both should have status_code
        status_codes = read_back["status_code"].tolist()
        assert "OK" in status_codes
        assert "ERROR" in status_codes
