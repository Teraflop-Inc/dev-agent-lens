"""Git-host webhooks land as spans through the receiver's events route (ENG2-1622)."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request

import duckdb
import pytest

pytest.importorskip("opentelemetry.proto")

from dev_agent_lens.otlp_receiver import Receiver  # noqa: E402
from dev_agent_lens.storage.layouts import get_layout  # noqa: E402
from dev_agent_lens.storage.spanstore import open_store  # noqa: E402
from dev_agent_lens.webhook_events import event_to_row, verify_signature  # noqa: E402

REPO = {"full_name": "dal/dev-agent-lens", "name": "dev-agent-lens"}
ADA = {"login": "ada", "username": "ada"}

PUSH = {
    "ref": "refs/heads/rollout/sess-1",
    "before": "0" * 40,
    "after": "a" * 40,
    "commits": [
        {
            "id": "a" * 40,
            "message": "one",
            "timestamp": "2026-09-16T10:00:00-07:00",
            "author": {"username": "ada"},
        },
        {
            "id": "b" * 40,
            "message": "two",
            "timestamp": "2026-09-16T10:05:00-07:00",
            "author": {"username": "ada"},
        },
    ],
    "repository": REPO,
    "pusher": ADA,
    "sender": ADA,
}
PR_OPENED = {
    "action": "opened",
    "number": 7,
    "pull_request": {
        "number": 7,
        "title": "typed layout: ticket column",
        "state": "open",
        "merged": False,
        "user": ADA,
        "head": {"ref": "rollout/sess-1", "sha": "a" * 40},
        "base": {"ref": "main"},
        "created_at": "2026-09-16T17:10:00Z",
        "updated_at": "2026-09-16T17:10:00Z",
    },
    "repository": REPO,
    "sender": ADA,
}
REVIEW = {
    "action": "reviewed",
    "number": 7,
    "pull_request": PR_OPENED["pull_request"],
    "review": {"type": "pull_request_review_approved", "content": "lgtm"},
    "repository": REPO,
    "sender": {"login": "grace"},
}


def _headers(event: str, delivery: str) -> dict[str, str]:
    return {"X-Forgejo-Event": event, "X-Forgejo-Delivery": delivery}


def test_push_pr_and_review_type_out_the_fields_a_query_wants():
    push = event_to_row("forgejo", _headers("push", "d1"), json.dumps(PUSH).encode())
    a = json.loads(push["attributes"])
    assert push["name"] == "forgejo.push"
    assert a["git"]["repo"] == "dal/dev-agent-lens"
    assert a["git"]["ref"] == "refs/heads/rollout/sess-1"
    assert a["git"]["commits"] == 2
    assert a["git"]["actor"] == "ada" and a["user"]["id"] == "ada"
    assert a["git"]["last_commit_at"].startswith("2026-09-16T17:05:00")  # last commit, UTC
    assert push["start_time"] > push["start_time"].__class__(
        2026, 9, 16, 17, 5, tzinfo=push["start_time"].tzinfo
    )  # delivered, not committed

    pr = event_to_row("forgejo", _headers("pull_request", "d2"), json.dumps(PR_OPENED).encode())
    b = json.loads(pr["attributes"])
    assert pr["name"] == "forgejo.pull_request.opened"
    assert b["git"]["pr"]["number"] == 7 and b["git"]["pr"]["head"] == "rollout/sess-1"
    assert b["git"]["pr"]["author"] == "ada"

    rv = event_to_row(
        "forgejo", _headers("pull_request_review_approved", "d3"), json.dumps(REVIEW).encode()
    )
    c = json.loads(rv["attributes"])
    assert rv["name"] == "forgejo.pull_request_review_approved.reviewed"
    assert c["git"]["review"]["type"] == "pull_request_review_approved"
    assert c["git"]["actor"] == "grace"
    # the PR's opening and its review share a trace; the push has its own
    assert pr["context.trace_id"] == rv["context.trace_id"] != push["context.trace_id"]


def test_the_signed_body_makes_the_span_id_so_a_redelivery_or_a_replay_lands_once():
    body = json.dumps(PUSH).encode()
    a = event_to_row("forgejo", _headers("push", "same"), body)
    b = event_to_row("forgejo", _headers("push", "same"), body)
    # A captured signed body resent under a fresh delivery id is the replay an attacker
    # can mount without the secret; it must collapse onto the original span.
    c = event_to_row("forgejo", _headers("push", "other"), body)
    d = event_to_row("forgejo", _headers("push", "same"), json.dumps(PR_OPENED).encode())
    assert a["context.span_id"] == b["context.span_id"] == c["context.span_id"]
    assert d["context.span_id"] != a["context.span_id"]
    assert json.loads(c["attributes"])["git"]["delivery"] == "other"


def test_a_payload_missing_everything_still_lands():
    row = event_to_row("forgejo", {}, b"{}")
    assert row["name"] == "forgejo.unknown"
    with pytest.raises(ValueError):
        event_to_row("forgejo", {}, b"not json")
    with pytest.raises(ValueError):
        event_to_row("forgejo", {}, b"[1, 2]")


def _post(url: str, body: bytes, headers: dict[str, str]) -> int:
    r = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_receiver_lands_events_under_the_producer_source_and_the_typed_layout_names_the_actor(
    tmp_path, monkeypatch
):
    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    r = Receiver(
        store, con, host="127.0.0.1", port=0, default_source="x", flush_rows=2, flush_seconds=999
    )
    t = threading.Thread(target=r.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    base = f"http://127.0.0.1:{r.port}"
    try:
        assert (
            _post(f"{base}/v1/events/forgejo", json.dumps(PUSH).encode(), _headers("push", "d1"))
            == 200
        )
        assert (
            _post(
                f"{base}/v1/events/forgejo",
                json.dumps(PR_OPENED).encode(),
                _headers("pull_request", "d2"),
            )
            == 200
        )
        assert _post(f"{base}/v1/events/forgejo", b"nope", _headers("push", "d3")) == 400
        assert _post(f"{base}/v1/events/", b"{}", {}) == 404
    finally:
        r.stop()
        t.join(timeout=5)

    people = tmp_path / "identity.yaml"
    people.write_text("people:\n  - name: Ada\n    email: a@x\n    users: [ada]\n")
    monkeypatch.setenv("DAL_IDENTITY", str(people))
    lay = get_layout("typed")
    assert lay.build(con, store.read_glob("spans_raw"), store, zstd_level=3).rows == 2
    lay.attach(con, store)
    rows = con.execute(
        "SELECT name, source, person, json_extract_string(attributes_rest, '$.git.repo') "
        "FROM spans ORDER BY name"
    ).fetchall()
    assert rows == [
        ("forgejo.pull_request.opened", "forgejo", "Ada", "dal/dev-agent-lens"),
        ("forgejo.push", "forgejo", "Ada", "dal/dev-agent-lens"),
    ]


def _sig(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_github_and_forgejo_shapes_and_nothing_else():
    body = json.dumps(PUSH).encode()
    good = _sig(body, "s3")
    assert verify_signature({"X-Hub-Signature-256": "sha256=" + good}, body, "s3")
    assert verify_signature({"X-Forgejo-Signature": good}, body, "s3")
    assert verify_signature({"X-Gitea-Signature": good.upper()}, body, "s3")
    assert not verify_signature({}, body, "s3")
    assert not verify_signature({"X-Hub-Signature-256": "sha256=" + good}, body, "other")
    assert not verify_signature({"X-Hub-Signature-256": "sha256=" + good}, body + b" ", "s3")
    # A prefix match, a short one, and a header that is not hex at all (compare_digest
    # raises on non-ASCII; this must be a plain False, never a traceback).
    assert not verify_signature({"X-Forgejo-Signature": good[:8] + "0" * 56}, body, "s3")
    assert not verify_signature({"X-Forgejo-Signature": good[:8]}, body, "s3")
    assert not verify_signature({"X-Hub-Signature-256": "sha256=\u00e9" * 8}, body, "s3")
    assert not verify_signature({"X-Hub-Signature-256": "sha256=" + good + "00"}, body, "s3")
    # The header names a host actually sends, over the pretty-printed body GitHub sends.
    pretty = json.dumps(PUSH, indent=2).encode()
    assert verify_signature({"X-Gogs-Signature": _sig(pretty, "s3")}, pretty, "s3")
    assert verify_signature({"X-Forgejo-Signature": "  " + _sig(pretty, "s3") + "\n"}, pretty, "s3")
    assert not verify_signature({"X-Hub-Signature-256": "sha256=" + _sig(body, "s3")}, pretty, "s3")


def test_a_secret_on_the_receiver_turns_away_unsigned_and_missigned_deliveries(tmp_path):
    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    con = duckdb.connect()
    r = Receiver(
        store,
        con,
        host="127.0.0.1",
        port=0,
        default_source="x",
        flush_rows=1,
        flush_seconds=999,
        events_secret="hook-secret",
    )
    t = threading.Thread(target=r.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    base = f"http://127.0.0.1:{r.port}"
    body = json.dumps(PUSH, indent=2).encode()  # GitHub pretty-prints; sign the raw bytes
    try:
        assert _post(f"{base}/v1/events/github", body, {"X-GitHub-Event": "push"}) == 401
        wrong = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "w1",
            "X-Hub-Signature-256": "sha256=" + _sig(body, "no"),
        }
        assert _post(f"{base}/v1/events/github", body, wrong) == 401
        odd = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "w2",
            "X-Hub-Signature-256": "sha256=\u00e9",
        }
        assert _post(f"{base}/v1/events/github", body, odd) == 401
        assert _post(f"{base}/v1/events/github?x=1", body, {"X-GitHub-Event": "push"}) == 401
        assert _post(f"{base}/v1/events/a.b", body, {"X-GitHub-Event": "push"}) == 404
        assert _post(f"{base}/v1/events/GitHub", body, {"X-GitHub-Event": "push"}) == 404
        ok = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "g1",
            "X-Hub-Signature-256": "sha256=" + _sig(body, "hook-secret"),
        }
        assert _post(f"{base}/v1/events/github", body, ok) == 200
        forgejo = dict(_headers("push", "f1"), **{"X-Forgejo-Signature": _sig(body, "hook-secret")})
        assert _post(f"{base}/v1/events/forgejo", body, forgejo) == 200
    finally:
        r.stop()
        t.join(timeout=5)
    con2 = duckdb.connect()
    n = con2.execute(
        f"SELECT count(*) FROM read_parquet('{tmp_path}/s/spans_raw/**/*.parquet', hive_partitioning=1)"  # noqa: E501 -- complete test payload
    ).fetchone()[0]
    assert n == 2, "only the two signed deliveries landed"


def test_the_receiver_reads_the_secret_from_the_env_when_the_cli_passes_none(tmp_path, monkeypatch):
    """`dal otlp-receive` passes no secret; the env is the only production path."""
    monkeypatch.setenv("DAL_EVENTS_SECRET", "env-secret")
    store = open_store(f"file://{tmp_path}/s")
    store.ensure()
    r = Receiver(store, duckdb.connect(), host="127.0.0.1", port=0, flush_rows=1, flush_seconds=999)
    assert r.events_secret == "env-secret"
    t = threading.Thread(target=r.serve_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    base = f"http://127.0.0.1:{r.port}"
    body = json.dumps(PUSH).encode()
    try:
        assert _post(f"{base}/v1/events/github", body, {"X-GitHub-Event": "push"}) == 401
        ok = {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=" + _sig(body, "env-secret")}
        assert _post(f"{base}/v1/events/github", body, ok) == 200
        big = {"X-GitHub-Event": "push", "Content-Length": str(30 * 1024 * 1024)}
        assert _post(f"{base}/v1/events/github", b"", big) == 413
    finally:
        r.stop()
        t.join(timeout=5)
    monkeypatch.setenv("DAL_EVENTS_SECRET", "")
    assert Receiver(store, duckdb.connect(), host="127.0.0.1", port=0).events_secret is None


def test_a_github_review_types_out_the_same_fields_as_a_forgejo_one():
    gh = {
        "action": "submitted",
        "pull_request": PR_OPENED["pull_request"],
        "review": {"state": "approved", "body": "ship it", "user": {"login": "grace"}},
        "repository": REPO,
        "sender": {"login": "grace"},
    }
    row = event_to_row(
        "github",
        {"X-GitHub-Event": "pull_request_review", "X-GitHub-Delivery": "g9"},
        json.dumps(gh).encode(),
    )
    attrs = json.loads(row["attributes"])
    git = attrs["git"]
    assert row["name"] == "github.pull_request_review.submitted"
    assert git["review"] == {"type": "approved", "content": "ship it", "author": "grace"}
    assert git["pr"]["number"] == 7
    assert git["delivery"] == "g9" and git["actor"] == "grace" and attrs["user"]["id"] == "grace"
    opened = event_to_row(
        "github",
        {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "g8"},
        json.dumps(PR_OPENED).encode(),
    )
    assert row["context.trace_id"] == opened["context.trace_id"]


def test_repo_casing_never_splits_a_pull_requests_trace():
    """The hook says Teraflop-Inc/x; a backfill or a CLI call says teraflop-inc/x. Same PR,
    same trace, one spelling on the span."""
    a = event_to_row("github", {"X-GitHub-Event": "pull_request"}, json.dumps(PR_OPENED).encode())
    upper = {**PR_OPENED, "repository": {"full_name": "DAL/Dev-Agent-Lens"}}
    b = event_to_row("github", {"X-GitHub-Event": "pull_request"}, json.dumps(upper).encode())
    assert a["context.trace_id"] == b["context.trace_id"]
    assert json.loads(b["attributes"])["git"]["repo"] == "dal/dev-agent-lens"
