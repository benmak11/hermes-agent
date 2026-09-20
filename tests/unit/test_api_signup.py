# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``POST /account/signup`` — the one route that answers a stranger.

Three properties carry the weight:

1. **A stranger writes nothing under ``users/``.** They get one
   ``waitlist/{email}`` doc, keyed through the same normalisation the
   allowlist uses, so the operator's later ``allowlist add`` lands on the
   matching id. Nothing else — no profile stub, no tombstone, nothing a
   background loop could fan out over.
2. **An allowed account is stamped once, ever.** ``signup_source`` records the
   *first* entry point and ``signed_up_at`` never drifts; a repeat sign-in is
   a read and no write. A doc tombstoned by ``POST /account/delete`` is never
   recreated by a token that outlives the account.
3. **It fails closed where ``api.deps`` does** — a token with no email claim
   under enforcement is a 403 and touches nothing.

The Firestore fake is ``test_account_delete``'s path-keyed one (it supports any
collection and records every op); ``test_allowlist``'s fake asserts the
collection name and so cannot host both ``allowlist/`` and ``waitlist/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_account_delete import _FakeDB

import api.routes.account as account
from api.deps import Identity, verify_identity
from tools import allowlist

SEAT = f"{allowlist.COLLECTION}/user@example.com"
WAITLIST = f"{allowlist.WAITLIST_COLLECTION}/user@example.com"


async def _explode_async(*args, **kwargs):
    raise AssertionError("must not have checked the allowlist")


@pytest.fixture
def api(monkeypatch):
    """The account router on a path-keyed fake, enforcement on, identity
    overridable per test (default: an allowlisted mixed-case address)."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    db = _FakeDB({})
    monkeypatch.setattr(account, "_client", lambda: db)
    app = FastAPI()
    app.include_router(account.router)
    state = SimpleNamespace(
        client=TestClient(app), db=db, identity=Identity("u1", "User@Example.com", True)
    )
    app.dependency_overrides[verify_identity] = lambda: state.identity
    return state


def _signup(api, source: str | None = "hero"):
    return api.client.post("/account/signup", json={"source": source})


def _users_paths(db: _FakeDB) -> list[str]:
    return [p for p in db.docs if p.startswith("users/")]


def _users_ops(db: _FakeDB) -> list[tuple]:
    return [op for op in db.ops if str(op[1]).startswith("users/")]


# ---------------------------------------------------------------------------
# Allowed
# ---------------------------------------------------------------------------


def test_an_allowed_signup_stamps_the_user_doc_and_writes_no_waitlist(api):
    api.db.docs[SEAT] = {"revoked": False}

    resp = _signup(api, "hero")

    assert resp.status_code == 200
    assert resp.json() == {"allowed": True}
    doc = api.db.docs["users/u1"]
    assert doc["signup_source"] == "hero"
    assert doc["signed_up_at"]
    assert not [p for p in api.db.docs if p.startswith("waitlist/")]


def test_an_allowed_signup_writes_the_user_doc_only_once(api):
    """``signup_source`` is the *first* entry point; a repeat sign-in is one
    read and no write under ``users/`` (the waitlist delete is unconditional
    and cheap, and is not a user-doc write)."""
    api.db.docs[SEAT] = {"revoked": False}

    assert _signup(api, "hero").json() == {"allowed": True}
    stamped = dict(api.db.docs["users/u1"])
    user_sets_after_first = [
        op for op in api.db.ops if op[0] == "set" and op[1].startswith("users/")
    ]

    assert _signup(api, "nav").json() == {"allowed": True}

    assert api.db.docs["users/u1"] == stamped
    assert api.db.docs["users/u1"]["signup_source"] == "hero"
    user_sets = [
        op for op in api.db.ops if op[0] == "set" and op[1].startswith("users/")
    ]
    assert user_sets == user_sets_after_first  # no second ("set", "users/u1")


def test_a_granted_waitlister_is_removed_from_the_waitlist_on_admission(api):
    """The waitlist is always exactly "people not yet in": the doc goes on the
    grantee's first allowed sign-in, not when the operator runs ``add``."""
    api.db.docs[SEAT] = {"revoked": False}
    api.db.docs[WAITLIST] = {"uid": "u1", "source": "hero"}

    resp = _signup(api, "closing")

    assert resp.status_code == 200
    assert WAITLIST not in api.db.docs
    assert api.db.docs["users/u1"]["signup_source"] == "closing"


