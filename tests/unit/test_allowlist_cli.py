# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.allowlist`` — dry-run by default, like ``cli.reset_user`` and
``cli.geo_resurrect`` (not ``cli.purge_discarded``, the one exception).

The one property worth its own emphasis: ``add`` resolves the email from
Firebase Admin, never from a string the operator typed and never from
``users/{uid}.email`` — see ``tools.allowlist``'s docstring for why that field
is the wrong one everywhere in this PR.

No real Firestore, no real Firebase Admin: both are faked at the same seam
``test_geo_enforce.py`` fakes them at for ``cli.geo_resurrect`` — a
``firestore.AsyncClient`` replacement, called through ``main()`` with
``sys.argv`` patched.
"""

from __future__ import annotations

import sys

import pytest
from test_allowlist import _FakeDB  # the transactional Firestore fake

import cli.allowlist as allowlist_cli


class _UserNotFound(Exception):
    pass


class _FakeFirebaseAuth:
    """Just the two lookups ``_resolve_email`` makes."""

    def __init__(self, records: dict[str, str]):
        # uid -> email, and email -> uid (reverse), for the two lookup paths.
        self._by_uid = dict(records)

    def get_user(self, uid: str):
        if uid not in self._by_uid:
            raise _UserNotFound(uid)
        return type("Record", (), {"email": self._by_uid[uid]})()

    def get_user_by_email(self, email: str):
        for e in self._by_uid.values():
            if e == email:
                return type("Record", (), {"email": e})()
        raise _UserNotFound(email)


def _run(monkeypatch, argv: list[str], db: _FakeDB, auth: _FakeFirebaseAuth) -> None:
    monkeypatch.setattr(sys, "argv", ["allowlist", *argv])
    monkeypatch.setattr(allowlist_cli.firestore, "AsyncClient", lambda: db)
    monkeypatch.setattr(allowlist_cli, "_firebase_auth", lambda: auth)
    allowlist_cli.main()


# ---------------------------------------------------------------------------
# _resolve_email
# ---------------------------------------------------------------------------


def test_resolve_email_by_uid():
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})
    assert (
        allowlist_cli._resolve_email(auth, uid="u1", email=None) == "user@example.com"
    )


def test_resolve_email_by_email():
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})
    assert (
        allowlist_cli._resolve_email(auth, uid=None, email="user@example.com")
        == "user@example.com"
    )


def test_resolve_email_never_trusts_the_callers_own_string():
    """The identifier is only ever used to *look up* the Auth record — the
    value stored is what Firebase Admin says the address is, which can differ
    (case, or simply be wrong) from what was typed."""
    auth = _FakeFirebaseAuth({"u1": "canonical@example.com"})
    assert (
        allowlist_cli._resolve_email(auth, uid="u1", email=None)
        == "canonical@example.com"
    )


def test_resolve_email_raises_on_a_failed_lookup():
    auth = _FakeFirebaseAuth({})
    with pytest.raises(SystemExit):
        allowlist_cli._resolve_email(auth, uid="ghost", email=None)


def test_resolve_email_raises_on_a_record_with_no_address():
    class _NoEmailAuth:
        def get_user(self, uid):
            return type("Record", (), {"email": None})()

    with pytest.raises(SystemExit):
        allowlist_cli._resolve_email(_NoEmailAuth(), uid="u1", email=None)


# ---------------------------------------------------------------------------
# add — dry-run by default
# ---------------------------------------------------------------------------


def test_add_is_dry_run_by_default(monkeypatch, capsys):
    db = _FakeDB()
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})

    _run(monkeypatch, ["add", "--uid", "u1", "--max-users", "5"], db, auth)

    assert db.store == {}
    assert "Would add" in capsys.readouterr().out


def test_add_execute_writes_through_tools_allowlist(monkeypatch, capsys):
    db = _FakeDB()
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})

    _run(
        monkeypatch,
        ["add", "--uid", "u1", "--note", "beta", "--max-users", "5", "--execute"],
        db,
        auth,
    )

    assert db.store["user@example.com"]["note"] == "beta"
    assert "Added" in capsys.readouterr().out


def test_add_execute_refuses_over_the_cap(monkeypatch, capsys):
    db = _FakeDB({"existing@example.com": {"revoked": False}})
    auth = _FakeFirebaseAuth({"u1": "new@example.com"})

    _run(monkeypatch, ["add", "--uid", "u1", "--max-users", "1", "--execute"], db, auth)

    assert "new@example.com" not in db.store
    assert "Did NOT add" in capsys.readouterr().out


def test_add_falls_back_to_the_max_users_env_var(monkeypatch, capsys):
    db = _FakeDB()
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})
    monkeypatch.setenv("MAX_USERS", "3")

    _run(monkeypatch, ["add", "--uid", "u1", "--execute"], db, auth)

    assert "user@example.com" in db.store


def test_add_without_max_users_anywhere_errors_out(monkeypatch):
    db = _FakeDB()
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})
    monkeypatch.delenv("MAX_USERS", raising=False)

    with pytest.raises(SystemExit):
        _run(monkeypatch, ["add", "--uid", "u1", "--execute"], db, auth)


def test_add_requires_exactly_one_identifier(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["allowlist", "add", "--max-users", "5"])
    with pytest.raises(SystemExit):
        allowlist_cli.main()


# ---------------------------------------------------------------------------
# revoke — dry-run by default
# ---------------------------------------------------------------------------


def test_revoke_is_dry_run_by_default(monkeypatch, capsys):
    db = _FakeDB({"user@example.com": {"revoked": False}})
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})

    _run(monkeypatch, ["revoke", "--uid", "u1"], db, auth)

    assert db.store["user@example.com"]["revoked"] is False
    assert "Would revoke" in capsys.readouterr().out


def test_revoke_execute_frees_the_seat(monkeypatch, capsys):
    db = _FakeDB({"user@example.com": {"revoked": False}})
    auth = _FakeFirebaseAuth({"u1": "user@example.com"})

    _run(monkeypatch, ["revoke", "--uid", "u1", "--execute"], db, auth)

    assert db.store["user@example.com"]["revoked"] is True
    assert "Revoked" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_reports_active_and_revoked_counts(monkeypatch, capsys):
    db = _FakeDB(
        {
            "a@example.com": {"added_by": "op", "revoked": False},
            "b@example.com": {"revoked_by": "op", "revoked": True},
        }
    )
    monkeypatch.setattr(sys, "argv", ["allowlist", "list"])
    monkeypatch.setattr(allowlist_cli.firestore, "AsyncClient", lambda: db)

    allowlist_cli.main()

    out = capsys.readouterr().out
    assert "a@example.com" in out and "b@example.com" in out
    assert "1 active, 1 revoked, 2 total" in out


# ---------------------------------------------------------------------------
# A contended add still lands exactly one grant through the CLI path too
# ---------------------------------------------------------------------------


def test_add_execute_is_still_transactional_through_the_cli(monkeypatch, capsys):
    db = _FakeDB({"existing@example.com": {"revoked": False}}, abort_once=True)
    auth = _FakeFirebaseAuth({"u1": "new@example.com"})

    _run(monkeypatch, ["add", "--uid", "u1", "--max-users", "2", "--execute"], db, auth)

    active = sum(1 for d in db.store.values() if not d.get("revoked"))
    assert active == 2
