"""Tests for account_uuid -> human identity resolution.

Uses synthetic fixtures only — no real person/account data (the real
identity.yaml is gitignored PII). The invariants here exist because getting them
wrong misattributes one engineer's work to another (ENG2-1393).
"""

from __future__ import annotations

import textwrap

from dev_agent_lens.core.identity import IDENTITY_FILE, _load, load_identity_map

# Fake UUIDs — structurally valid, deliberately not anyone's real account.
U1 = "00000000-0000-0000-0000-000000000001"
U2 = "00000000-0000-0000-0000-000000000002"
U3 = "00000000-0000-0000-0000-000000000003"


def _write(tmp_path, body: str):
    path = tmp_path / "identity.yaml"
    path.write_text(textwrap.dedent(body))
    return path


class TestResolution:
    def test_verified_account_resolves_to_its_person(self, tmp_path):
        m = _load(
            _write(
                tmp_path,
                f"""
                people:
                  - email: a@example.com
                    name: A
                    accounts:
                      - uuid: {U1}
                        verified: true
                unclaimed: []
                """,
            )
        )
        assert m.resolve(U1).email == "a@example.com"
        assert U1 in m.resolve(U1).verified_accounts

    def test_many_to_one_two_accounts_same_person(self, tmp_path):
        m = _load(
            _write(
                tmp_path,
                f"""
                people:
                  - email: b@example.com
                    name: B
                    accounts:
                      - uuid: {U2}
                        verified: true
                      - uuid: {U3}
                        verified: false
                        evidence: os_user=b
                unclaimed: []
                """,
            )
        )
        assert m.resolve(U2) is m.resolve(U3)

    def test_unknown_account_resolves_to_none_not_a_guess(self, tmp_path):
        m = _load(_write(tmp_path, "people: []\nunclaimed: []\n"))
        assert m.resolve("deadbeef-0000-0000-0000-000000000000") is None

    def test_missing_account_resolves_to_none(self, tmp_path):
        m = _load(_write(tmp_path, "people: []\nunclaimed: []\n"))
        assert m.resolve(None) is None
        assert m.resolve("") is None

    def test_resolution_is_case_and_whitespace_insensitive(self, tmp_path):
        m = _load(
            _write(
                tmp_path,
                f"""
                people:
                  - email: a@example.com
                    name: A
                    accounts:
                      - uuid: {U1}
                        verified: true
                unclaimed: []
                """,
            )
        )
        assert m.resolve(f"  {U1.upper()} ").email == "a@example.com"

    def test_missing_file_yields_empty_map_not_a_crash(self, tmp_path):
        m = _load(tmp_path / "nope.yaml")
        assert m.people == ()
        assert m.resolve(U1) is None


class TestLabels:
    def test_sandbox_spans_are_labelled_unattributed(self, tmp_path):
        m = _load(_write(tmp_path, "people: []\nunclaimed: []\n"))
        assert m.label(None) == "(sandbox/unattributed)"

    def test_unknown_account_labelled_as_unclaimed_not_blank(self, tmp_path):
        m = _load(_write(tmp_path, "people: []\nunclaimed: []\n"))
        assert m.label("deadbeef-1111") == "(unclaimed:deadbeef)"

    def test_known_account_labelled_with_email(self, tmp_path):
        m = _load(
            _write(
                tmp_path,
                f"""
                people:
                  - email: a@example.com
                    name: A
                    accounts:
                      - uuid: {U1}
                        verified: true
                unclaimed: []
                """,
            )
        )
        assert m.label(U1) == "a@example.com"


class TestGuardrails:
    def test_unverified_account_warns_with_its_evidence(self, tmp_path, caplog):
        _load(
            _write(
                tmp_path,
                f"""
                people:
                  - email: a@example.com
                    name: A
                    accounts:
                      - uuid: {U1}
                        verified: false
                        evidence: os_user=someuser
                unclaimed: []
                """,
            )
        )
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "UNVERIFIED" in msgs
        assert "os_user=someuser" in msgs

    def test_an_account_claimed_by_two_people_resolves_to_neither(
        self, tmp_path, caplog
    ):
        # Two people claiming one uuid is a data error. Guessing between them is
        # exactly the misattribution we refuse to make: BOTH claims are dropped
        # (first-claim-wins would let an unverified guess listed earlier beat a
        # verified self-report), and the collision is logged loudly.
        m = _load(
            _write(
                tmp_path,
                f"""
                people:
                  - email: first@example.com
                    name: First
                    accounts:
                      - uuid: {U1}
                        verified: true
                  - email: second@example.com
                    name: Second
                    accounts:
                      - uuid: {U1}
                        verified: true
                unclaimed: []
                """,
            )
        )
        assert m.resolve(U1) is None
        assert m.label(U1) == f"(unclaimed:{U1[:8]})"
        assert any("claimed by both" in r.message for r in caplog.records)

    def test_unclaimed_accounts_make_the_map_incomplete(self, tmp_path):
        m = _load(
            _write(
                tmp_path,
                f"""
                people: []
                unclaimed:
                  - uuid: {U1}
                """,
            )
        )
        assert not m.is_complete
        assert m.resolve(U1) is None


class TestShippedMap:
    """The real identity.yaml is gitignored (PII), so it's absent in CI and on a
    fresh clone. When it IS present (a real local checkout), it must at least load
    and resolve — asserted structurally, never against specific real values."""

    def test_shipped_map_loads_if_present(self):
        m = load_identity_map()
        if not IDENTITY_FILE.exists():
            assert m.people == ()  # graceful empty map when absent
            return
        # Present locally: every listed account must resolve and label non-blank.
        for person in m.people:
            for uuid in person.accounts:
                assert m.resolve(uuid) is person
                assert m.label(uuid).strip()
