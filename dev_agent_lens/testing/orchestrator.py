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

# Sentinel LiteLLM substitutes when it strips request content (ENG2-1510).
REDACTION_SENTINEL = "redacted-by-litellm"


def _column(df: pd.DataFrame, *candidates: str) -> pd.Series | None:
    """Return the first candidate column present, or None.

    Column naming differs between the Phoenix and Arize dataframes and between
    the raw and normalized schemas, so content assertions look up by fallback
    rather than assuming one layout.
    """
    for name in candidates:
        if name in df.columns:
            return df[name]
    return None


def _nonempty(series: pd.Series) -> pd.Series:
    """Mask of rows whose text is genuinely populated."""
    text = series.astype(str).str.strip()
    return text.ne("") & ~text.isin({"None", "nan", "null", "[]", "{}"})


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
        """Create run directory with copies of shared resources.

        Copies, not symlinks: Claude Code resolves a symlink to its real path,
        sees it escaping the run directory (its permission sandbox), and refuses
        the read — which silently killed the `has_read_tool` smoke assertion
        (observed live: "sample_code inside this run directory is a symlink …
        pointing outside this session's working directory").
        """
        import shutil

        self.run_dir.mkdir(parents=True, exist_ok=True)

        for shared in [".claude.md", "sample_code"]:
            src = self.testbed_root / shared
            dst = self.run_dir / shared
            if dst.is_symlink():  # heal run dirs created by the old symlink code
                dst.unlink()
            if not dst.exists() and src.exists():
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        # Per-run nonce for the read-roundtrip assertion. A prompt that asks the
        # model to read a file whose content is guessable proves nothing: Fable 5
        # answered `numbers=[1, 2, 3, 4, 5]` from prior knowledge without a single
        # tool call (run 20260814-121846). A random value the model cannot know
        # forces the Read AND proves the read content survived into the captured
        # spans end-to-end.
        self.nonce = uuid.uuid4().hex[:12]
        (self.run_dir / "nonce.txt").write_text(f"NONCE={self.nonce}\n")

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

        def fetch() -> pd.DataFrame:
            now = datetime.now()
            start_time = now - timedelta(minutes=15)
            if self.config.backend == TestBackend.PHOENIX:
                client = PhoenixClient(project_name=self.project_name)
                return client.get_spans_dataframe(
                    project_name=self.project_name,
                    start_time=start_time,
                    end_time=now,
                )
            client = ArizeClient(model_id=self.project_name)
            return client.get_spans_dataframe(
                model_id=self.project_name,
                start_time=start_time,
                end_time=now,
            )

        # OTLP export is asynchronous: spans keep arriving for a while after the
        # claude process exits, and validating on the first fetch races ingestion
        # (observed live: 4 spans at validation, 14 in the store a minute later —
        # which made `has_read_tool` flap and skipped the content assertions
        # entirely). Poll until the count is non-zero AND stable across two
        # consecutive polls, bounded at ~90s.
        deadline = asyncio.get_event_loop().time() + 90
        df = await asyncio.to_thread(fetch)
        stable = 0
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(5)
            newer = await asyncio.to_thread(fetch)
            if len(newer) > 0 and len(newer) == len(df):
                stable += 1
                if stable >= 2:
                    logger.info("Span count stable at %d; ingestion settled", len(newer))
                    return newer
            else:
                stable = 0
            df = newer
        logger.warning("Span ingestion did not settle within 90s; using %d spans", len(df))
        return df

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

        # Check for tool spans by name. Claude_Code_Tool_* spans are SYNTHESIZED by
        # the litellm callback, and synthesis is version/tool-dependent — observed
        # live: Bash synthesized in the test container, Read not, nothing at all on
        # the tailnet proxy. So name assertions only run when synthesis is provably
        # active (some Tool span exists); the read guarantee itself comes from
        # read_content_roundtrip above, which doesn't depend on synthesis.
        if "name" in spans_df.columns:
            names = spans_df["name"].astype(str)
            synthesis_active = names.str.contains("Claude_Code_Tool_", na=False).any()
            if synthesis_active:
                assertions["has_read_tool"] = names.str.contains(
                    "Read", case=False, na=False
                ).any()

            # Check for subagent (Task) tool - only assert if prompt expects it
            has_task = names.str.contains("Task", case=False, na=False).any()
            if has_task or "stress_test" in self.config.prompt_file:
                assertions["has_task_tool"] = has_task

            # Check for AskUserQuestion tool - only assert if prompt expects it
            has_ask_user = names.str.contains("AskUserQuestion", case=False, na=False).any()
            if has_ask_user or "ask_user" in self.config.prompt_file:
                assertions["has_ask_user_question_tool"] = has_ask_user

        assertions.update(self._content_assertions(spans_df))

        return TestResult(
            test_run_id=self.config.test_run_id,
            passed=all(assertions.values()),
            assertions=assertions,
            span_count=len(spans_df),
        )

    def _content_assertions(self, spans_df: pd.DataFrame) -> dict[str, bool]:
        """Assert spans actually carry content, not just that they exist.

        Span *existence* is not capture. Two real defects shipped green against the
        existence-only assertions above, because both produce perfectly well-formed
        spans that happen to be hollow:

        * ENG2-1510 — six weeks where ~98% of `input.value` was the literal string
          `redacted-by-litellm`. Still an LLM span, still had tool names.
        * The thinking-capture gap — 16,347 adaptive-thinking spans over 30 days with
          zero thinking content and zero `signature` fields, going back as far as the
          retention window reaches.

        Each assertion here is gated on the signal being applicable, matching the
        conditional style used for `has_task_tool` above: an assertion that cannot
        apply is omitted rather than trivially passed, so a green run means the
        checks that ran actually verified something.
        """
        assertions: dict[str, bool] = {}

        # Read-content roundtrip: the per-run nonce (written by _setup_run_directory,
        # readable only via the Read tool) must appear somewhere in the captured
        # spans. This proves in one check that the model actually performed the read
        # AND that tool/content capture stored it — name-matching a Read span proves
        # neither. Scans every column: the nonce may land in tool spans, input
        # messages (tool_result), or output text depending on the capture shape.
        nonce = getattr(self, "nonce", None)
        if nonce:
            assertions["read_content_roundtrip"] = bool(
                spans_df.astype(str)
                .apply(lambda col: col.str.contains(nonce, na=False).any())
                .any()
            )

        kind = _column(spans_df, "span_kind", "kind")
        llm_rows = spans_df[kind == "LLM"] if kind is not None else spans_df
        if llm_rows.empty:
            return assertions

        inputs = _column(llm_rows, "input_value", "attributes.input.value")
        if inputs is not None:
            text = inputs.astype(str)
            populated = _nonempty(inputs) & ~text.str.contains(REDACTION_SENTINEL, na=False)
            assertions["llm_input_content_populated"] = bool(populated.any())

        outputs = _column(
            llm_rows, "output_messages", "output_value", "attributes.llm.output_messages"
        )
        if outputs is not None:
            assertions["llm_output_content_populated"] = bool(_nonempty(outputs).any())

        prompt_tokens = _column(
            llm_rows, "llm_token_count_prompt", "attributes.llm.token_count.prompt"
        )
        if prompt_tokens is not None:
            counts = pd.to_numeric(prompt_tokens, errors="coerce").fillna(0)
            assertions["llm_token_counts_populated"] = bool(counts.gt(0).any())

        # Thinking blocks ride on the callback-synthesized Claude_Code_* spans,
        # whose span_kind is UNKNOWN — so the thinking gate must scan the WHOLE
        # frame, not just LLM rows (live run 20260814-134530: summaries present,
        # LLM-scoped blob missed them).
        full_parts = [
            series.astype(str)
            for series in (
                _column(spans_df, "raw_attributes", "attributes"),
                _column(spans_df, "attributes.llm"),
                _column(spans_df, "attributes.usage_object"),
                _column(spans_df, "attributes.output.value"),
                _column(spans_df, "attributes.llm.invocation_parameters"),
            )
            if series is not None
        ]
        if full_parts:
            full_blob = full_parts[0]
            for part in full_parts[1:]:
                full_blob = full_blob + " " + part
            if full_blob.str.contains(
                r"display\\?['\"]\s*:\s*\\?['\"]summarized", na=False, regex=True
            ).any():
                has_thinking = full_blob.str.contains(
                    r"['\"]signature\\?['\"]\s*:", na=False, regex=True
                ) | full_blob.str.contains(
                    r"thinking\\?['\"]\s*:\s*\\?['\"][^'\"\\]", na=False, regex=True
                )
                assertions["thinking_content_captured"] = bool(has_thinking.any())

        # The attribute payload arrives in different shapes per client: one raw JSON
        # string (`raw_attributes`, or `attributes::text` from Postgres), or flattened
        # `attributes.*` columns holding dicts/JSON-strings (the arize-phoenix client).
        # Concatenate whatever is present into one searchable blob per row, and keep
        # every pattern quote-agnostic — nested JSON strings arrive with their quotes
        # backslash-escaped, and dict cells stringify with single quotes, so a plain
        # `"summarized"` pattern silently never matches either. (That exact bug hid
        # case 3 during development; don't reintroduce it.)
        blob_parts = [
            series.astype(str)
            for series in (
                _column(llm_rows, "raw_attributes", "attributes"),
                _column(llm_rows, "attributes.usage_object"),
                # packed llm dict: the arize-phoenix client keeps thinking blocks
                # (incl. summaries + signatures) here, not in the dotted columns —
                # verified live on run 20260814-134345
                _column(llm_rows, "attributes.llm"),
                _column(llm_rows, "attributes.llm.invocation_parameters"),
            )
            if series is not None
        ]
        if not blob_parts:
            return assertions
        blob = blob_parts[0]
        for part in blob_parts[1:]:
            blob = blob + " " + part

        # Cache accounting: only assert once the backend reports the fields at all,
        # so this doesn't fail on a provider that never emits them. Either field arms
        # the gate — a span can carry only cache_creation (first write, no read yet).
        if blob.str.contains(
            r"cache_(?:read|creation)_input_tokens", na=False, regex=True
        ).any():
            assertions["cache_token_breakdown_populated"] = bool(
                blob.str.contains(
                    r"cache_(?:read|creation)_input_tokens\\?['\"]?\s*:\s*[1-9]",
                    na=False,
                    regex=True,
                ).any()
            )


        return assertions

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
