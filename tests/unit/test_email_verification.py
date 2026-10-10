# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Verified email for email/password accounts (``REQUIRE_VERIFIED_EMAIL``).

An email/password token whose address is unverified is refused on every app
route with 403 ``{"reason": "email_unverified"}``, whether or not the
allowlist is enforced, and ``POST /account/signup`` answers ``verify_email``
without touching Firestore. Google tokens, the dev bypass and the admin gate
are unchanged; ``REQUIRE_VERIFIED_EMAIL=0`` restores the old behaviour.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_account_delete import _FakeDB

import api.deps as deps
import api.routes.account as account
from api.deps import Identity, verify_identity
from tools import allowlist

AUTH = {"Authorization": "Bearer tok"}
UNVERIFIED = {"detail": {"reason": "email_unverified"}}


def _token(provider: str | None, verified: bool | None, **extra) -> dict:
    decoded: dict = {"uid": "u1", "email": "user@example.com", **extra}
    if verified is not None:
        decoded["email_verified"] = verified
    if provider is not None:
        decoded["firebase"] = {"sign_in_provider": provider}
    return decoded


def _patch_decoded(monkeypatch, decoded: dict) -> None:
    """Stand in for Firebase token verification, never the real Admin SDK."""
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    monkeypatch.setattr(fb_auth_module, "verify_id_token", lambda token: dict(decoded))


async def _explode_async(*args, **kwargs):
    raise AssertionError("must not have checked the allowlist")


def _explode(*args, **kwargs):
    raise AssertionError("must not have reached Firestore")


async def _allowed(db, email):
    return True


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("REQUIRE_VERIFIED_EMAIL", raising=False)
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER", raising=False)
    monkeypatch.setattr(deps, "_allowlist_cache", {})


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(user_id: str = Depends(deps.verify_user)):
        return {"user_id": user_id}

    @app.get("/stream")
    def stream(user_id: str = Depends(deps.verify_user_query)):
        return {"user_id": user_id}

    @app.get("/secret")
    def secret(ident: Annotated[Identity, Depends(deps.verify_admin)]):
        return {"uid": ident.uid}

    return TestClient(app)


def _enforce_allowlist(monkeypatch, *, check=_allowed) -> None:
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    monkeypatch.setattr(deps, "_client", lambda: object())
    monkeypatch.setattr(allowlist, "is_allowed", check)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_the_provider_is_read_off_the_firebase_claim(monkeypatch):
    _patch_decoded(monkeypatch, _token("password", False))
    ident = asyncio.run(deps.verify_identity(authorization="Bearer tok"))
    assert ident == Identity("u1", "user@example.com", False, "password")
    assert ident.needs_verification


@pytest.mark.parametrize(
    "identity, needs",
    [
        (Identity("u1", "user@example.com", False, "password"), True),
        (Identity("u1", "user@example.com", True, "password"), False),
        (Identity("u1", "user@example.com", False, "google.com"), False),
        (Identity("u1", None, False, None), False),
    ],
)
def test_needs_verification_is_only_unverified_password_accounts(identity, needs):
    assert identity.needs_verification is needs


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", " off ", "no"])
def test_the_kill_switch_values(monkeypatch, raw):
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", raw)
    assert deps.verification_required() is False


@pytest.mark.parametrize("raw", [None, "", "1", "true", "yes"])
def test_verification_defaults_on(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("REQUIRE_VERIFIED_EMAIL", raising=False)
    else:
        monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", raw)
    assert deps.verification_required() is True


# ---------------------------------------------------------------------------
# verify_user
# ---------------------------------------------------------------------------


def test_an_unverified_password_account_is_refused_with_the_allowlist_off(
    monkeypatch, client
):
    """The allowlist lifts at launch; verification must not lift with it."""
    _patch_decoded(monkeypatch, _token("password", False))
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    resp = client.get("/whoami", headers=AUTH)

    assert resp.status_code == 403
    assert resp.json() == UNVERIFIED


def test_an_unverified_password_account_is_refused_with_the_allowlist_on(
    monkeypatch, client
):
    """Refused before the allowlist is read, even for a seated address."""
    _enforce_allowlist(monkeypatch, check=_explode_async)
    _patch_decoded(monkeypatch, _token("password", False))

    resp = client.get("/whoami", headers=AUTH)

    assert resp.status_code == 403
    assert resp.json() == UNVERIFIED


def test_a_password_token_with_no_verified_claim_is_refused(monkeypatch, client):
    _patch_decoded(monkeypatch, _token("password", None))
    assert client.get("/whoami", headers=AUTH).json() == UNVERIFIED


def test_the_allowlist_refusal_is_distinguishable(monkeypatch, client):
    """The web app picks the screen off ``detail.reason``; the allowlist's
    403 must not carry it."""

    async def not_allowed(db, email):
        return False

    _enforce_allowlist(monkeypatch, check=not_allowed)
    _patch_decoded(monkeypatch, _token("password", True))

    resp = client.get("/whoami", headers=AUTH)

    assert resp.status_code == 403
    assert isinstance(resp.json()["detail"], str)


def test_the_sse_dependency_refuses_too(monkeypatch, client):
    _patch_decoded(monkeypatch, _token("password", False))
    resp = client.get("/stream?token=tok")
    assert resp.status_code == 403
    assert resp.json() == UNVERIFIED


@pytest.mark.parametrize("allowlist_on", [False, True])
def test_a_verified_password_account_passes(monkeypatch, client, allowlist_on):
    if allowlist_on:
        _enforce_allowlist(monkeypatch)
    _patch_decoded(monkeypatch, _token("password", True))

    resp = client.get("/whoami", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "u1"}


@pytest.mark.parametrize("verified", [None, False, True])
@pytest.mark.parametrize("allowlist_on", [False, True])
def test_google_tokens_pass_whatever_their_verified_claim(
    monkeypatch, client, verified, allowlist_on
):
    if allowlist_on:
        _enforce_allowlist(monkeypatch)
    _patch_decoded(monkeypatch, _token("google.com", verified))

    resp = client.get("/whoami", headers=AUTH)

    assert resp.status_code == 200


def test_a_token_with_no_firebase_claim_passes(monkeypatch, client):
    _patch_decoded(monkeypatch, _token(None, False))
    assert client.get("/whoami", headers=AUTH).status_code == 200


def test_the_dev_bypass_passes(monkeypatch, client):
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv("AUTH_DEV_USER", "demo-uid")
    monkeypatch.setattr(deps, "_client", _explode)

    resp = client.get("/whoami")

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "demo-uid"}


