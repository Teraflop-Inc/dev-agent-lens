"""An unattended capture install must never choose someone else's endpoint or scope."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_capture_requires_an_explicit_destination_and_scope(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DAL_CODEX_")}
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/codex_capture.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "DAL_CODEX_ENDPOINT" in result.stderr
