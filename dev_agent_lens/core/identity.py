"""
Identity Resolution

Turns the opaque `account_uuid` on a span into a human.

Trace data names nobody: the only email present is the `oauth@claude-code.ai`
placeholder, `user_api_key_alias` is unpopulated on every span, and
`requester_ip_address` is empty. `identity.yaml` is therefore the sole bridge
between a trace and a person, and it is populated by self-report — never by
inference (see ENG2-1393).

Two properties callers must respect:

  * The mapping is MANY-TO-ONE. A human typically owns an interactive login
    plus a CLAUDE_CODE_OAUTH_TOKEN account used inside sandbox VMs.
  * An account_uuid identifies the account whose token authed the agent, not
    the human at the keyboard.

Unverified accounts resolve to ``None``, not to a guess. Callers should render
that as an explicit "unattributed" bucket rather than dropping the rows —
silently discarding unattributed spans would understate volume, and silently
guessing would misattribute one engineer's work to another.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

IDENTITY_FILE = Path(__file__).parent / "identity.yaml"


@dataclass(frozen=True)
class Person:
    """A human, and the account_uuids known to belong to them."""

    email: str
    name: str
    accounts: tuple[str, ...]
    verified_accounts: tuple[str, ...]

    @property
    def is_verified(self) -> bool:
        """True if at least one of this person's accounts was self-reported."""
        return bool(self.verified_accounts)


@dataclass(frozen=True)
class IdentityMap:
    """Resolved account_uuid -> Person index, plus the accounts nobody claims."""

    people: tuple[Person, ...]
    _by_account: dict[str, Person]
    unclaimed: tuple[str, ...]

    def resolve(self, account_uuid: str | None) -> Person | None:
        """Resolve an account_uuid to a Person, or None if unknown/unclaimed.

        Returns None rather than guessing. An unattributed span is a fact; a
        mislabelled one is a bug that reads as a fact.
        """
        if not account_uuid:
            return None
        return self._by_account.get(account_uuid.strip().lower())

    def label(self, account_uuid: str | None) -> str:
        """Human-readable label for a uuid, safe for display.

        Unknown accounts render as a short uuid prefix rather than a name, so
        an unattributed row is visibly unattributed instead of quietly blank.
        """
        person = self.resolve(account_uuid)
        if person:
            return person.email
        if not account_uuid:
            return "(sandbox/unattributed)"
        return f"(unclaimed:{account_uuid[:8]})"

    @property
    def is_complete(self) -> bool:
        """True when every account seen in the corpus maps to a person."""
        return not self.unclaimed


def _load(path: Path) -> IdentityMap:
    if not path.exists():
        logger.warning("[identity] no identity file at %s — nothing will resolve", path)
        return IdentityMap(people=(), _by_account={}, unclaimed=())

    raw = yaml.safe_load(path.read_text()) or {}

    people: list[Person] = []
    by_account: dict[str, Person] = {}

    for entry in raw.get("people") or []:
        accounts = entry.get("accounts") or []
        uuids = tuple(str(a["uuid"]).strip().lower() for a in accounts)
        verified = tuple(
            str(a["uuid"]).strip().lower() for a in accounts if a.get("verified")
        )
        unverified = [
            a for a in accounts if not a.get("verified")
        ]
        if unverified:
            # Loud on purpose: an unverified uuid under a named person is
            # exactly the mislabelling this module exists to prevent. The
            # evidence is included so a reader can weigh it rather than having
            # to go dig for why we believe this.
            for a in unverified:
                logger.warning(
                    "[identity] %s <- %s is UNVERIFIED (evidence: %s) — "
                    "inferred, not self-reported",
                    entry.get("email"),
                    str(a["uuid"])[:8],
                    a.get("evidence") or "none",
                )

        person = Person(
            email=entry["email"],
            name=entry.get("name") or entry["email"],
            accounts=uuids,
            verified_accounts=verified,
        )
        people.append(person)
        for uuid in uuids:
            if uuid in by_account:
                logger.error(
                    "[identity] account %s claimed by both %s and %s — "
                    "refusing to resolve it to either",
                    uuid[:8],
                    by_account[uuid].email,
                    person.email,
                )
                continue
            by_account[uuid] = person

    unclaimed = tuple(
        str(u["uuid"]).strip().lower() for u in (raw.get("unclaimed") or [])
    )

    logger.info(
        "[identity] loaded %d people / %d accounts (%d verified), %d unclaimed",
        len(people),
        len(by_account),
        sum(len(p.verified_accounts) for p in people),
        len(unclaimed),
    )
    if unclaimed:
        logger.info(
            "[identity] unclaimed accounts will resolve to None: %s",
            ", ".join(u[:8] for u in unclaimed),
        )

    return IdentityMap(people=tuple(people), _by_account=by_account, unclaimed=unclaimed)


@lru_cache(maxsize=1)
def load_identity_map() -> IdentityMap:
    """Load (and cache) the identity map from identity.yaml."""
    return _load(IDENTITY_FILE)


def resolve_account(account_uuid: str | None) -> Person | None:
    """Convenience wrapper: resolve one account_uuid to a Person, or None."""
    return load_identity_map().resolve(account_uuid)


def label_account(account_uuid: str | None) -> str:
    """Convenience wrapper: display label for one account_uuid."""
    return load_identity_map().label(account_uuid)
