"""Test isolation: the suite must not read the developer's machine.

Without this, results depend on whoever runs them. Two real instances found 2026-09-04:

* `test_no_oxen_configured` and `test_repr_shows_path_and_status` read the real
  `~/.dal/config.json`. They pass on a laptop with no Oxen remote and fail on one that has
  it, for reasons that look unrelated to whatever change is being tested.
* Anything reading `OXEN_REMOTE_URL`, `DAL_SPAN_STORE` or AWS credentials picks up
  whatever happens to be exported in the shell, including values sourced from `.env`.

The fixture below is autouse, so isolation is the default and a test has to opt out
deliberately rather than remember to opt in. tests/e2e and anything marked `integration`
need a live Phoenix, a live proxy, or the real ~/.dal/data; they are skipped unless
DAL_E2E=1, because with them the suite's result depended on which machine ran it.
"""

from __future__ import annotations

import os

import pytest

# Environment that must never leak from the developer's shell into a test. Anything here
# is unset for every test; a test that needs one sets it explicitly with monkeypatch.
_ISOLATED_ENV = (
    # DAL configuration and span store
    "DAL_CONFIG_PATH",
    # The receiver reads the hook secret from the env; an operator shell that sourced
    # deploy/.env would otherwise turn every unsigned test delivery into a 401.
    "DAL_EVENTS_SECRET",
    "DAL_SPAN_STORE",
    "DAL_SPAN_LAYOUT",
    "DAL_ZSTD_LEVEL",
    "DAL_DUCKDB_HTTPFS_PATH",
    "DAL_DEFAULT_BACKEND",
    "DAL_DATA_PATH",
    # Phoenix source selection. These were missing from the first list, and the `sync`
    # command was found to WRITE them into os.environ as a side effect, so one test's
    # source URL leaked into a later test's PhoenixClient() default.
    "DAL_PHOENIX_URL",
    "DAL_PHOENIX_PROJECT",
    # Oxen
    "OXEN_REMOTE_URL",
    "OXEN_API_KEY",
    "OXEN_HOME",
    # object storage credentials
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "DAL_S3_ENDPOINT",
    # trace sources and the live-service tests' own knobs
    "PHOENIX_SQL_DATABASE_URL",
    "PHOENIX_SQL_DATABASE_SCHEMA",
    "PHOENIX_API_KEY",
    "PHOENIX_COLLECTOR_ENDPOINT",
    "PHOENIX_URL",
    "CLAUDE_LENS_PROXY_URL",
    "TEST_SESSION_ID",
    "ARIZE_API_KEY",
    "ARIZE_SPACE_ID",
    "ARIZE_SPACE_KEY",
    # model providers, so no test can accidentally spend money
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)


@pytest.fixture(autouse=True)
def isolated_dal_environment(tmp_path, monkeypatch):
    """Strip host environment, then point DAL config at a per-test temp file.

    Deliberately ONE fixture rather than two. The first version split clearing and setting
    across two autouse fixtures, pytest ran them in the opposite order to the one written,
    and the clear wiped the config path the other had just set. The isolation silently did
    nothing and the same tests kept failing. Ordering between autouse fixtures is not
    something to rely on when one undoes the other.
    """
    for var in _ISOLATED_ENV:
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "dal-config.json"
    monkeypatch.setenv("DAL_CONFIG_PATH", str(cfg))


def pytest_collection_modifyitems(config, items):
    """Skip live-service tests unless explicitly requested."""
    if os.getenv("DAL_E2E"):
        return
    skip = pytest.mark.skip(reason="needs live services or real local data; set DAL_E2E=1")
    for item in items:
        if "/tests/e2e/" in str(item.fspath) or item.get_closest_marker("integration"):
            item.add_marker(skip)
