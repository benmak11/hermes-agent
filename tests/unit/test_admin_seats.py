# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``POST /admin/seats`` and ``POST /admin/seats/revoke`` through the real
admin gate and the real ``tools.allowlist`` transaction, against the shared
Firestore and Firebase Auth fakes."""

from __future__ import annotations

import pytest
import structlog
from auth_fakes import FakeAuth, auth_user
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firestore_fakes import FakeStoreDB

import api.deps as deps
import api.routes.admin as admin
from tools import allowlist

ADMIN = "admin-uid"
ADMIN_EMAIL = "admin@example.com"
AUTH = {"Authorization": "Bearer tok"}


def _seat(email: str, *, revoked: bool = False, added_by: str = "op") -> dict:
    return {
        "email": email,
        "added_at": "2026-08-01T00:00:00+00:00",
        "added_by": added_by,
        "note": None,
        "revoked": revoked,
    }


AUTH_USERS = FakeAuth(
    [
        auth_user(ADMIN, ADMIN_EMAIL),
        auth_user("u-wait", "waiting@x.com"),
        auth_user("u-seated", "seated@x.com"),
    ],
    # Page two, so a lookup that reads only the first page misses these.
    [auth_user("u-gone", "Revoked@X.com"), auth_user("u-new", "new@x.com")],
)


@pytest.fixture
def db() -> FakeStoreDB:
    return FakeStoreDB(
        {
            "allowlist": {
                ADMIN_EMAIL: _seat(ADMIN_EMAIL),
                "seated@x.com": _seat("seated@x.com"),
                "revoked@x.com": _seat("revoked@x.com", revoked=True),
                "orphan@x.com": _seat("orphan@x.com"),
            },
            "waitlist": {
                "waiting@x.com": {
                    "uid": "u-wait",
                    "email": "waiting@x.com",
                    "first_seen": "2026-09-20T12:00:00+00:00",
                }
            },
            "users": {},
        }
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, db):
    monkeypatch.setenv("ADMIN_UIDS", ADMIN)
    monkeypatch.setenv("MAX_USERS", "25")
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER", raising=False)
    monkeypatch.setattr(deps, "_allowlist_cache", {})
    monkeypatch.setattr(admin, "_client", lambda: db)
    monkeypatch.setattr(admin, "firebase_auth", lambda: AUTH_USERS)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app)


def _as(monkeypatch, uid: str, email: str) -> None:
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    monkeypatch.setattr(
        fb_auth_module,
        "verify_id_token",
        lambda token: {"uid": uid, "email": email, "email_verified": True},
    )


@pytest.fixture
def as_admin(monkeypatch):
    _as(monkeypatch, ADMIN, ADMIN_EMAIL)


def _grant(client, email="waiting@x.com", **extra):
    return client.post("/admin/seats", json={"email": email, **extra}, headers=AUTH)


def _revoke(client, email="seated@x.com", confirm=None):
    return client.post(
        "/admin/seats/revoke",
        json={"email": email, "confirm": email if confirm is None else confirm},
        headers=AUTH,
    )


def _events(logs, name):
    return [e for e in logs if e["event"] == name]


# ------------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    "call",
    [
        lambda c: _grant(c),
        lambda c: _revoke(c),
    ],
    ids=["grant", "revoke"],
)
def test_a_non_admin_gets_the_plain_404_and_nothing_is_written(
    monkeypatch, client, db, call
):
    _as(monkeypatch, "u-seated", "seated@x.com")
    resp = call(client)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert db.writes == []


@pytest.mark.parametrize("call", [_grant, _revoke], ids=["grant", "revoke"])
def test_dev_mode_is_a_404_even_for_the_admin_token(monkeypatch, client, db, call):
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv("AUTH_DEV_USER", ADMIN)
    _as(monkeypatch, ADMIN, ADMIN_EMAIL)
    assert call(client).status_code == 404
    assert db.writes == []


# ---------------------------------------------------------------------- grant


def test_grant_records_the_admin_and_the_note(as_admin, client, db):
    resp = _grant(client, note="  beta cohort  ")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {"granted": True, "email": "waiting@x.com", "seat_cap": 25}
    seat = db.data["allowlist"]["waiting@x.com"]
    assert seat["added_by"] == f"admin:{ADMIN}"
    assert seat["note"] == "beta cohort"
    assert seat["revoked"] is False
    # Through the transaction: the seat count was read inside it.
    assert "allowlist" in db.transactional_reads
    assert db.transactions[-1].commits == 1


def test_grant_writes_the_auth_records_own_email_not_the_typed_one(
    as_admin, client, db
):
    """``Revoked@X.com`` is on page two of Auth; the typed casing differs."""
    resp = _grant(client, email="  revoked@x.COM ")
    assert resp.status_code == 200
    assert resp.json()["email"] == "Revoked@X.com"
    assert db.data["allowlist"]["revoked@x.com"]["revoked"] is False
    assert db.data["allowlist"]["revoked@x.com"]["added_by"] == f"admin:{ADMIN}"


def test_grant_leaves_the_waitlist_doc_as_the_cli_does(as_admin, client, db):
    assert _grant(client).status_code == 200
    assert "waiting@x.com" in db.data["waitlist"]
    assert db.deletes == []


def test_grant_of_an_already_active_seat_is_an_idempotent_success(as_admin, client, db):
    before = dict(db.data["allowlist"]["seated@x.com"])
    resp = _grant(client, email="seated@x.com")
    assert resp.status_code == 200
    assert resp.json()["granted"] is True
    assert db.data["allowlist"]["seated@x.com"] == before
    assert db.writes == []


def test_grant_at_the_cap_is_a_409_naming_the_cap(monkeypatch, as_admin, client, db):
    # admin, seated, orphan are active: three of three.
    monkeypatch.setenv("MAX_USERS", "3")
    with structlog.testing.capture_logs() as logs:
        resp = _grant(client)
    assert resp.status_code == 409
    assert resp.headers["cache-control"] == "no-store"
    assert "seat cap reached" in resp.json()["detail"]
    assert "3" in resp.json()["detail"]
    assert "waiting@x.com" not in db.data["allowlist"]
    [refused] = _events(logs, "admin.seat_grant_refused")
    assert refused["reason"] == "cap_reached"
    assert refused["target_uid"] == "u-wait"


@pytest.mark.parametrize("raw", [None, "", "  ", "lots", "2.5", "0", "-4"])
def test_grant_with_no_usable_cap_is_refused_never_unlimited(
    monkeypatch, as_admin, client, db, raw
):
    if raw is None:
        monkeypatch.delenv("MAX_USERS", raising=False)
    else:
        monkeypatch.setenv("MAX_USERS", raw)
    with structlog.testing.capture_logs() as logs:
        resp = _grant(client)
    assert resp.status_code == 503
    assert resp.json()["detail"].startswith("seat cap not configured")
    assert db.writes == []
    assert [e["reason"] for e in _events(logs, "admin.seat_grant_refused")] == [
        "cap_not_configured"
    ]


def test_grant_without_an_auth_account_is_a_422(as_admin, client, db):
    with structlog.testing.capture_logs() as logs:
        resp = _grant(client, email="typo@x.com")
    assert resp.status_code == 422
    assert "sign in once" in resp.json()["detail"]
    assert "typo@x.com" not in db.data["allowlist"]
    assert db.writes == []
    assert _events(logs, "admin.seat_grant_refused")[0]["reason"] == "no_auth_account"


def test_grant_of_a_malformed_address_is_a_422(as_admin, client, db):
    resp = _grant(client, email="not-an-email")
    assert resp.status_code == 422
    assert db.writes == []


def test_grant_when_auth_is_down_is_a_503_and_writes_nothing(
    monkeypatch, as_admin, client, db
):
    monkeypatch.setattr(
        admin, "firebase_auth", lambda: FakeAuth(fail=RuntimeError("for x@x.com"))
    )
    with structlog.testing.capture_logs() as logs:
        resp = _grant(client)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "could not read firebase_auth"
    assert db.writes == []
    [refused] = _events(logs, "admin.seat_grant_refused")
    assert refused["error_type"] == "RuntimeError"
    assert "@" not in repr(refused)


# --------------------------------------------------------------------- revoke


def test_revoke_records_the_admin_and_says_five_minutes(as_admin, client, db):
    resp = _revoke(client)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["revoked"] is True
    assert body["already_revoked"] is False
    assert "within 5 minutes" in body["message"]
    seat = db.data["allowlist"]["seated@x.com"]
    assert seat["revoked"] is True
    assert seat["revoked_by"] == f"admin:{ADMIN}"
    assert seat["revoked_at"]


def test_the_lag_in_the_message_is_the_allowlist_cache(as_admin, client):
    assert deps._ALLOWLIST_CHECK_EVERY.total_seconds() == 300
    assert admin._REVOKE_LAG_MINUTES == 5


@pytest.mark.parametrize("confirm", ["", "seated@x.co", "other@x.com", " "])
def test_revoke_with_a_mismatched_confirm_is_a_400_and_revokes_nothing(
    as_admin, client, db, confirm
):
    with structlog.testing.capture_logs() as logs:
        resp = _revoke(client, confirm=confirm)
    assert resp.status_code == 400
    assert db.data["allowlist"]["seated@x.com"]["revoked"] is False
    assert db.writes == []
    assert _events(logs, "admin.seat_revoke_refused")[0]["reason"] == (
        "confirm_mismatch"
    )


def test_revoke_confirm_tolerates_case_and_whitespace_like_account_delete(
    as_admin, client, db
):
    resp = _revoke(client, confirm="  SEATED@x.com ")
    assert resp.status_code == 200
    assert db.data["allowlist"]["seated@x.com"]["revoked"] is True


@pytest.mark.parametrize("email", [ADMIN_EMAIL, "  ADMIN@example.com "])
def test_the_admin_cannot_revoke_their_own_seat(as_admin, client, db, email):
    with structlog.testing.capture_logs() as logs:
        resp = _revoke(client, email=email)
    assert resp.status_code == 409
    assert "your own seat" in resp.json()["detail"]
    assert db.data["allowlist"][ADMIN_EMAIL]["revoked"] is False
    assert db.writes == []
    [refused] = _events(logs, "admin.seat_revoke_refused")
    assert refused["reason"] == "self_revoke"
    assert refused["target_uid"] == ADMIN


def test_self_revoke_is_refused_by_uid_when_the_token_email_differs(
    monkeypatch, client, db
):
    """The token's email claim and the Auth record can disagree; the uid
    still identifies the admin's own login."""
    _as(monkeypatch, ADMIN, "alias@example.com")
    resp = _revoke(client, email=ADMIN_EMAIL)
    assert resp.status_code == 409
    assert db.data["allowlist"][ADMIN_EMAIL]["revoked"] is False


