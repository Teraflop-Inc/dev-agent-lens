"""`dal github-backfill` replays PR history as signed deliveries that land once, dated
by the payload (ENG2-1538, 1539)."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest

from dev_agent_lens.github_backfill import (
    GitHub,
    Plan,
    deliveries_for_pr,
    plan_repo,
    post,
    sign,
)
from dev_agent_lens.webhook_events import event_to_row, verify_signature

REPO = "teraflop-inc/agent-forge"
ADA = {"login": "ada", "type": "User"}
GRACE = {"login": "grace", "type": "User"}
BOT = {"login": "greptile-apps[bot]", "type": "Bot"}

PR = {
    "number": 42,
    "title": "typed layout: ticket column",
    "body": "the why\n\nhttps://claude.ai/code/session_015abc",
    "user": ADA,
    "head": {"ref": "adam/eng2-1540", "sha": "a" * 40},
    "base": {"ref": "main"},
    "state": "closed",
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-03T09:00:00Z",
    "closed_at": "2026-06-03T09:00:00Z",
    "merged_at": "2026-06-03T09:00:00Z",
    "merged_by": GRACE,
    "additions": 120,
    "deletions": 8,
    "changed_files": 3,
    "draft": False,
}
REVIEWS = [
    {
        "id": 1,
        "state": "COMMENTED",
        "body": "",
        "user": BOT,
        "submitted_at": "2026-06-01T10:05:00Z",
    },
    {
        "id": 2,
        "state": "APPROVED",
        "body": "lgtm",
        "user": GRACE,
        "submitted_at": "2026-06-02T15:00:00Z",
    },
    {"id": 3, "state": "APPROVED", "body": None, "user": GRACE, "submitted_at": None},  # pending
]
COMMENTS = [
    {
        "id": 9,
        "body": "nit",
        "path": "x.py",
        "in_reply_to_id": None,
        "pull_request_review_id": 2,
        "user": GRACE,
        "created_at": "2026-06-02T14:58:00Z",
    },
]


def test_one_pr_yields_open_close_reviews_and_comments_dated_by_themselves():
    ds, cut = deliveries_for_pr(REPO, PR, REVIEWS, COMMENTS, until=None)
    assert cut == 0
    assert [d.name for d in ds] == [
        "github.pull_request.opened",
        "github.pull_request_review.submitted",
        "github.pull_request_review_comment.created",
        "github.pull_request_review.submitted",
        "github.pull_request.closed",
    ]  # oldest first; the review with no submitted_at (pending) never lands
    rows = [
        event_to_row("github", {"X-GitHub-Event": d.event, "X-GitHub-Delivery": d.delivery}, d.body)
        for d in ds
    ]
    times = [r["start_time"] for r in rows]
    assert times == [
        datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 1, 10, 5, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 14, 58, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 3, 9, 0, tzinfo=timezone.utc),
    ]
    opened, bot_review, comment, approve, closed = (
        json.loads(r["attributes"])["git"] for r in rows
    )
    assert opened["pr"]["state"] == "open" and opened["pr"]["merged"] is False
    assert "closed_at" not in opened["pr"] and opened["pr"]["additions"] == 120
    assert opened["pr"]["agent_session"] == "https://claude.ai/code/session_015abc"
    assert opened["backfill"] is True and opened["actor"] == "ada"
    assert (
        bot_review["review"]["type"] == "commented" and bot_review["actor"] == "greptile-apps[bot]"
    )
    assert approve["review"] == {
        "id": 2,
        "type": "approved",
        "content": "lgtm",
        "author": "grace",
        "submitted_at": "2026-06-02T15:00:00Z",
    }
    assert comment["comment"] == {"id": 9, "path": "x.py", "author": "grace"}
    assert closed["pr"]["merged"] is True and closed["pr"]["closed_at"] == "2026-06-03T09:00:00Z"
    assert closed["actor"] == "grace"  # merged_by, not the author
    # every event of the PR shares one trace, like the live hook's
    assert len({r["context.trace_id"] for r in rows}) == 1
    # and each has its own span, stable across a second run
    again, _ = deliveries_for_pr(REPO, PR, REVIEWS, COMMENTS, until=None)
    assert [d.body for d in again] == [d.body for d in ds]
    assert len({r["context.span_id"] for r in rows}) == 5


def test_until_cuts_what_the_live_hook_already_delivered():
    until = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    ds, cut = deliveries_for_pr(REPO, PR, REVIEWS, COMMENTS, until=until)
    assert [d.name for d in ds] == [
        "github.pull_request.opened",
        "github.pull_request_review.submitted",
        "github.pull_request_review_comment.created",
    ]
    assert cut == 2  # the approval at exactly `until`, and the close after it


def test_the_signature_is_the_one_the_receiver_checks():
    ds, _ = deliveries_for_pr(REPO, PR, [], [], until=None)
    sig = sign(ds[0].body, "s3cret")
    assert sig.startswith("sha256=")
    assert verify_signature({"X-Hub-Signature-256": sig}, ds[0].body, "s3cret")
    assert not verify_signature({"X-Hub-Signature-256": sig}, ds[0].body, "other")


class _FakeGitHub(GitHub):
    """Answers from dicts; counts calls like the real one."""

    def __init__(self, pulls, details, reviews, comments):
        super().__init__(token=None)
        self._pulls, self._details, self._reviews, self._comments = (
            pulls,
            details,
            reviews,
            comments,
        )

    def pulls(self, repo):
        self.calls += 1
        yield from self._pulls

    def pull(self, repo, number):
        self.calls += 1
        return self._details[number]

    def reviews(self, repo, number):
        self.calls += 1
        return self._reviews.get(number, [])

    def review_comments(self, repo, number):
        self.calls += 1
        return self._comments.get(number, [])


def test_plan_reads_three_calls_per_pr_and_skips_prs_the_hook_saw_open():
    late = {
        **PR,
        "number": 43,
        "created_at": "2026-09-17T17:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "state": "open",
    }
    gh = _FakeGitHub(
        pulls=[
            {"number": 42, "created_at": PR["created_at"]},
            {"number": 43, "created_at": late["created_at"]},
        ],
        details={42: PR, 43: late},
        reviews={42: REVIEWS},
        comments={42: COMMENTS},
    )
    plan = plan_repo(gh, REPO, until=datetime(2026, 9, 17, 16, 38, tzinfo=timezone.utc))
    assert isinstance(plan, Plan)
    assert plan.prs == 1 and plan.skipped_after_until == 1
    assert len(plan.deliveries) == 5
    assert gh.calls == 1 + 3  # the list, then PR + reviews + comments for the one kept


def test_post_speaks_the_hook_shape(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ds, _ = deliveries_for_pr(REPO, PR, [], [], until=None)
    assert post("http://receiver:4318/", ds[0], "s3cret") == 200
    assert seen["url"] == "http://receiver:4318/v1/events/github"
    assert seen["headers"]["x-github-event"] == "pull_request"
    assert seen["headers"]["x-github-delivery"] == "backfill-teraflop-inc-agent-forge-42-opened-"
    assert verify_signature(
        {"X-Hub-Signature-256": seen["headers"]["x-hub-signature-256"]}, seen["body"], "s3cret"
    )


@pytest.mark.parametrize("state", ["APPROVED", "approved"])
def test_review_state_lands_lowercase_like_the_live_hook(state):
    ds, _ = deliveries_for_pr(REPO, PR, [{**REVIEWS[1], "state": state}], [], until=None)
    rv = [d for d in ds if d.event == "pull_request_review"][0]
    assert json.loads(rv.body)["review"]["state"] == "approved"


def test_post_rides_out_a_receiver_restart(monkeypatch):
    calls = {"n": 0}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def flaky(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ds, _ = deliveries_for_pr(REPO, PR, [], [], until=None)
    assert post("http://receiver:4318", ds[0], None) == 200
    assert calls["n"] == 3
