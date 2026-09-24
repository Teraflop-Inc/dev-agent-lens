"""Replay a repository's pull-request history into the store as signed deliveries.

The webhooks (ENG2-1622) start the day the hook is created. The review questions
(ENG2-1538, 1539) need the months before that: when each PR opened, who reviewed it,
how long that took, how big the diff was. GitHub's REST API still has all of it, so this
reads it and posts it to the receiver's ``/v1/events/github`` door in the same shape the
hook would have used, signed with the same secret. Nothing new lands on the write side:
``event_to_row`` dates each span by the payload's own timestamp, and the span id is the
hash of the body, so running this twice lands each event once.

Four events per pull request, at most: ``pull_request.opened`` at ``created_at``,
``pull_request.closed`` at ``closed_at`` (with ``merged``), one
``pull_request_review.submitted`` per review at its ``submitted_at``, and one
``pull_request_review_comment.created`` per inline comment. Every payload carries
``"backfill": true`` so a query can tell replayed history from live deliveries.

``--until`` is the moment the live hook took over. Events at or after it are skipped so a
PR opened after the hook exists is not landed twice (the hook's body and this one differ
in fields we do not type, so the ids would differ). Read the hook's creation time from
the repo's hook page and pass it.

Budget: three API calls per pull request (the PR, its reviews, its inline comments) plus
one list page per hundred. Six repos with ~1,200 PRs is ~3,700 calls against a 5,000 per
hour token limit; the client sleeps on a 403 rate-limit reply until the window resets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)

API = "https://api.github.com"
USER_AGENT = "dev-agent-lens github-backfill"


def github_token() -> str | None:
    """GITHUB_TOKEN or GH_TOKEN from the environment, else what `gh auth token` says."""
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tok = out.stdout.strip()
    return tok or None


class GitHub:
    """The three reads the backfill needs, paginated, rate-limit aware."""

    def __init__(self, token: str | None, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self.token = token
        self.calls = 0
        self._sleep = sleep

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, dict[str, str]]:
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(6):
            req = urllib.request.Request(url, headers=headers)
            self.calls += 1
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.loads(resp.read())
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    log.debug(
                        "[backfill:api] %s in %.0fms remaining=%s",
                        path,
                        (time.perf_counter() - t0) * 1000,
                        hdrs.get("x-ratelimit-remaining"),
                    )
                    return body, hdrs
            except urllib.error.HTTPError as e:
                hdrs = {k.lower(): v for k, v in e.headers.items()}
                if e.code in (403, 429) and hdrs.get("x-ratelimit-remaining") == "0":
                    reset = int(hdrs.get("x-ratelimit-reset") or 0)
                    wait = max(5.0, reset - time.time() + 2)
                    log.warning("[backfill:api] rate limited on %s; sleeping %.0fs", path, wait)
                    self._sleep(wait)
                    continue
                if e.code >= 500 or e.code == 429:
                    wait = 2.0 * (attempt + 1)
                    log.warning("[backfill:api] %s -> %d; retry in %.0fs", path, e.code, wait)
                    self._sleep(wait)
                    continue
                raise
        raise RuntimeError(f"gave up on {path} after 6 attempts")

    def _pages(self, path: str, params: dict[str, Any]) -> Iterator[Any]:
        page = 1
        while True:
            body, _ = self._get(path, {**params, "per_page": 100, "page": page})
            if not body:
                return
            yield from body
            if len(body) < 100:
                return
            page += 1

    def pulls(self, repo: str) -> Iterator[dict[str, Any]]:
        yield from self._pages(
            f"/repos/{repo}/pulls", {"state": "all", "sort": "created", "direction": "asc"}
        )

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        body, _ = self._get(f"/repos/{repo}/pulls/{number}")
        return body

    def reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self._pages(f"/repos/{repo}/pulls/{number}/reviews", {}))

    def review_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self._pages(f"/repos/{repo}/pulls/{number}/comments", {}))


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _user(u: Any) -> dict[str, Any] | None:
    return {"login": u.get("login"), "type": u.get("type")} if isinstance(u, dict) else None


def _pr_payload(pr: dict[str, Any], *, at_open: bool) -> dict[str, Any]:
    """The pull_request object as the hook would have sent it, trimmed to what lands.

    ``at_open`` is the snapshot when the PR was opened: open, unmerged, no close time.
    Everything else is the PR's final state, which is what a review or close event
    carried at its own time (near enough: the diff size is the last one, not the one at
    review time; GitHub keeps no history of that).
    """
    out = {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "body": pr.get("body"),
        "user": _user(pr.get("user")),
        "head": {
            "ref": (pr.get("head") or {}).get("ref"),
            "sha": (pr.get("head") or {}).get("sha"),
        },
        "base": {"ref": (pr.get("base") or {}).get("ref")},
        "created_at": pr.get("created_at"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "draft": pr.get("draft"),
    }
    if at_open:
        out.update({"state": "open", "merged": False, "updated_at": pr.get("created_at")})
    else:
        out.update(
            {
                "state": pr.get("state"),
                "merged": bool(pr.get("merged_at")),
                "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"),
                "updated_at": pr.get("updated_at"),
            }
        )
    return out


@dataclass
class Delivery:
    event: str
    delivery: str
    body: bytes
    at: datetime
    name: str


@dataclass
class Plan:
    repo: str
    deliveries: list[Delivery] = field(default_factory=list)
    prs: int = 0
    skipped_after_until: int = 0


def deliveries_for_pr(
    repo: str,
    pr: dict[str, Any],
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    *,
    until: datetime | None,
) -> tuple[list[Delivery], int]:
    """Every delivery one pull request yields, oldest first, and how many `until` cut."""
    number = int(pr["number"])
    repository = {"full_name": repo, "name": repo.split("/")[-1]}
    out: list[Delivery] = []
    cut = 0

    def emit(
        event: str, kind: str, ident: Any, at: datetime | None, payload: dict[str, Any]
    ) -> None:
        nonlocal cut
        if at is None:
            return
        if until is not None and at >= until:
            cut += 1
            return
        payload = {**payload, "repository": repository, "backfill": True}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        out.append(
            Delivery(
                event=event,
                delivery=f"backfill-{repo.replace('/', '-')}-{number}-{kind}-{ident}",
                body=body,
                at=at,
                name=f"github.{event}.{payload.get('action')}",
            )
        )

    emit(
        "pull_request",
        "opened",
        "",
        _parse(pr.get("created_at")),
        {
            "action": "opened",
            "number": number,
            "pull_request": _pr_payload(pr, at_open=True),
            "sender": _user(pr.get("user")),
        },
    )
    if pr.get("closed_at"):
        emit(
            "pull_request",
            "closed",
            "",
            _parse(pr.get("closed_at")),
            {
                "action": "closed",
                "number": number,
                "pull_request": _pr_payload(pr, at_open=False),
                # GitHub names whoever pressed merge or close; the API's merged_by is the
                # nearest thing, else the author.
                "sender": _user(pr.get("merged_by")) or _user(pr.get("user")),
            },
        )
    final = _pr_payload(pr, at_open=False)
    for rv in reviews:
        state = str(rv.get("state") or "").lower()  # the hook sends lowercase; the API shouts
        emit(
            "pull_request_review",
            "review",
            rv.get("id"),
            _parse(rv.get("submitted_at")),
            {
                "action": "submitted",
                "number": number,
                "pull_request": final,
                "review": {
                    "id": rv.get("id"),
                    "state": state,
                    "body": rv.get("body"),
                    "user": _user(rv.get("user")),
                    "submitted_at": rv.get("submitted_at"),
                    "commit_id": rv.get("commit_id"),
                },
                "sender": _user(rv.get("user")),
            },
        )
    for c in comments:
        emit(
            "pull_request_review_comment",
            "comment",
            c.get("id"),
            _parse(c.get("created_at")),
            {
                "action": "created",
                "number": number,
                "pull_request": final,
                "comment": {
                    "id": c.get("id"),
                    "body": c.get("body"),
                    "path": c.get("path"),
                    "in_reply_to_id": c.get("in_reply_to_id"),
                    "pull_request_review_id": c.get("pull_request_review_id"),
                    "user": _user(c.get("user")),
                    "created_at": c.get("created_at"),
                },
                "sender": _user(c.get("user")),
            },
        )
    out.sort(key=lambda d: d.at)
    return out, cut


def plan_repo(
    gh: GitHub,
    repo: str,
    *,
    until: datetime | None,
    since: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> Plan:
    """Read one repo's pull requests and build every delivery, oldest PR first."""
    plan = Plan(repo=repo)
    t0 = time.perf_counter()
    for head in gh.pulls(repo):
        number = int(head["number"])
        created = _parse(head.get("created_at"))
        if since is not None and created is not None and created < since:
            continue
        if until is not None and created is not None and created >= until:
            # Opened after the hook existed: the hook has everything about it.
            plan.skipped_after_until += 1
            continue
        pr = gh.pull(repo, number)  # the list shape lacks additions/deletions/changed_files
        reviews = gh.reviews(repo, number)
        comments = gh.review_comments(repo, number)
        ds, cut = deliveries_for_pr(repo, pr, reviews, comments, until=until)
        plan.deliveries.extend(ds)
        plan.skipped_after_until += cut
        plan.prs += 1
        if progress and plan.prs % 25 == 0:
            progress(
                f"{repo}: {plan.prs} PRs read, {len(plan.deliveries)} deliveries, "
                f"{gh.calls} calls, {time.perf_counter() - t0:.0f}s"
            )
    log.info(
        "[backfill] %s: %d PRs -> %d deliveries (%d cut by --until) in %.1fs, %d API calls",
        repo,
        plan.prs,
        len(plan.deliveries),
        plan.skipped_after_until,
        time.perf_counter() - t0,
        gh.calls,
    )
    return plan


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(receiver: str, d: Delivery, secret: str | None, *, timeout: float = 30.0) -> int:
    """POST one delivery to ``<receiver>/v1/events/github``; the HTTP status."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Event": d.event,
        "X-GitHub-Delivery": d.delivery,
    }
    if secret:
        headers["X-Hub-Signature-256"] = sign(d.body, secret)
    req = urllib.request.Request(
        receiver.rstrip("/") + "/v1/events/github", data=d.body, headers=headers, method="POST"
    )
    # A receiver mid-restart refuses the connection for a few seconds; the run is
    # idempotent but an hour of API reads is not worth losing to that. Five tries, then
    # the error is the caller's.
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as e:
            return int(e.code)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 4:
                raise
            wait = 3.0 * (attempt + 1)
            log.warning("[backfill] %s: %s; retry in %.0fs", d.delivery, e, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def post_all(
    receiver: str,
    deliveries: list[Delivery],
    secret: str | None,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[int, int]:
    """POST every delivery in order; a count per HTTP status. Stops on the first 401."""
    by_status: dict[int, int] = {}
    t0 = time.perf_counter()
    for i, d in enumerate(deliveries, 1):
        code = post(receiver, d, secret)
        by_status[code] = by_status.get(code, 0) + 1
        if code == 401:
            log.error("[backfill] receiver refused the signature on %s; stopping", d.delivery)
            break
        if code != 200:
            log.warning("[backfill] %s -> %d", d.delivery, code)
        if progress and i % 200 == 0:
            progress(f"posted {i}/{len(deliveries)} in {time.perf_counter() - t0:.0f}s")
    log.info(
        "[backfill] posted %d deliveries in %.1fs: %s",
        sum(by_status.values()),
        time.perf_counter() - t0,
        by_status,
    )
    return by_status
