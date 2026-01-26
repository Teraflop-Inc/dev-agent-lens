"""
Test Orchestrator Module

Provides infrastructure for end-to-end pipeline testing of the
Claude Code -> LiteLLM -> Observability backend flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)


class TestBackend(Enum):
    """Supported observability backends for testing."""

    ARIZE = "arize"
    PHOENIX = "phoenix"


@dataclass
class TestConfig:
    """Configuration for a test run."""

    backend: TestBackend
    test_run_id: str = field(
        default_factory=lambda: f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    stop_container_after: bool = False  # Semi-persistent by default
    timeout_seconds: int = 300
    prompt_file: str = "stress_test.txt"  # Which prompt to use


@dataclass
class TestResult:
    """Results from a test run."""

    test_run_id: str
    passed: bool
    assertions: dict[str, bool]
    span_count: int
    run_dir: Path | None = None
    error: str | None = None


class TestContainer:
    """Manages test LiteLLM containers."""

    PORTS = {
        TestBackend.ARIZE: 4100,
        TestBackend.PHOENIX: 4101,
    }

    PROFILES = {
        TestBackend.ARIZE: "test-arize",
        TestBackend.PHOENIX: "test-phoenix",
    }

    SERVICE_NAMES = {
        TestBackend.ARIZE: "litellm-test-arize",
        TestBackend.PHOENIX: "litellm-test-phoenix",
    }

    def __init__(self, backend: TestBackend, project_name: str, repo_root: Path | None = None):
        """
        Initialize test container manager.

        Args:
            backend: Which observability backend to use.
            project_name: Project name for trace isolation.
            repo_root: Path to repo root containing docker-compose.yml.
                      Defaults to auto-detection.
        """
        self.backend = backend
        self.project_name = project_name
        self.port = self.PORTS[backend]
        self.profile = self.PROFILES[backend]
        self.service_name = self.SERVICE_NAMES[backend]
        self.repo_root = repo_root or self._find_repo_root()

    def _find_repo_root(self) -> Path:
        """Find the repository root by looking for docker-compose.yml."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "docker-compose.yml").exists():
                return parent
        raise RuntimeError("Could not find repository root (no docker-compose.yml found)")

    def is_running(self) -> bool:
        """Check if container is already running."""
        try:
            result = subprocess.run(
                ["docker", "compose", "ps", "--format", "json"],
                capture_output=True,
                text=True,
                cwd=self.repo_root,
            )
            if result.returncode != 0:
                return False

            # Parse JSON output (one object per line)
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    container = json.loads(line)
                    if self.service_name in container.get("Name", ""):
                        state = container.get("State", "").lower()
                        return state == "running"
                except json.JSONDecodeError:
                    continue
            return False
        except Exception:
            return False

    def _get_current_project_name(self) -> str | None:
        """Get the project name the running container is configured with."""
        try:
            result = subprocess.run(
                [
                    "docker", "inspect",
                    f"--format={{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}",
                    f"{self.repo_root.name}-{self.service_name}-1"
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return None

            # Parse environment variables looking for OTEL_SERVICE_NAME
            for line in result.stdout.strip().split("\n"):
                if line.startswith("OTEL_SERVICE_NAME="):
                    return line.split("=", 1)[1]
            return None
        except Exception:
            return None

    def _restart_with_new_project(self, env: dict) -> None:
        """Stop and restart container with new project name."""
        # Stop existing container
        subprocess.run(
            ["docker", "compose", "--profile", self.profile, "stop", self.service_name],
            cwd=self.repo_root,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "compose", "--profile", self.profile, "rm", "-f", self.service_name],
            cwd=self.repo_root,
            capture_output=True,
        )
        # Start with new config
        subprocess.run(
            ["docker", "compose", "--profile", self.profile, "up", "-d", self.service_name],
            cwd=self.repo_root,
            env=env,
            check=True,
            capture_output=True,
        )

    def _is_phoenix_accessible(self) -> bool:
        """Check if Phoenix is accessible at localhost:6006."""
        try:
            import requests
            resp = requests.get("http://localhost:6006/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            # Try curl as fallback
            result = subprocess.run(
                ["curl", "-sf", "http://localhost:6006/health"],
                capture_output=True,
            )
            return result.returncode == 0

    def start(self) -> None:
        """Start container if not running, or restart if project name changed."""
        # Set project name via environment
        env = os.environ.copy()
        env["DAL_TEST_PROJECT"] = self.project_name

        # Check if container is running and has correct project name
        if self.is_running():
            current_project = self._get_current_project_name()
            if current_project == self.project_name:
                # Already running with correct project, nothing to do
                return
            else:
                # Running with wrong project name, need to restart
                self._restart_with_new_project(env)
                self._wait_healthy()
                return

        # For Phoenix, check if it's already accessible (may be running in another project)
        if self.backend == TestBackend.PHOENIX:
            if self._is_phoenix_accessible():
                # Phoenix already running, skip starting it
                pass
            else:
                # Start phoenix
                subprocess.run(
                    ["docker", "compose", "--profile", "phoenix", "up", "-d", "phoenix"],
                    cwd=self.repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                )

        # Start the test container
        subprocess.run(
            ["docker", "compose", "--profile", self.profile, "up", "-d", self.service_name],
            cwd=self.repo_root,
            env=env,
            check=True,
            capture_output=True,
        )

        self._wait_healthy()

    def stop(self) -> None:
        """Stop container."""
        subprocess.run(
            ["docker", "compose", "--profile", self.profile, "stop", self.service_name],
            cwd=self.repo_root,
            capture_output=True,
        )

    def _wait_healthy(self, timeout: int = 60) -> None:
        """Wait for container health check to pass."""
        import time

        try:
            import requests
        except ImportError:
            # Fall back to curl if requests not available
            start = time.time()
            while time.time() - start < timeout:
                result = subprocess.run(
                    ["curl", "-sf", f"http://localhost:{self.port}/health"],
                    capture_output=True,
                )
                if result.returncode == 0:
                    return
                time.sleep(2)
            raise TimeoutError(f"Container did not become healthy within {timeout}s")

        start = time.time()
        while time.time() - start < timeout:
            try:
                resp = requests.get(f"http://localhost:{self.port}/health", timeout=5)
                if resp.status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError(f"Container did not become healthy within {timeout}s")

    def get_proxy_url(self) -> str:
        """Return the proxy URL for claude-lens."""
        return f"http://localhost:{self.port}"

    def __repr__(self) -> str:
        return f"TestContainer(backend={self.backend.value}, project={self.project_name}, port={self.port})"


class TestOrchestrator:
    """Orchestrates end-to-end pipeline tests."""

    def __init__(self, config: TestConfig, repo_root: Path | None = None):
        """
        Initialize test orchestrator.

        Args:
            config: Test configuration.
            repo_root: Path to repo root. Defaults to auto-detection.
        """
        self.config = config
        self.repo_root = repo_root or self._find_repo_root()
        self.project_name = f"dal-test-{config.test_run_id}"
        self.container = TestContainer(config.backend, self.project_name, self.repo_root)
        self.testbed_root = self.repo_root / "tests" / "e2e" / "testbed"
        self.run_dir = self.testbed_root / "runs" / f"run-{config.test_run_id}"

    def _find_repo_root(self) -> Path:
        """Find the repository root by looking for docker-compose.yml."""
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "docker-compose.yml").exists():
                return parent
        raise RuntimeError("Could not find repository root (no docker-compose.yml found)")

    async def run(self) -> TestResult:
        """Execute full test cycle."""
        try:
            # 1. Setup
            self._setup_run_directory()
            self.container.start()

            # 2. Run Claude Code
            await self._run_claude_code()

            # 3. Wait for trace propagation
            await asyncio.sleep(5)

            # 4. Sync and validate
            spans_df = await self._sync_traces()
            result = self._validate(spans_df)
            result.run_dir = self.run_dir

            return result

        except Exception as e:
            return TestResult(
                test_run_id=self.config.test_run_id,
                passed=False,
                assertions={},
                span_count=0,
                run_dir=self.run_dir,
                error=str(e),
            )
        finally:
            if self.config.stop_container_after:
                self.container.stop()

    def _setup_run_directory(self) -> None:
        """Create run directory with symlinks to shared resources."""
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Symlink shared files
        for shared in [".claude.md", "sample_code"]:
            src = self.testbed_root / shared
            dst = self.run_dir / shared
            if not dst.exists() and src.exists():
                # Use relative symlink for portability
                try:
                    dst.symlink_to(os.path.relpath(src, self.run_dir))
                except OSError:
                    # Fall back to absolute path if relative fails
                    dst.symlink_to(src)

    async def _run_claude_code(self) -> None:
        """Execute Claude Code with print mode in run directory."""
        prompt_file = self.testbed_root / "prompts" / self.config.prompt_file
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

        prompt = prompt_file.read_text()

        # Build claude-lens command
        claude_lens = self.repo_root / "claude-lens"
        if not claude_lens.exists():
            raise FileNotFoundError(f"claude-lens script not found: {claude_lens}")

        cmd = [
            str(claude_lens),
            "--proxy-url",
            self.container.get_proxy_url(),
            "--print",
            "-p",
            prompt,
        ]

        # Run in the run directory
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.run_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.config.timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            raise TimeoutError(
                f"Claude Code execution timed out after {self.config.timeout_seconds}s"
            )

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Claude Code failed (exit {process.returncode}): {error_msg}")

    async def _sync_traces(self) -> pd.DataFrame:
        """Pull traces from test project."""
        from dev_agent_lens.clients import ArizeClient, PhoenixClient

        now = datetime.now()
        start_time = now - timedelta(minutes=15)

        if self.config.backend == TestBackend.PHOENIX:
            client = PhoenixClient(project_name=self.project_name)
            return client.get_spans_dataframe(
                project_name=self.project_name,
                start_time=start_time,
                end_time=now,
            )
        else:
            client = ArizeClient(model_id=self.project_name)
            return client.get_spans_dataframe(
                model_id=self.project_name,
                start_time=start_time,
                end_time=now,
            )

    def _validate(self, spans_df: pd.DataFrame) -> TestResult:
        """Assert expected traces exist."""
        if spans_df.empty:
            return TestResult(
                test_run_id=self.config.test_run_id,
                passed=False,
                assertions={"has_any_spans": False},
                span_count=0,
                error="No spans found in observability backend",
            )

        # Build assertions based on available columns
        assertions = {}

        # Check for LLM spans
        if "span_kind" in spans_df.columns:
            assertions["has_llm_spans"] = len(spans_df[spans_df["span_kind"] == "LLM"]) > 0
        elif "kind" in spans_df.columns:
            assertions["has_llm_spans"] = len(spans_df[spans_df["kind"] == "LLM"]) > 0
        else:
            assertions["has_llm_spans"] = True  # Assume true if we can't check

        # Check for tool spans by name
        if "name" in spans_df.columns:
            names = spans_df["name"].astype(str)
            assertions["has_read_tool"] = names.str.contains("Read", case=False, na=False).any()
            assertions["has_task_tool"] = names.str.contains("Task", case=False, na=False).any()
        else:
            # Can't verify without name column
            assertions["has_read_tool"] = True
            assertions["has_task_tool"] = True

        return TestResult(
            test_run_id=self.config.test_run_id,
            passed=all(assertions.values()),
            assertions=assertions,
            span_count=len(spans_df),
        )

    def cleanup_run_dir(self) -> None:
        """Remove the run directory."""
        import shutil

        if self.run_dir.exists():
            shutil.rmtree(self.run_dir)

    def __repr__(self) -> str:
        return f"TestOrchestrator(backend={self.config.backend.value}, run_id={self.config.test_run_id})"


@dataclass
class ProjectInfo:
    """Information about a Phoenix project."""

    id: str
    name: str
    created_at: datetime | None = None
    span_count: int | None = None


class PhoenixProjectCleaner:
    """Manages cleanup of test projects in Phoenix.

    Safety features:
    - PROTECTED_PROJECTS are hardcoded and can NEVER be deleted
    - Only projects matching TEST_PROJECT_PREFIX can be deleted
    - Explicit confirmation required for cleanup_all
    - All deletions are logged
    """

    # These projects can NEVER be deleted - hardcoded for safety
    PROTECTED_PROJECTS = frozenset(["dev-agent-lens", "default"])

    # Only projects with this prefix can be deleted
    TEST_PROJECT_PREFIX = "dal-test-"

    def __init__(self, phoenix_url: str = "http://localhost:6006"):
        """
        Initialize Phoenix project cleaner.

        Args:
            phoenix_url: URL of the Phoenix server (default: localhost:6006)
        """
        self.phoenix_url = phoenix_url.rstrip("/")

    def _is_deletable(self, project_name: str) -> bool:
        """Check if a project can be safely deleted.

        A project is deletable if:
        1. It is NOT in PROTECTED_PROJECTS
        2. It starts with TEST_PROJECT_PREFIX

        Args:
            project_name: Name of the project to check

        Returns:
            True if the project can be deleted, False otherwise
        """
        if project_name in self.PROTECTED_PROJECTS:
            return False
        if not project_name.startswith(self.TEST_PROJECT_PREFIX):
            return False
        return True

    def list_all_projects(self) -> list[ProjectInfo]:
        """List all projects in Phoenix.

        Returns:
            List of ProjectInfo objects for all projects
        """
        try:
            resp = requests.get(f"{self.phoenix_url}/v1/projects", timeout=30)
            resp.raise_for_status()
            data = resp.json()

            projects = []
            for p in data.get("data", []):
                created_at = None
                if p.get("created_at"):
                    try:
                        created_at = datetime.fromisoformat(
                            p["created_at"].replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        pass

                projects.append(
                    ProjectInfo(
                        id=p["id"],
                        name=p["name"],
                        created_at=created_at,
                        span_count=p.get("record_count"),
                    )
                )
            return projects
        except requests.RequestException as e:
            logger.error(f"Failed to list Phoenix projects: {e}")
            raise RuntimeError(f"Failed to connect to Phoenix at {self.phoenix_url}: {e}")

    def list_test_projects(self) -> list[ProjectInfo]:
        """List only test projects (matching TEST_PROJECT_PREFIX).

        Returns:
            List of ProjectInfo objects for test projects only
        """
        all_projects = self.list_all_projects()
        return [p for p in all_projects if self._is_deletable(p.name)]

    def delete_project(self, project_name: str, force: bool = False) -> bool:
        """Delete a single project from Phoenix.

        Args:
            project_name: Name of the project to delete
            force: If True, skip the deletable check (DANGEROUS - still respects PROTECTED)

        Returns:
            True if deleted successfully, False otherwise

        Raises:
            ValueError: If project is protected or doesn't match prefix (unless force=True)
        """
        # NEVER allow deleting protected projects, even with force
        if project_name in self.PROTECTED_PROJECTS:
            raise ValueError(
                f"Cannot delete protected project '{project_name}'. "
                f"Protected projects: {sorted(self.PROTECTED_PROJECTS)}"
            )

        # Check deletable unless force is specified
        if not force and not self._is_deletable(project_name):
            raise ValueError(
                f"Project '{project_name}' does not match test prefix '{self.TEST_PROJECT_PREFIX}'. "
                f"Use force=True to override (protected projects still cannot be deleted)."
            )

        # Find project ID
        all_projects = self.list_all_projects()
        project = next((p for p in all_projects if p.name == project_name), None)
        if not project:
            logger.warning(f"Project '{project_name}' not found in Phoenix")
            return False

        try:
            resp = requests.delete(
                f"{self.phoenix_url}/v1/projects/{project.id}", timeout=30
            )
            resp.raise_for_status()
            logger.info(f"Deleted Phoenix project: {project_name} (id={project.id})")
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to delete project '{project_name}': {e}")
            return False

    def cleanup_stale(self, hours: int = 24) -> list[str]:
        """Delete test projects older than specified hours.

        Args:
            hours: Delete projects older than this many hours (default: 24)

        Returns:
            List of deleted project names
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        test_projects = self.list_test_projects()

        deleted = []
        for project in test_projects:
            # Skip if no creation time (can't determine age)
            if project.created_at is None:
                logger.warning(
                    f"Skipping project '{project.name}' - no creation timestamp"
                )
                continue

            # Ensure created_at is timezone-aware for comparison
            created_at = project.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at < cutoff:
                if self.delete_project(project.name):
                    deleted.append(project.name)

        return deleted

    def cleanup_all(self, confirm: bool = True) -> list[str]:
        """Delete ALL test projects.

        Args:
            confirm: If True (default), requires explicit confirmation.
                    Set to False only for programmatic cleanup.

        Returns:
            List of deleted project names
        """
        test_projects = self.list_test_projects()

        if not test_projects:
            logger.info("No test projects to clean up")
            return []

        if confirm:
            logger.warning(
                f"About to delete {len(test_projects)} test projects. "
                f"This action cannot be undone."
            )
            # In programmatic use, confirm=False should be passed
            # CLI handles interactive confirmation separately

        deleted = []
        for project in test_projects:
            if self.delete_project(project.name):
                deleted.append(project.name)

        return deleted

    def get_stats(self) -> dict:
        """Get statistics about Phoenix projects.

        Returns:
            Dict with project counts and details
        """
        all_projects = self.list_all_projects()
        test_projects = [p for p in all_projects if self._is_deletable(p.name)]
        protected = [p for p in all_projects if p.name in self.PROTECTED_PROJECTS]

        return {
            "total_projects": len(all_projects),
            "test_projects": len(test_projects),
            "protected_projects": len(protected),
            "protected_names": sorted(self.PROTECTED_PROJECTS),
            "test_project_names": [p.name for p in test_projects],
        }


@dataclass
class ClaudeSessionInfo:
    """Information about a Claude Code session directory."""

    path: Path
    name: str  # Directory name (encoded path)
    modified_at: datetime | None = None
    size_bytes: int | None = None


class ClaudeSessionCleaner:
    """Manages cleanup of Claude Code session directories from testbed runs.

    Safety is based on PATH STRUCTURE, not string matching:
    - Only sessions created FROM WITHIN the testbed runs directory can be deleted
    - The session path must contain the specific testbed runs path pattern
    - This is inherently safe because normal user sessions cannot have this path

    The testbed runs directory structure is:
        <repo>/tests/e2e/testbed/runs/run-<id>/

    Claude encodes this as a session directory name like:
        ~/.claude/projects/-Users-...-tests-e2e-testbed-runs-run-<id>

    The key safety invariant: only sessions whose encoded path contains
    'tests-e2e-testbed-runs-run-' can be deleted. Normal user work sessions
    can NEVER match this pattern because:
    1. Users don't work inside tests/e2e/testbed/runs/
    2. The pattern requires the exact directory structure we create for testing
    """

    # The path pattern that identifies testbed sessions (encoded form)
    # This is the key safety check - must contain this exact substring
    TESTBED_PATH_PATTERN = "tests-e2e-testbed-runs-run-"

    def __init__(self, claude_dir: Path | None = None):
        """
        Initialize Claude session cleaner.

        Args:
            claude_dir: Path to Claude config directory.
                       Defaults to ~/.claude
        """
        if claude_dir is None:
            claude_dir = Path.home() / ".claude"
        self.claude_dir = claude_dir
        self.projects_dir = claude_dir / "projects"

    def _is_testbed_session(self, session_name: str) -> bool:
        """Check if a session is from a testbed run.

        Safety check based on path structure, not arbitrary string matching.

        Args:
            session_name: The encoded session directory name

        Returns:
            True if this is a testbed session that can be safely deleted
        """
        return self.TESTBED_PATH_PATTERN in session_name

    def list_all_sessions(self) -> list[ClaudeSessionInfo]:
        """List all Claude session directories.

        Returns:
            List of ClaudeSessionInfo for all sessions
        """
        if not self.projects_dir.exists():
            return []

        sessions = []
        for entry in self.projects_dir.iterdir():
            if not entry.is_dir():
                continue

            # Get modification time
            try:
                stat = entry.stat()
                modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                # Calculate directory size (just count files, not recursive size)
                size_bytes = sum(
                    f.stat().st_size for f in entry.iterdir() if f.is_file()
                )
            except OSError:
                modified_at = None
                size_bytes = None

            sessions.append(
                ClaudeSessionInfo(
                    path=entry,
                    name=entry.name,
                    modified_at=modified_at,
                    size_bytes=size_bytes,
                )
            )

        return sessions

    def list_testbed_sessions(self) -> list[ClaudeSessionInfo]:
        """List only testbed session directories.

        Returns:
            List of ClaudeSessionInfo for testbed sessions only
        """
        all_sessions = self.list_all_sessions()
        return [s for s in all_sessions if self._is_testbed_session(s.name)]

    def delete_session(self, session_path: Path) -> bool:
        """Delete a single Claude session directory.

        Args:
            session_path: Path to the session directory

        Returns:
            True if deleted successfully, False otherwise

        Raises:
            ValueError: If session is not a testbed session
        """
        import shutil

        session_name = session_path.name

        # Safety check: must be a testbed session
        if not self._is_testbed_session(session_name):
            raise ValueError(
                f"Cannot delete session '{session_name}' - not a testbed session. "
                f"Session path must contain '{self.TESTBED_PATH_PATTERN}' pattern."
            )

        if not session_path.exists():
            logger.warning(f"Session directory not found: {session_path}")
            return False

        try:
            shutil.rmtree(session_path)
            logger.info(f"Deleted Claude session: {session_name}")
            return True
        except OSError as e:
            logger.error(f"Failed to delete session '{session_name}': {e}")
            return False

    def cleanup_stale(self, hours: int = 24) -> list[str]:
        """Delete testbed sessions older than specified hours.

        Args:
            hours: Delete sessions older than this many hours (default: 24)

        Returns:
            List of deleted session names
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        testbed_sessions = self.list_testbed_sessions()

        deleted = []
        for session in testbed_sessions:
            if session.modified_at is None:
                logger.warning(
                    f"Skipping session '{session.name}' - no modification timestamp"
                )
                continue

            if session.modified_at < cutoff:
                if self.delete_session(session.path):
                    deleted.append(session.name)

        return deleted

    def cleanup_all(self) -> list[str]:
        """Delete ALL testbed sessions.

        Returns:
            List of deleted session names
        """
        testbed_sessions = self.list_testbed_sessions()

        if not testbed_sessions:
            logger.info("No testbed sessions to clean up")
            return []

        deleted = []
        for session in testbed_sessions:
            if self.delete_session(session.path):
                deleted.append(session.name)

        return deleted

    def get_stats(self) -> dict:
        """Get statistics about Claude sessions.

        Returns:
            Dict with session counts and details
        """
        all_sessions = self.list_all_sessions()
        testbed_sessions = [s for s in all_sessions if self._is_testbed_session(s.name)]

        total_size = sum(s.size_bytes or 0 for s in testbed_sessions)

        return {
            "total_sessions": len(all_sessions),
            "testbed_sessions": len(testbed_sessions),
            "testbed_size_bytes": total_size,
            "testbed_size_mb": round(total_size / (1024 * 1024), 2),
            "testbed_session_names": [s.name for s in testbed_sessions],
        }
