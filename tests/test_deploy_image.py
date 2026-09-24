"""The image carries every script its entrypoint runs.

deploy/entrypoint.sh ran `python scripts/store_health.py` while deploy/Dockerfile never
copied it, so the daily content sentinel on lambda1 died on "can't open file" at every run
from 2026-09-11 to 09-21. Python exits 2 on a missing file, the sentinel's code for
"inconclusive", so nothing looked wrong. This holds the two files in step.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = re.compile(r"\bscripts/[\w.-]+\.(?:py|sh)\b")


def _copied_scripts(dockerfile: str) -> set[str] | None:
    """Script paths the Dockerfile COPYs; None when it copies the whole directory."""
    copied: set[str] = set()
    for line in dockerfile.splitlines():
        if not line.strip().startswith("COPY"):
            continue
        if re.search(r"\sscripts/?\s", line):
            return None
        copied.update(SCRIPT.findall(line))
    return copied


def test_every_script_the_entrypoint_runs_is_in_the_image():
    called = set(SCRIPT.findall((ROOT / "deploy" / "entrypoint.sh").read_text()))
    assert called, "entrypoint.sh names no scripts/ file; this test is reading the wrong thing"
    copied = _copied_scripts((ROOT / "deploy" / "Dockerfile").read_text())
    if copied is None:
        return
    missing = sorted(called - copied)
    assert not missing, f"entrypoint.sh runs scripts the image does not have: {missing}"


def test_the_check_reads_a_missing_script_as_a_missing_copy():
    # The failure this file exists for, reproduced: before the fix the Dockerfile's COPY
    # line named the oracle and the daily script but not the sentinel.
    before = "COPY scripts/migration_oracle.py scripts/oracle_daily.sh ./scripts/\n"
    assert "scripts/store_health.py" not in _copied_scripts(before)
    assert _copied_scripts("COPY scripts ./scripts\n") is None


def test_compose_uses_the_selected_object_store_without_minio_dependencies():
    """The hosted override must resolve to the requested store, not local MinIO."""
    import json
    import os
    import subprocess

    store = "s3://example-dal/spans?endpoint=account.r2.cloudflarestorage.com&region=auto"
    env = {
        **os.environ,
        "DAL_SPAN_STORE": store,
        "DAL_RAW_GLOB": "s3://example-dal/spans/spans_raw/**/*.parquet",
        "AWS_ACCESS_KEY_ID": "test-access",
        "AWS_SECRET_ACCESS_KEY": "test-secret",
        "AWS_REGION": "auto",
        "DAL_STORE_USER": "local-user",
        "DAL_STORE_PASSWORD": "local-secret",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose.hosted.yml"),
            "--profile",
            "maintenance",
            "config",
            "--format",
            "json",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    for name in ("dal-sync", "dal-otlp", "dal-oracle"):
        service = config["services"][name]
        assert service["environment"]["DAL_SPAN_STORE"] == store
        assert service["environment"]["AWS_ACCESS_KEY_ID"] == "test-access"
        assert "minio" not in service.get("depends_on", {})
    assert "minio" not in config["services"]
    assert config["services"]["dal-sync"]["profiles"] == ["maintenance"]
