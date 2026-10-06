# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``api.deps.verify_admin``: the admin gets in, everyone else gets the same
404 an unknown path gets — never a 401 or 403 that would reveal the route."""

from __future__ import annotations

from typing import Annotated

import pytest
import structlog
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import api.deps as deps
from tools import allowlist

ADMIN = "admin-uid"


def _patch_decoded(monkeypatch, decoded: dict | None) -> list[str]:
    """Stand in for Firebase token verification; ``None`` makes every token
    invalid. Returns the tokens it was asked to verify."""
    calls: list[str] = []
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    def verify(token):
        calls.append(token)
        if decoded is None:
            raise ValueError("bad token")
        return dict(decoded)

    monkeypatch.setattr(fb_auth_module, "verify_id_token", verify)
    return calls


def _admin_token(**overrides) -> dict:
    return {
        "uid": ADMIN,
        "email": "admin@example.com",
        "email_verified": True,
        **overrides,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ADMIN_UIDS", ADMIN)
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER", raising=False)
    monkeypatch.setattr(deps, "_allowlist_cache", {})


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/secret")
    def secret(ident: Annotated[deps.Identity, Depends(deps.verify_admin)]):
        return {"uid": ident.uid}

    return TestClient(app)


AUTH = {"Authorization": "Bearer tok"}


def _assert_hidden(client: TestClient, resp) -> None:
    """Indistinguishable from a path that does not exist."""
    unknown = client.get("/no-such-route")
    assert resp.status_code == 404
    assert resp.json() == unknown.json() == {"detail": "Not Found"}


def test_the_admin_gets_in(monkeypatch, client):
    _patch_decoded(monkeypatch, _admin_token())
    resp = client.get("/secret", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"uid": ADMIN}


def test_a_non_admin_uid_is_hidden(monkeypatch, client):
    _patch_decoded(monkeypatch, _admin_token(uid="someone-else"))
    _assert_hidden(client, client.get("/secret", headers=AUTH))


def test_the_admin_email_on_another_uid_is_not_admin(monkeypatch, client):
    """The match is on uid: an account that now holds the admin's address is
    still somebody else."""
    monkeypatch.setenv("ADMIN_UIDS", "admin@example.com")
    _patch_decoded(monkeypatch, _admin_token(uid="someone-else"))
    _assert_hidden(client, client.get("/secret", headers=AUTH))


def test_the_dev_bypass_as_the_admin_uid_is_hidden_without_verifying(
    monkeypatch, client
):
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv("AUTH_DEV_USER", ADMIN)
    calls = _patch_decoded(monkeypatch, _admin_token())
    with structlog.testing.capture_logs() as logs:
        _assert_hidden(client, client.get("/secret", headers=AUTH))
    assert calls == []
    # Refused for being dev mode, not merely because the bypass identity
    # happens to be unverified.
    assert [e["reason"] for e in logs if e["event"] == "admin.denied"] == ["dev_mode"]


def test_dev_mode_with_a_real_admin_token_is_hidden(monkeypatch, client):
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    _patch_decoded(monkeypatch, _admin_token())
    _assert_hidden(client, client.get("/secret", headers=AUTH))


def test_an_unverified_admin_email_is_hidden(monkeypatch, client):
    _patch_decoded(monkeypatch, _admin_token(email_verified=False))
    _assert_hidden(client, client.get("/secret", headers=AUTH))


def test_a_missing_token_is_404_not_401(monkeypatch, client):
    _patch_decoded(monkeypatch, _admin_token())
    _assert_hidden(client, client.get("/secret"))


def test_an_invalid_token_is_hidden(monkeypatch, client):
    _patch_decoded(monkeypatch, None)
    _assert_hidden(client, client.get("/secret", headers=AUTH))


@pytest.mark.parametrize("value", [None, "", " , "])
def test_no_configured_admin_means_nobody_is_admin(monkeypatch, client, value):
    if value is None:
        monkeypatch.delenv("ADMIN_UIDS", raising=False)
    else:
        monkeypatch.setenv("ADMIN_UIDS", value)
    _patch_decoded(monkeypatch, _admin_token())
    assert deps.admin_uids() == frozenset()
    _assert_hidden(client, client.get("/secret", headers=AUTH))


def test_enforcement_on_and_the_admin_not_allowlisted_is_hidden(monkeypatch, client):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    monkeypatch.setattr(deps, "_client", lambda: object())

    async def not_allowed(db, email):
        return False

    monkeypatch.setattr(allowlist, "is_allowed", not_allowed)
    _patch_decoded(monkeypatch, _admin_token())
    _assert_hidden(client, client.get("/secret", headers=AUTH))


def test_enforcement_on_and_the_admin_allowlisted_gets_in(monkeypatch, client):
    """Positive control for the test above."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    monkeypatch.setattr(deps, "_client", lambda: object())

    async def allowed(db, email):
        return True

    monkeypatch.setattr(allowlist, "is_allowed", allowed)
    _patch_decoded(monkeypatch, _admin_token())
    assert client.get("/secret", headers=AUTH).status_code == 200


def test_admin_uids_strips_and_drops_empties(monkeypatch):
    monkeypatch.setenv("ADMIN_UIDS", " a , ,b")
    assert deps.admin_uids() == frozenset({"a", "b"})


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"decoded": _admin_token(uid="x")}, "not_admin"),
        ({"decoded": _admin_token(email_verified=False)}, "email_unverified"),
        ({"decoded": None}, "invalid_token"),
        ({"decoded": _admin_token(), "no_token": True}, "missing_token"),
        ({"decoded": _admin_token(), "dev": True}, "dev_mode"),
    ],
)
def test_a_refusal_logs_admin_denied_with_a_reason(monkeypatch, client, setup, reason):
    if setup.get("dev"):
        monkeypatch.setenv("AUTH_DEV_MODE", "1")
    _patch_decoded(monkeypatch, setup["decoded"])
    headers = {} if setup.get("no_token") else AUTH
    with structlog.testing.capture_logs() as logs:
        assert client.get("/secret", headers=headers).status_code == 404
    denied = [e for e in logs if e["event"] == "admin.denied"]
    assert [e["reason"] for e in denied] == [reason]
