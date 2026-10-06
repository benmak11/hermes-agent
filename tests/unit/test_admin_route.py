# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``/admin/accounts`` and ``/admin/me`` through the real gate, against fake
Firestore and a fake, paginated Firebase Auth."""

from __future__ import annotations

import pytest
import structlog
from auth_fakes import FakeAuth, auth_user
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firestore_fakes import FakeQueryDB

import api.deps as deps
import api.routes.admin as admin
from tools import allowlist

ADMIN = "admin-uid"
AUTH = {"Authorization": "Bearer tok"}


def _patch_decoded(monkeypatch, decoded: dict) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    def verify(token):
        calls.append(token)
        return dict(decoded)

    monkeypatch.setattr(fb_auth_module, "verify_id_token", verify)
    return calls


def _db() -> FakeQueryDB:
    return FakeQueryDB(
        {
            "allowlist": {
                "seated@x.com": {
                    "email": "seated@x.com",
                    "added_at": "2026-08-01T00:00:00+00:00",
                    "added_by": "op",
                    "note": None,
                    "revoked": False,
                },
                "nobody@x.com": {
                    "email": "nobody@x.com",
                    "added_at": "2026-08-01T00:00:00+00:00",
                    "added_by": "op",
                    "note": None,
                    "revoked": False,
                },
            },
            "waitlist": {},
            "users": {"u1": {"full_name": "Seated"}},
        }
    )


AUTH_USERS = FakeAuth(
    [auth_user("u1", "seated@x.com")], [auth_user("u2", "stranger@x.com")]
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ADMIN_UIDS", ADMIN)
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER", raising=False)
    monkeypatch.setattr(deps, "_allowlist_cache", {})
    db = _db()
    monkeypatch.setattr(admin, "_client", lambda: db)
    monkeypatch.setattr(admin, "firebase_auth", lambda: AUTH_USERS)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app)


def _as_admin(monkeypatch):
    return _patch_decoded(
        monkeypatch,
        {"uid": ADMIN, "email": "admin@example.com", "email_verified": True},
    )


def test_accounts_is_no_store(monkeypatch, client):
    _as_admin(monkeypatch)
    resp = client.get("/admin/accounts", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def test_accounts_returns_the_roster(monkeypatch, client):
    _as_admin(monkeypatch)
    body = client.get("/admin/accounts", headers=AUTH).json()
    assert body["enforced"] is False
    assert {r["key"]: r["status"] for r in body["accounts"]} == {
        "uid:u1": "active",
        "uid:u2": "active",
        "email:nobody@x.com": "seat_without_login",
    }
    assert body["summary"]["total"] == 3
    assert body["summary"]["needs_attention"] == 2
    assert body["summary"]["seats_active"] == 2


def test_accounts_logs_one_audit_line_with_counts_and_no_email(monkeypatch, client):
    _as_admin(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        assert client.get("/admin/accounts", headers=AUTH).status_code == 200
    viewed = [e for e in logs if e["event"] == "admin.accounts_viewed"]
    assert len(viewed) == 1
    assert viewed[0]["rows"] == 3
    assert viewed[0]["needs_attention"] == 2
    assert "@" not in repr(viewed[0])


def test_an_auth_failure_is_a_503_naming_the_source_not_a_partial_list(
    monkeypatch, client
):
    _as_admin(monkeypatch)
    monkeypatch.setattr(
        admin, "firebase_auth", lambda: FakeAuth(fail=RuntimeError("auth down"))
    )
    resp = client.get("/admin/accounts", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "could not read firebase_auth"}
    assert "accounts" not in resp.json()
    assert resp.headers["cache-control"] == "no-store"


def test_accounts_is_hidden_from_a_non_admin(monkeypatch, client):
    _patch_decoded(
        monkeypatch, {"uid": "u1", "email": "seated@x.com", "email_verified": True}
    )
    assert client.get("/admin/accounts", headers=AUTH).status_code == 404


def test_me_is_true_for_the_admin_and_verifies_once(monkeypatch, client):
    calls = _as_admin(monkeypatch)
    resp = client.get("/admin/me", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"admin": True}
    assert calls == ["tok"]


def test_me_is_false_for_an_allowlisted_non_admin(monkeypatch, client):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    monkeypatch.setattr(deps, "_client", lambda: object())

    async def allowed(db, email):
        return True

    monkeypatch.setattr(allowlist, "is_allowed", allowed)
    _patch_decoded(
        monkeypatch, {"uid": "u1", "email": "seated@x.com", "email_verified": True}
    )
    resp = client.get("/admin/me", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"admin": False}


def test_me_is_false_under_the_dev_bypass_even_as_the_admin_uid(monkeypatch, client):
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv("AUTH_DEV_USER", ADMIN)
    resp = client.get("/admin/me")
    assert resp.status_code == 200
    assert resp.json() == {"admin": False}


def test_me_is_false_in_dev_mode_even_with_a_real_admin_token(monkeypatch, client):
    """The bypass identity is unverified anyway; this is the case where only
    the dev-mode refusal says no."""
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    _as_admin(monkeypatch)
    resp = client.get("/admin/me", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"admin": False}


def test_me_keeps_the_normal_401_for_a_missing_token(client):
    assert client.get("/admin/me").status_code == 401


def test_a_failure_log_names_the_source_and_type_but_never_the_message(
    monkeypatch, client
):
    """Upstream error text can carry a user's email; the log keeps only what
    failed and how."""
    _as_admin(monkeypatch)
    monkeypatch.setattr(
        admin,
        "firebase_auth",
        lambda: FakeAuth(fail=RuntimeError("no user record for ada@example.com")),
    )
    with structlog.testing.capture_logs() as logs:
        assert client.get("/admin/accounts", headers=AUTH).status_code == 503
    [failed] = [e for e in logs if e["event"] == "admin.accounts_failed"]
    assert failed["source"] == "firebase_auth"
    assert failed["error_type"] == "RuntimeError"
    assert "@" not in str(failed)