def test_a_stale_waitlist_doc_is_cleared_even_for_an_already_stamped_user(api):
    """revoke -> the user signs in and is waitlisted -> re-grant -> sign in.
    The user doc was stamped long ago, so the first-time branch is skipped;
    the waitlist doc must still go, or ``cli.allowlist waitlist`` lists
    someone who is in."""
    api.db.docs[SEAT] = {"revoked": False}
    api.db.docs["users/u1"] = {
        "signup_source": "hero",
        "signed_up_at": "2026-01-01T00:00:00+00:00",
    }
    api.db.docs[WAITLIST] = {"uid": "u1", "source": "footer"}

    resp = _signup(api, "nav")

    assert resp.status_code == 200
    assert WAITLIST not in api.db.docs
    assert api.db.docs["users/u1"]["signup_source"] == "hero"  # not re-stamped


def test_a_tombstoned_doc_is_not_recreated(api):
    """A Firebase ID token stays verifiable for up to an hour after
    ``POST /account/delete`` closed the account — the same window
    ``tools.account.delete`` documents. It must not resurrect the doc."""
    api.db.docs[SEAT] = {"revoked": False}
    api.db.docs["users/u1"] = {"deleted_at": "2026-09-20T12:00:00+00:00"}

    resp = _signup(api, "hero")

    assert resp.status_code == 200
    assert resp.json() == {"allowed": True}
    assert ("set", "users/u1") not in api.db.ops
    assert "signup_source" not in api.db.docs["users/u1"]


def test_enforcement_off_admits_everyone_without_reading_the_allowlist(
    api, monkeypatch
):
    """Mirrors ``_check_allowlist``: unenforced means everyone is in, and the
    dev bypass (which carries no email) keeps working locally."""
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)
    api.identity = Identity("me", None, False)

    resp = _signup(api, "hero")

    assert resp.json() == {"allowed": True}
    assert api.db.docs["users/me"]["signup_source"] == "hero"


# ---------------------------------------------------------------------------
# Not allowed
# ---------------------------------------------------------------------------


def test_a_stranger_is_waitlisted_under_the_normalised_key_and_nothing_under_users(
    api,
):
    """The key is ``tools.allowlist._key`` — the same strip+casefold the
    allowlist uses — so the operator's later ``add`` matches. And *nothing*
    under ``users/``: not a stub, not a read that turned into a write."""
    api.identity = Identity("u1", "  User@Example.COM ", False)

    resp = _signup(api, "footer")

    assert resp.status_code == 200
    assert resp.json() == {"allowed": False}
    doc = api.db.docs[WAITLIST]
    assert doc["uid"] == "u1"
    assert doc["email"] == "user@example.com"
    assert doc["source"] == "footer"
    assert doc["email_verified"] is False
    assert doc["first_seen"] == doc["last_seen"]
    assert set(doc) == {
        "uid",
        "email",
        "source",
        "email_verified",
        "first_seen",
        "last_seen",
    }
    assert [p for p in api.db.docs if p.startswith("waitlist/")] == [WAITLIST]
    assert _users_paths(api.db) == []
    assert _users_ops(api.db) == []


def test_a_repeat_stranger_keeps_first_seen_and_source(api, monkeypatch):
    """A second sign-in moves ``last_seen`` and nothing else — the place in
    line and the entry point are both first-write-wins."""
    t1 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)
    clock = {"now": t1}
    monkeypatch.setattr(
        allowlist, "datetime", SimpleNamespace(now=lambda tz=None: clock["now"])
    )

    assert _signup(api, "hero").json() == {"allowed": False}
    clock["now"] = t2
    assert _signup(api, "footer").json() == {"allowed": False}

    doc = api.db.docs[WAITLIST]
    assert doc["first_seen"] == t1.isoformat()
    assert doc["last_seen"] == t2.isoformat()
    assert doc["source"] == "hero"
    assert [p for p in api.db.docs if p.startswith("waitlist/")] == [WAITLIST]
    assert _users_ops(api.db) == []


def test_a_token_with_no_email_is_refused_closed(api):
    """Same situation, same answer as ``api.deps._check_allowlist``: with
    enforcement on there has to be an email to check. Nothing is written."""
    api.identity = Identity("u1", None, False)

    resp = _signup(api, "hero")

    assert resp.status_code == 403
    assert "no email claim" in resp.json()["detail"]
    assert api.db.ops == []
    assert api.db.docs == {}


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allowed", [True, False])
def test_an_unknown_source_is_stored_as_null(api, allowed):
    """The client names its own source; anything outside the four marketing
    CTAs is not trusted and is stored as null, on both branches."""
    if allowed:
        api.db.docs[SEAT] = {"revoked": False}

    resp = _signup(api, "evil")

    assert resp.json() == {"allowed": allowed}
    path = "users/u1" if allowed else WAITLIST
    field = "signup_source" if allowed else "source"
    assert api.db.docs[path][field] is None