@pytest.mark.parametrize("raw", ["0", "false"])
def test_the_kill_switch_restores_the_old_behaviour(monkeypatch, client, raw):
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", raw)
    _patch_decoded(monkeypatch, _token("password", False))

    resp = client.get("/whoami", headers=AUTH)

    assert resp.status_code == 200


def test_the_check_raises_an_http_403():
    with pytest.raises(HTTPException) as exc:
        deps._check_verified(Identity("u1", "user@example.com", False, "password"))
    assert exc.value.status_code == 403
    assert exc.value.detail == {"reason": "email_unverified"}


# ---------------------------------------------------------------------------
# The admin gate
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_UIDS", "u1")


def test_a_google_admin_is_unaffected(monkeypatch, client, admin_env):
    _patch_decoded(monkeypatch, _token("google.com", True))
    assert client.get("/secret", headers=AUTH).status_code == 200


def test_a_verified_password_admin_is_unaffected(monkeypatch, client, admin_env):
    _patch_decoded(monkeypatch, _token("password", True))
    assert client.get("/secret", headers=AUTH).status_code == 200


def test_an_unverified_password_admin_stays_hidden_not_403(
    monkeypatch, client, admin_env
):
    """The admin gate already demands a verified address and answers 404 to
    everyone else; the new 403 must not leak the route's existence."""
    _patch_decoded(monkeypatch, _token("password", False))
    resp = client.get("/secret", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------------------
# POST /account/signup
# ---------------------------------------------------------------------------


@pytest.fixture
def signup_api(monkeypatch):
    db = _FakeDB({})
    monkeypatch.setattr(account, "_client", lambda: db)
    app = FastAPI()
    app.include_router(account.router)
    state = SimpleNamespace(
        client=TestClient(app),
        db=db,
        identity=Identity("u1", "user@example.com", False, "password"),
    )
    app.dependency_overrides[verify_identity] = lambda: state.identity
    return state


def _signup(api):
    return api.client.post("/account/signup", json={"source": "hero"})


@pytest.mark.parametrize("allowlist_on", [False, True])
def test_signup_of_an_unverified_password_account_writes_nothing(
    monkeypatch, signup_api, allowlist_on
):
    """No seat, no waitlist place, no ``signup_source`` — not even a read."""
    if allowlist_on:
        monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    monkeypatch.setattr(account, "_client", _explode)
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)
    monkeypatch.setattr(allowlist, "record_waitlist", _explode_async)

    resp = _signup(signup_api)

    assert resp.status_code == 200
    assert resp.json() == {"allowed": False, "reason": "verify_email"}
    assert signup_api.db.ops == []
    assert signup_api.db.docs == {}


def test_signup_of_a_verified_password_account_is_admitted(signup_api):
    signup_api.identity = Identity("u1", "user@example.com", True, "password")

    resp = _signup(signup_api)

    assert resp.json() == {"allowed": True}
    assert signup_api.db.docs["users/u1"]["signup_source"] == "hero"


def test_signup_of_an_unverified_google_account_is_admitted(signup_api):
    signup_api.identity = Identity("u1", "user@example.com", False, "google.com")
    assert _signup(signup_api).json() == {"allowed": True}


def test_signup_with_the_kill_switch_off_waitlists_as_before(monkeypatch, signup_api):
    monkeypatch.setenv("REQUIRE_VERIFIED_EMAIL", "0")
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")

    resp = _signup(signup_api)

    assert resp.json() == {"allowed": False}
    assert f"{allowlist.WAITLIST_COLLECTION}/user@example.com" in signup_api.db.docs
