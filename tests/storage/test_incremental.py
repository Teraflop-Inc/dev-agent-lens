"""Incremental updates expose a complete snapshot, never a half-rebuilt store."""

import datetime as dt
import json
import os
import uuid

import duckdb
import pandas as pd
import pytest

from dev_agent_lens.storage.layouts import get_layout
from dev_agent_lens.storage.spanstore import open_store


def append(store, con, day, sid, trace=None, attributes=None, parent=None):
    stamp = dt.datetime.fromisoformat(day).replace(hour=12, tzinfo=dt.timezone.utc)
    frame = pd.DataFrame(
        [
            dict(
                span_id=sid,
                trace_id=trace or sid,
                parent_id=parent,
                name="root",
                span_kind="LLM",
                start_time=stamp,
                end_time=stamp,
                status_code="OK",
                attributes=attributes
                if isinstance(attributes, str)
                else json.dumps(attributes or {}),
                events="[]",
                llm_token_count_prompt=1,
                llm_token_count_completion=2,
            )
        ]
    )
    store.append_frame(con, frame, source="test")


def test_updates_only_changed_days_and_noop_does_not_publish(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "a")
    append(store, con, "2026-09-02", "b")
    first = lay.update(con, store, zstd_level=3)
    lay.attach(con, store)
    assert con.sql("SELECT count(*) FROM spans").fetchone()[0] == 2
    append(store, con, "2026-09-02", "c")
    second = lay.update(con, store, zstd_level=3)
    assert second.detail["rebuilt_days"] == ["2026-09-02"]
    lay.attach(con, store)
    assert con.sql("SELECT span_id FROM spans ORDER BY span_id").fetchall() == [
        ("a",),
        ("b",),
        ("c",),
    ]
    assert second.detail["snapshot"] != first.detail["snapshot"]
    noop = lay.update(con, store, zstd_level=3)
    assert noop.detail["rebuilt_days"] == []
    assert noop.detail["snapshot"] == second.detail["snapshot"]


def test_late_parent_rebuilds_connected_trace_and_session_days(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "child", trace="a", parent="parent")
    append(store, con, "2026-09-02", "other", trace="b", attributes={"session.id": "session"})
    # JSON attributes use the nested producer shape.
    append(
        store,
        con,
        "2026-09-02",
        "session-root",
        trace="b",
        attributes={"session": {"id": "session"}},
    )
    append(store, con, "2026-09-04", "unrelated")
    lay.update(con, store, zstd_level=3)
    append(
        store,
        con,
        "2026-09-03",
        "parent",
        trace="a",
        attributes={"session": {"id": "session"}, "user": {"id": "alice"}},
    )
    result = lay.update(con, store, zstd_level=3)
    assert result.detail["rebuilt_days"] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    lay.attach(con, store)
    assert con.sql(
        "SELECT account_uuid FROM spans WHERE span_id IN ('child','other') ORDER BY span_id"
    ).fetchall() == [("alice",), ("alice",)]


def test_failed_build_preserves_both_existing_and_new_readers(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "a")
    lay.update(con, store, zstd_level=3)
    reader = duckdb.connect()
    lay.attach(reader, store)
    append(store, con, "2026-09-02", "bad", attributes="{")
    with pytest.raises(duckdb.InvalidInputException):
        lay.update(con, store, zstd_level=3)
    assert reader.sql("SELECT span_id FROM spans").fetchall() == [("a",)]
    lay.attach(con, store)
    assert con.sql("SELECT span_id FROM spans").fetchall() == [("a",)]


def test_identity_mapping_change_rebuilds_all_days(tmp_path, monkeypatch):
    identity = tmp_path / "identity.yaml"
    identity.write_text("people: []\n")
    monkeypatch.setenv("DAL_IDENTITY", str(identity))
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    for day in ("2026-09-01", "2026-09-02"):
        append(store, con, day, day, attributes={"user": {"id": "alice"}})
    lay.update(con, store, zstd_level=3)
    identity.write_text("people:\n  - name: Alice\n    users: [alice]\n")
    result = lay.update(con, store, zstd_level=3)
    assert result.detail["rebuilt_days"] == ["2026-09-01", "2026-09-02"]
    lay.attach(con, store)
    assert con.sql("SELECT DISTINCT person FROM spans").fetchall() == [("Alice",)]


