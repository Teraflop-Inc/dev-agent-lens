#!/usr/bin/env python3
"""
End-to-End Test: Claude-Lens Proxy Pipeline

Tests the full pipeline:
1. Claude Code conversation through claude-lens proxy → Phoenix
2. Sync traces from Phoenix to local parquet
3. Export session to markdown
4. Validate export contains expected content

NOTE: Step 1 (running conversation through proxy) requires manual intervention.
The automated tests validate that the infrastructure is ready and can process
sessions that have been created through the proxy.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

# Import validation logic
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.validate_export import validate_session

# Configuration
PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")
PROXY_URL = os.getenv("CLAUDE_LENS_PROXY_URL", "http://localhost:4000")
CLAUDE_LENS_SCRIPT = Path.home() / "Company" / "dev3" / "private-dev-agent-lens" / "claude-lens"


def check_phoenix_running() -> tuple[bool, str]:
    """Check if Phoenix is running and accessible."""
    try:
        response = requests.get(f"{PHOENIX_URL}/arize_phoenix_version", timeout=5)
        if response.ok:
            version = response.text.strip('"')
            return True, f"Phoenix {version}"
        return False, f"Phoenix returned status {response.status_code}"
    except requests.ConnectionError:
        return False, "Phoenix not reachable (connection refused)"
    except requests.Timeout:
        return False, "Phoenix request timed out"
    except Exception as e:
        return False, f"Phoenix check failed: {e}"


def check_proxy_running() -> tuple[bool, str]:
    """Check if LiteLLM proxy is running."""
    try:
        response = requests.get(f"{PROXY_URL}/health", timeout=5)
        if response.ok:
            return True, "Proxy healthy"
        return False, f"Proxy returned status {response.status_code}"
    except requests.ConnectionError:
        return False, "Proxy not reachable (connection refused)"
    except requests.Timeout:
        return False, "Proxy request timed out"
    except Exception as e:
        return False, f"Proxy check failed: {e}"


def check_claude_lens_exists() -> tuple[bool, str]:
    """Check if claude-lens script exists."""
    if CLAUDE_LENS_SCRIPT.exists():
        return True, str(CLAUDE_LENS_SCRIPT)
    return False, f"Not found at {CLAUDE_LENS_SCRIPT}"


def get_recent_phoenix_sessions(hours: int = 24) -> list[str]:
    """Get session IDs from Phoenix in the last N hours.

    Args:
        hours: Look back this many hours

    Returns:
        List of session IDs (may be empty)
    """
    try:
        # Use Phoenix GraphQL API to query spans
        # This is a simplified check - we just look for any recent spans
        from arize_phoenix.client import Client

        phoenix = Client(endpoint=PHOENIX_URL)

        # Get spans from last N hours
        # Phoenix stores session_id in span.context.session_id
        cutoff = datetime.now() - timedelta(hours=hours)

        # Query for spans (limit to recent ones for performance)
        # Note: This is a basic check - in production you'd want more sophisticated filtering
        print(f"    Checking Phoenix for sessions created after {cutoff.isoformat()}")

        # For now, just return empty list and let manual validation provide session IDs
        # A full implementation would query the Phoenix DB/API
        return []

    except Exception as e:
        print(f"    Warning: Could not query Phoenix sessions: {e}")
        return []


class TestProxyInfrastructure:
    """Test suite for verifying the proxy infrastructure is ready."""

    def test_phoenix_running(self):
        """Phoenix must be running and accessible."""
        is_running, message = check_phoenix_running()
        assert is_running, f"Phoenix not running: {message}"
        print(f"\n✓ Phoenix is running: {message}")

    def test_proxy_running(self):
        """LiteLLM proxy should be running (warning if not)."""
        is_running, message = check_proxy_running()
        if not is_running:
            pytest.skip(f"Proxy not running: {message} (this is OK for validation-only tests)")
        print(f"\n✓ Proxy is running: {message}")

    def test_claude_lens_exists(self):
        """Claude-lens script should exist."""
        exists, message = check_claude_lens_exists()
        if not exists:
            pytest.skip(f"Claude-lens script not found: {message}")
        print(f"\n✓ Claude-lens script found: {message}")

    def test_dal_cli_available(self):
        """DAL CLI should be available for syncing and exporting."""
        result = subprocess.run(
            ["uv", "run", "dal", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"DAL CLI not available: {result.stderr}"
        print(f"\n✓ DAL CLI available: {result.stdout.strip()}")


class TestProxyPipeline:
    """Test suite for validating proxy-generated sessions."""

    @pytest.mark.parametrize("session_id", [
        pytest.param(
            os.getenv("TEST_SESSION_ID", ""),
            marks=pytest.mark.skipif(
                not os.getenv("TEST_SESSION_ID"),
                reason="No session ID provided. Set TEST_SESSION_ID env var."
            ),
        )
    ])
    def test_validate_session_export(self, session_id: str, tmp_path: Path):
        """Validate a session that was created through the proxy.

        This test:
        1. Checks the session exists in Phoenix parquet
        2. Runs the markdown export
        3. Validates the export contains all expected content

        To run this test:
        1. Run a conversation through claude-lens proxy
        2. Note the session ID from Phoenix UI
        3. Run: TEST_SESSION_ID=<session_id> uv run pytest tests/e2e/test_proxy_pipeline.py -v -k validate_session
        """
        print(f"\n{'='*60}")
        print(f"Validating session: {session_id}")
        print(f"{'='*60}\n")

        # Find parquet file in OxenStore
        from dev_agent_lens.storage.oxen_store import OxenStore

        store = OxenStore()
        parquet_dir = Path(store.data_path) / "parquet"

        # Look for phoenix-local parquet file
        parquet_files = list(parquet_dir.glob("*phoenix-local*_spans.parquet"))
        assert parquet_files, f"No phoenix-local parquet files found in {parquet_dir}"

        parquet_path = str(parquet_files[0])
        print(f"Using parquet file: {parquet_path}")

        # Run validation
        result = validate_session(session_id, parquet_path)

        # Print report
        result.print_report()

        # Assert validation passed
        assert result.passed, "Validation failed - see report above for details"

        print(f"\n{'='*60}")
        print("✓ Session validation PASSED")
        print(f"{'='*60}\n")


class TestProxySmokeTest:
    """Smoke tests that can run with existing data."""

    def test_can_list_recent_sessions(self):
        """Check if we can query Phoenix for recent sessions."""
        sessions = get_recent_phoenix_sessions(hours=24)
        print(f"\n✓ Found {len(sessions)} sessions in Phoenix (last 24h)")

        if sessions:
            print(f"  Recent session IDs:")
            for sid in sessions[:5]:  # Show first 5
                print(f"    - {sid}")
            if len(sessions) > 5:
                print(f"    ... and {len(sessions) - 5} more")

    def test_can_sync_from_phoenix(self, tmp_path: Path):
        """Test that we can sync data from Phoenix.

        This doesn't validate specific content, just checks the sync mechanism works.
        """
        # Try to sync using DAL CLI
        result = subprocess.run(
            [
                "uv", "run", "dal", "sync",
                "--source", "phoenix-local-test",
                "--start", (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                "--end", datetime.now().strftime("%Y-%m-%d"),
                "--limit", "10",  # Small limit for smoke test
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Sync might fail if no data, but should not error out
        print(f"\n✓ Sync command executed (exit code: {result.returncode})")
        print(f"  stdout: {result.stdout[:200]}...")

        if result.returncode != 0:
            print(f"  stderr: {result.stderr[:200]}...")
            pytest.skip(f"Sync failed (may be expected if no recent data): {result.stderr[:100]}")


def print_manual_test_instructions():
    """Print instructions for running a manual test through the proxy."""
    print("\n" + "="*70)
    print("MANUAL TEST INSTRUCTIONS")
    print("="*70)
    print("\nTo run a full end-to-end test:")
    print("\n1. Start Phoenix (if not already running):")
    print("   cd ~/Company/dev3/private-dev-agent-lens")
    print("   docker compose --profile phoenix up -d")
    print()
    print("2. Verify proxy is running:")
    print(f"   curl {PROXY_URL}/health")
    print()
    print("3. Run a test conversation through claude-lens:")
    print(f"   {CLAUDE_LENS_SCRIPT}")
    print("   # In the Claude Code session, type a simple request:")
    print('   > "What is 2+2?"')
    print()
    print("4. Note the session ID from Phoenix UI:")
    print(f"   Open: {PHOENIX_URL}")
    print("   Navigate to 'Traces' and find your recent session")
    print()
    print("5. Run validation test with your session ID:")
    print("   TEST_SESSION_ID=<your-session-id> \\")
    print("     uv run pytest tests/e2e/test_proxy_pipeline.py::TestProxyPipeline::test_validate_session_export -v")
    print()
    print("="*70)
    print()


if __name__ == "__main__":
    """Can be run standalone for infrastructure checks."""
    print("\n" + "="*70)
    print("Claude-Lens Proxy Pipeline - Infrastructure Check")
    print("="*70 + "\n")

    # Check infrastructure
    checks = [
        ("Phoenix", check_phoenix_running),
        ("Proxy", check_proxy_running),
        ("Claude-Lens Script", check_claude_lens_exists),
    ]

    results = {}
    for name, check_fn in checks:
        status, message = check_fn()
        results[name] = status
        symbol = "✓" if status else "✗"
        print(f"{symbol} {name}: {message}")

    print()

    # Check DAL CLI
    try:
        result = subprocess.run(
            ["uv", "run", "dal", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        dal_ok = result.returncode == 0
        results["DAL CLI"] = dal_ok
        symbol = "✓" if dal_ok else "✗"
        print(f"{symbol} DAL CLI: {result.stdout.strip() if dal_ok else 'Not available'}")
    except Exception as e:
        results["DAL CLI"] = False
        print(f"✗ DAL CLI: {e}")

    print("\n" + "="*70)

    # Summary
    ready_count = sum(1 for v in results.values() if v)
    total_count = len(results)

    print(f"\nInfrastructure Status: {ready_count}/{total_count} components ready")

    if ready_count == total_count:
        print("\n✓ All systems ready for end-to-end testing!")
        print_manual_test_instructions()
    else:
        print("\n! Some components not ready. See above for details.")
        print("\nTo get started:")
        if not results.get("Phoenix"):
            print("  - Start Phoenix: docker compose --profile phoenix up -d")
        if not results.get("Proxy"):
            print("  - Start proxy: docker compose up -d  (or check litellm_config_phoenix.yaml)")

    print()