def test_revoke_of_a_seat_with_no_login_works(as_admin, client, db):
    with structlog.testing.capture_logs() as logs:
        resp = _revoke(client, email="orphan@x.com")
    assert resp.status_code == 200
    assert db.data["allowlist"]["orphan@x.com"]["revoked"] is True
    assert _events(logs, "admin.seat_revoked")[0]["target_uid"] is None


def test_revoke_of_an_already_revoked_seat_succeeds_without_restamping(
    as_admin, client, db
):
    resp = _revoke(client, email="revoked@x.com")
    assert resp.status_code == 200
    assert resp.json()["already_revoked"] is True
    assert "within 5 minutes" in resp.json()["message"]
    assert db.writes == []


def test_revoke_of_an_email_with_no_seat_is_a_409(as_admin, client, db):
    resp = _revoke(client, email="new@x.com")
    assert resp.status_code == 409
    assert "new@x.com" not in db.data["allowlist"]
    assert db.writes == []


def test_a_revoke_bites_once_the_cache_expires(monkeypatch, as_admin, client, db):
    """End to end against ``is_allowed``: the doc the route writes is one the
    gate reads as not allowed."""
    assert _revoke(client).status_code == 200
    import asyncio

    assert asyncio.run(allowlist.is_allowed(db, "seated@x.com")) is False