def test_rebuilding_neighbor_does_not_cascade_through_unrelated_sessions(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(
        store,
        con,
        "2026-09-01",
        "old",
        trace="unrelated",
        attributes={"session": {"id": "other-session"}, "user": {"id": "bob"}},
    )
    append(store, con, "2026-09-02", "neighbor", trace="unrelated", parent="old")
    append(store, con, "2026-09-02", "child", trace="changed", parent="late")
    lay.update(con, store, zstd_level=3)
    append(store, con, "2026-09-03", "late", trace="changed", attributes={"user": {"id": "alice"}})
    result = lay.update(con, store, zstd_level=3)
    assert result.detail["rebuilt_days"] == ["2026-09-02", "2026-09-03"]
    lay.attach(con, store)
    assert con.sql(
        "SELECT span_id,account_uuid FROM spans "
        "WHERE span_id IN ('child','neighbor') ORDER BY span_id"
    ).fetchall() == [("child", "alice"), ("neighbor", "bob")]


def test_deleted_parent_removes_attribution_but_old_reader_keeps_snapshot(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(
        store,
        con,
        "2026-09-01",
        "parent",
        attributes={"session": {"id": "s"}, "user": {"id": "alice"}},
    )
    append(store, con, "2026-09-02", "child", attributes={"session": {"id": "s"}})
    lay.update(con, store, zstd_level=3)
    reader = duckdb.connect()
    lay.attach(reader, store)
    for path in (store.root / "spans_raw" / "day=2026-09-01").glob("*.parquet"):
        path.unlink()
    lay.update(con, store, zstd_level=3)
    lay.attach(con, store)
    assert con.sql("SELECT span_id,account_uuid FROM spans").fetchall() == [("child", None)]
    assert reader.sql("SELECT count(*) FROM spans").fetchone()[0] == 2
    for path in (store.root / "spans_raw").rglob("*.parquet"):
        path.unlink()
    lay.update(con, store, zstd_level=3)
    lay.attach(con, store)
    assert con.sql("SELECT count(*) FROM spans").fetchone()[0] == 0


def test_shared_blob_is_queryable_once_across_generations(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    attrs = {"llm": {"anthropic": {"messages": "shared payload"}}}
    append(store, con, "2026-09-01", "a", attributes=attrs)
    lay.update(con, store, zstd_level=3)
    append(store, con, "2026-09-02", "b", attributes=attrs)
    lay.update(con, store, zstd_level=3)
    lay.attach(con, store)
    assert con.sql("SELECT kind,body FROM blobs").fetchall() == [("messages", "shared payload")]
    assert (
        con.sql(
            "SELECT count(*) FROM spans s JOIN blobs b ON s.payload_messages_ref=b.ref"
        ).fetchone()[0]
        == 2
    )


def test_cli_rebuild_publishes_and_full_option_rebuilds_unchanged_days(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from dev_agent_lens.cli.main import main

    store = open_store(str(tmp_path / "store"))
    store.ensure()
    monkeypatch.setenv("DAL_SPAN_STORE", store.uri)
    monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
    append(store, duckdb.connect(), "2026-09-01", "a")
    runner = CliRunner()
    first = runner.invoke(main, ["store", "rebuild"])
    assert first.exit_code == 0, first.output
    assert "rebuilt_days=1" in first.output
    second = runner.invoke(main, ["store", "rebuild"])
    assert second.exit_code == 0, second.output
    assert "rebuilt_days=0" in second.output
    forced = runner.invoke(main, ["store", "rebuild", "--full"])
    assert forced.exit_code == 0, forced.output
    assert "rebuilt_days=1" in forced.output
    status = runner.invoke(main, ["store", "status"])
    assert status.exit_code == 0, status.output
    assert "snapshot=" in status.output
    assert "spans_typed  empty" not in status.output


@pytest.mark.parametrize("layout_args", [[], ["--layout", "typed"]])
def test_cli_queries_published_snapshot(tmp_path, monkeypatch, layout_args):
    from click.testing import CliRunner

    from dev_agent_lens.cli.main import main

    store = open_store(str(tmp_path / "store"))
    store.ensure()
    monkeypatch.setenv("DAL_SPAN_STORE", store.uri)
    monkeypatch.setenv("DAL_CONFIG_PATH", str(tmp_path / "config"))
    monkeypatch.delenv("DAL_SPAN_LAYOUT", raising=False)
    append(store, duckdb.connect(), "2026-09-01", "a")
    runner = CliRunner()
    built = runner.invoke(main, ["store", "rebuild"])
    assert built.exit_code == 0, built.output
    result = runner.invoke(main, ["store", "query", *layout_args, "--format", "json",
                                 "SELECT tokens_prompt FROM spans"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"tokens_prompt": 1}]


def test_legacy_rebuild_cannot_silently_leave_snapshot_readers_stale(tmp_path):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "a")
    lay.update(con, store, zstd_level=3)
    with pytest.raises(ValueError, match="store rebuild"):
        lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3)


def test_competing_updater_cannot_replace_newer_publication(tmp_path, monkeypatch):
    from dev_agent_lens.storage.snapshots import SnapshotIO

    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "a")
    lay.update(con, store, zstd_level=3)
    original = lay.build
    winner = []

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        winner.append(get_layout("typed").update(duckdb.connect(), store, zstd_level=3, full=True))
        return result

    monkeypatch.setattr(lay, "build", race)
    with pytest.raises(RuntimeError, match="snapshot changed"):
        lay.update(con, store, zstd_level=3, full=True)
    manifest, _ = SnapshotIO(store).read_current()
    assert manifest["snapshot"] == winner[0].detail["snapshot"]


def test_append_during_build_is_picked_up_next_pass(tmp_path, monkeypatch):
    store = open_store(str(tmp_path / "store"))
    store.ensure()
    con = duckdb.connect()
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "a")
    original = lay.build

    def append_during_build(*args, **kwargs):
        result = original(*args, **kwargs)
        append(store, duckdb.connect(), "2026-09-02", "b")
        return result

    monkeypatch.setattr(lay, "build", append_during_build)
    assert lay.update(con, store, zstd_level=3).rows == 1
    monkeypatch.setattr(lay, "build", original)
    assert lay.update(con, store, zstd_level=3).rows == 2


