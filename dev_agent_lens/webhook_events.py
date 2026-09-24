"""Git-host webhooks (Forgejo first) as spans in the store (ENG2-1622).

The OTLP receiver already runs; this is a second door on the same server. A git host
POSTs one JSON document per event to ``/v1/events/<producer>``, and it lands as one raw
span under ``source=<producer>``: name ``<producer>.<event>[.<action>]``, the parts a
query wants typed out under ``git.*``, the actor under ``user.id`` so ``identity.yaml``
can name them, and the payload itself kept (trimmed) under ``event.payload`` for
whatever the next question needs. Push, pull request and review are the three kinds
the first-loop questions need (ENG2-1538, 1539); every other event lands the same way
with fewer typed fields.

Signature: when ``DAL_EVENTS_SECRET`` is set the receiver requires every delivery to carry
an HMAC-SHA256 of the body under ``X-Hub-Signature-256`` (GitHub, ``sha256=<hex>``) or
``X-Forgejo-Signature`` / ``X-Gitea-Signature`` (bare hex); an unsigned or wrong delivery
gets 401 and lands nothing. Unset, the door is open, which is only fine on the tailnet.
GitHub reaches it through the public relay on sf-tailscale-router, so the secret is set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

PAYLOAD_LIMIT = 64_000  # bytes of payload kept on the span; the rest is on the git host

EVENT_HEADERS = ("X-Forgejo-Event", "X-Gitea-Event", "X-GitHub-Event", "X-Gogs-Event")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
# A producer is a path segment we chose when creating the hook, nothing else.
PRODUCER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
SIGNATURE_HEADERS = (
    "X-Hub-Signature-256",
    "X-Forgejo-Signature",
    "X-Gitea-Signature",
    "X-Gogs-Signature",
)
DELIVERY_HEADERS = (
    "X-Forgejo-Delivery",
    "X-Gitea-Delivery",
    "X-GitHub-Delivery",
    "X-Gogs-Delivery",
)


def _first(headers: Any, names: tuple[str, ...]) -> str | None:
    for n in names:
        v = headers.get(n) if headers is not None else None
        if v:
            return str(v)
    return None


def verify_signature(headers: Any, body: bytes, secret: str) -> bool:
    """True when the delivery carries a valid HMAC-SHA256 of ``body`` under ``secret``.

    GitHub prefixes the hex with ``sha256=``; Forgejo, Gitea and Gogs send bare hex. A
    missing header is a failure, not a pass. Constant-time compare.
    """
    sig = _first(headers, SIGNATURE_HEADERS)
    if not sig:
        return False
    sig = sig.strip()
    if sig.lower().startswith("sha256="):
        sig = sig[len("sha256=") :]
    # Exactly 64 hex characters or it is not a signature. compare_digest raises on
    # non-ASCII input, and a public door must never turn a header into a traceback.
    if not _HEX64.fullmatch(sig):
        return False
    expect = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect.encode(), sig.lower().encode())


def _login(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    for k in ("login", "username", "name"):
        v = obj.get(k)
        if v:
            return str(v)
    return None


_SESSION_LINK = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")


def _agent_session(body: Any) -> str | None:
    if not isinstance(body, str):
        return None
    m = _SESSION_LINK.search(body)
    return m.group(0) if m else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def event_to_row(
    producer: str, headers: Any, body: bytes, *, now: datetime | None = None
) -> dict[str, Any]:
    """One webhook delivery -> one row in the store's raw shape.

    ``producer`` is the path segment (``forgejo``), ``headers`` the request headers, ``body``
    the JSON. Raises ``ValueError`` on a body that is not a JSON object; the caller answers
    400. Never raises on a payload missing fields: the span lands with what was there.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON: {e}") from e
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    event = _first(headers, EVENT_HEADERS) or str(payload.get("event") or "unknown")
    delivery = _first(headers, DELIVERY_HEADERS)
    action = payload.get("action")
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    sender = payload.get("sender")
    actor = _login(sender) or _login(payload.get("pusher"))
    number = payload.get("number") or pr.get("number")
    ref = payload.get("ref")
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    commits = payload.get("commits") if isinstance(payload.get("commits"), list) else []

    comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}

    # The span's time is when the event happened on the host. A push is dated when it was
    # delivered: it carries its commits' own times (Forgejo lists at most the last ten),
    # and a branch of old commits pushed today is still today's push; the first spike
    # push landed dated five days back for that reason. Commit times stay on the span
    # under git.last_commit_at. A pull request, a review and a comment each carry the
    # moment they happened, and that is the time a queue question needs: a backfilled
    # review from June must land in June, not on the day it was replayed (ENG2-1538).
    when = now or datetime.now(timezone.utc)
    own = None
    if review:
        own = _parse_time(review.get("submitted_at"))
    elif comment:
        own = _parse_time(comment.get("created_at"))
    if own is None and pr and not commits:
        if action == "opened":
            own = _parse_time(pr.get("created_at"))
        elif action == "closed":
            own = _parse_time(pr.get("closed_at") or pr.get("merged_at"))
        own = own or _parse_time(pr.get("updated_at"))
    when = own or when
    last_commit_at = _parse_time(commits[-1].get("timestamp")) if commits else None

    # GitHub writes the owner as the org spells it (Teraflop-Inc) and the API as the caller
    # spelt it; the trace id is seeded by the repo name, so one casing or a PR's opening
    # and its reviews land in two traces. Lowercase, the way GitHub itself compares.
    repo_name = str(repo.get("full_name") or repo.get("name") or "").lower()
    # One trace per pull request or, for a push, per branch: the reviews and comments of
    # a PR sit on its opening in the same trace, the way a session's calls do.
    trace_seed = (
        f"{producer}:{repo_name}:pr:{number}" if number else f"{producer}:{repo_name}:ref:{ref}"
    )
    trace_id = hashlib.sha256(trace_seed.encode()).hexdigest()[:32]
    # The span id comes from the signed bytes, not the delivery id: a git host's redelivery
    # carries the same body and lands once, and a captured signed body resent under a fresh
    # delivery id lands nowhere new. The delivery id stays on the span for the audit trail.
    span_id = hashlib.sha256(f"{producer}:{hashlib.sha256(body).hexdigest()}".encode()).hexdigest()[
        :16
    ]

    raw = body if len(body) <= PAYLOAD_LIMIT else body[:PAYLOAD_LIMIT]
    git: dict[str, Any] = {
        "producer": producer,
        "event": event,
        "action": action,
        "delivery": delivery,
        "repo": repo_name or None,
        "actor": actor,
        "ref": ref,
        "before": payload.get("before"),
        "after": payload.get("after"),
        "commits": len(commits) if event == "push" else None,
        "last_commit_at": last_commit_at.isoformat() if last_commit_at else None,
        "pr": {
            "number": number,
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged": pr.get("merged"),
            "head": head.get("ref"),
            "head_sha": head.get("sha"),
            "base": base.get("ref"),
            "author": _login(pr.get("user")),
            "created_at": pr.get("created_at"),
            "merged_at": pr.get("merged_at"),
            "closed_at": pr.get("closed_at"),
            # Diff size, the denominator of every review-depth question. GitHub sends
            # these on the pull_request object; Forgejo does not, so they stay absent.
            "additions": _int(pr.get("additions")),
            "deletions": _int(pr.get("deletions")),
            "changed_files": _int(pr.get("changed_files")),
            # The session link a Claude-written PR body ends with. Absent means a person
            # wrote it, or wrote it with an agent that leaves no mark; the review recipes
            # split agent-authored from human-authored on this.
            "agent_session": _agent_session(pr.get("body")),
        }
        if pr
        else None,
        # Forgejo says type/content; GitHub says state/body. One shape on the span.
        "review": {
            "id": review.get("id"),
            "type": review.get("type") or review.get("state"),
            "content": review.get("content") or review.get("body"),
            "author": _login(review.get("user")),
            "submitted_at": review.get("submitted_at"),
        }
        if review
        else None,
        "comment": {
            "id": comment.get("id"),
            "in_reply_to": comment.get("in_reply_to_id"),
            "path": comment.get("path"),
            "author": _login(comment.get("user")),
        }
        if comment
        else None,
        # A replayed delivery (dal github-backfill) says so; the query can tell history
        # from what the hook delivered live.
        "backfill": True if payload.get("backfill") is True else None,
    }
    git = {
        k: ({kk: vv for kk, vv in v.items() if vv is not None} if isinstance(v, dict) else v)
        for k, v in git.items()
        if v is not None
    }
    attributes: dict[str, Any] = {
        "openinference": {"span": {"kind": "CHAIN"}},
        "git": git,
        "event": {"payload": raw.decode("utf-8", "replace"), "bytes": len(body)},
    }
    if actor:
        attributes["user"] = {"id": actor}
    name = f"{producer}.{event}" + (f".{action}" if action else "")
    return {
        "context.span_id": span_id,
        "context.trace_id": trace_id,
        "parent_id": None,
        "name": name,
        "span_kind": "CHAIN",
        "start_time": when,
        "end_time": when,
        "status_code": "OK",
        "status_message": "",
        "attributes": json.dumps(attributes, default=str),
        "events": "[]",
        "cumulative_error_count": 0,
        "cumulative_llm_token_count_prompt": None,
        "cumulative_llm_token_count_completion": None,
        "llm_token_count_prompt": None,
        "llm_token_count_completion": None,
    }