# ---------------------------------------------------------------------- audit


def test_audit_lines_carry_uids_and_never_an_email(monkeypatch, as_admin, client):
    with structlog.testing.capture_logs() as logs:
        assert _grant(client, note="for ada@example.com").status_code == 200
        assert _revoke(client).status_code == 200
        assert _revoke(client, email=ADMIN_EMAIL).status_code == 409
        assert _revoke(client, confirm="nope").status_code == 400
        assert _grant(client, email="typo@x.com").status_code == 422
        monkeypatch.setenv("MAX_USERS", "1")
        assert _grant(client, email="new@x.com").status_code == 409
        monkeypatch.delenv("MAX_USERS")
        assert _grant(client).status_code == 503

    audit = [e for e in logs if e["event"].startswith("admin.seat")]
    assert [e["event"] for e in audit] == [
        "admin.seat_granted",
        "admin.seat_revoked",
        "admin.seat_revoke_refused",
        "admin.seat_revoke_refused",
        "admin.seat_grant_refused",
        "admin.seat_grant_refused",
        "admin.seat_grant_refused",
    ]
    assert audit[0]["target_uid"] == "u-wait"
    assert audit[1]["target_uid"] == "u-seated"
    for line in audit:
        assert "@" not in repr(line), line


# --------------------------------------------------------------------- roster


def test_the_roster_summary_carries_the_seat_cap(monkeypatch, as_admin, client):
    body = client.get("/admin/accounts", headers=AUTH).json()
    assert body["summary"]["seat_cap"] == 25
    assert body["summary"]["seats_active"] == 3


def test_the_roster_summary_seat_cap_is_null_when_unconfigured(
    monkeypatch, as_admin, client
):
    monkeypatch.setenv("MAX_USERS", "unlimited")
    body = client.get("/admin/accounts", headers=AUTH).json()
    assert body["summary"]["seat_cap"] is None


def test_a_contended_grant_retries_and_writes_one_seat(monkeypatch, as_admin, client):
    """The store fake aborts the first commit; the real decorator retries."""
    contended = FakeStoreDB({"allowlist": {}, "waitlist": {}}, abort_once=True)
    monkeypatch.setattr(admin, "_client", lambda: contended)
    assert _grant(client).status_code == 200
    assert [t.commits for t in contended.transactions] == [1]
    assert list(contended.data["allowlist"]) == ["waiting@x.com"]


def test_the_allowlist_lines_a_grant_and_revoke_trigger_carry_no_email(
    as_admin, client
):
    """The admin audit lines omit the email; the allowlist's own lines, written
    on the same request, must not put it back."""
    with structlog.testing.capture_logs() as logs:
        assert _grant(client).status_code == 200
        assert _revoke(client).status_code == 200
    lines = [e for e in logs if str(e.get("event", "")).startswith("allowlist.")]
    assert {e["event"] for e in lines} >= {"allowlist.seat_added", "allowlist.revoked"}
    assert all("@" not in str(e) for e in lines)