@pytest.mark.skipif(
    not os.environ.get("DAL_INCREMENTAL_TEST_STORE"), reason="object store not configured"
)
def test_object_store_snapshot_publication(monkeypatch):
    """Optional live S3/R2 check; writes only under a fresh validation generation."""
    from botocore.exceptions import ClientError
    from dotenv import dotenv_values

    from dev_agent_lens.storage.snapshots import SnapshotIO

    credentials = dotenv_values(os.environ["DAL_INCREMENTAL_TEST_ENV"])
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        if credentials.get(key):
            monkeypatch.setenv(key, credentials[key])
    parent = open_store(os.environ["DAL_INCREMENTAL_TEST_STORE"])
    store = SnapshotIO(parent).generation("validation-" + uuid.uuid4().hex)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    lay = get_layout("typed")
    append(store, con, "2026-09-01", "child", trace="trace", parent="root")
    lay.update(con, store, zstd_level=3)
    old, token = SnapshotIO(store).read_current()
    reader = duckdb.connect()
    lay.attach(reader, store)
    append(
        store, con, "2026-09-02", "root", trace="trace", attributes={"user": {"id": "synthetic"}}
    )
    latest = lay.update(con, store, zstd_level=3)
    lay.attach(con, store)
    assert con.sql("SELECT DISTINCT account_uuid FROM spans").fetchall() == [("synthetic",)]
    assert reader.sql("SELECT count(*) FROM spans").fetchone()[0] == 1
    assert lay.update(con, store, zstd_level=3).detail["rebuilt_days"] == []
    old["snapshot"] = "stale-" + uuid.uuid4().hex
    with pytest.raises(ClientError) as error:
        SnapshotIO(store).publish(old, token)
    assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] == 412
    assert SnapshotIO(store).read_current()[0]["snapshot"] == latest.detail["snapshot"]


@pytest.mark.parametrize("legacy_id", ["user_fixture_account_legacy", "{not-json"])
def test_dependency_scan_tolerates_opaque_user_ids(tmp_path, legacy_id):
    """Cached producer IDs need not be JSON; valid session links still propagate."""
    from dev_agent_lens.storage.layouts.incremental import dependency_days

    con = duckdb.connect()
    con.execute("CREATE TABLE facts(day VARCHAR, trace_key VARCHAR, euid VARCHAR, "
                "session_alt VARCHAR, session_rx VARCHAR)")
    con.executemany("INSERT INTO facts VALUES (?,?,?,?,?)", [
        ("2026-09-01", "json-parent", '{"session_id":"linked"}', None, None),
        ("2026-09-02", "changed", None, "linked", None),
        ("2026-09-03", "opaque", legacy_id, None, None),
    ])
    path = str(tmp_path / "dependencies.parquet")
    con.table("facts").write_parquet(path)
    previous = {"dependencies": {"2026-09-01": [path]}}
    _, affected = dependency_days(con, {}, previous, {"2026-09-02"}, None, False)
    assert affected == {"2026-09-01", "2026-09-02"}
