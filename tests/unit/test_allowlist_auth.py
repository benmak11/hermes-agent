# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The invite gate wired into ``api.deps`` — shipped with enforcement off.

Three things matter more than the rest, and the order is deliberate:

1. **The dev bypass is untouched.** ``_verify_token`` returns for
   ``dev_mode() and AUTH_DEV_USER`` before this PR's code runs at all — a local
   process and the ``me`` demo account have to keep working exactly as before,
   even with ``ALLOWLIST_ENFORCED=1``. Pinned all the way through a real route,
   not just a function call, and by asserting no Firestore/allowlist call was
   *reached* — not merely that the request succeeded.
2. **``ALLOWLIST_ENFORCED`` unset is a full no-op** on the real-token path too
   — the whole point of this PR (D1) shipping before the one that flips the
   flag (D2).
3. **Enforced, it fails closed** — denied on a token with no ``email`` claim
   (403, not 500), denied on an email the allowlist rejects, and the 5-minute
   TTL cache (mirrors ``api.routes.discovery._last_tick_check``) both saves the
   redundant read and expires on schedule.

``tools.allowlist`` itself — the predicate's own fail-closed matrix, the
transactional seat cap — is pinned in ``test_allowlist.py``; this file is only
about the wiring in ``api.deps``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.deps as deps
from tools import allowlist


def _patch_decoded(monkeypatch, decoded: dict) -> None:
    """Stand in for a verified Firebase ID token, without touching the real
    Admin SDK (which needs live ADC credentials this suite must not have)."""
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    monkeypatch.setattr(fb_auth_module, "verify_id_token", lambda token: dict(decoded))


@pytest.fixture(autouse=True)
def _clear_allowlist_cache(monkeypatch):
    """The TTL cache is module state keyed only by uid — every test in this
    file uses ``u1``, so a stale entry from one test would otherwise leak into
    the next."""
    monkeypatch.setattr(deps, "_allowlist_cache", {})


@pytest.fixture
def protected_app() -> FastAPI:
    """The smallest possible route behind ``verify_user`` — enough to prove
    what actually reaches the dependency, not just what a specific endpoint
    happens to do with it."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(user_id: str = Depends(deps.verify_user)):
        return {"user_id": user_id}

    return app


def _explode(*args, **kwargs):
    raise AssertionError("must not have reached Firestore")


async def _explode_async(*args, **kwargs):
    raise AssertionError("must not have checked the allowlist")


# ---------------------------------------------------------------------------
# 1. The dev bypass is untouched
# ---------------------------------------------------------------------------


def test_the_dev_bypass_reaches_the_route_without_touching_firestore(
    monkeypatch, protected_app
):
    """Even with enforcement fully on: the bypass returns before this PR's
    code runs at all, by construction — this is the test that would fail if
    the ordering ever slipped."""
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv("AUTH_DEV_USER", "demo-uid")
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    monkeypatch.setattr(deps, "_client", _explode)
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    resp = TestClient(protected_app).get("/whoami")

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "demo-uid"}


def test_the_dev_bypass_is_untouched_with_enforcement_off_too(
    monkeypatch, protected_app
):
    """Positive control for the test above."""
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv("AUTH_DEV_USER", "demo-uid")
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.setattr(deps, "_client", _explode)
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    resp = TestClient(protected_app).get("/whoami")

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "demo-uid"}


# ---------------------------------------------------------------------------
# 2. ALLOWLIST_ENFORCED unset is a no-op on the real-token path
# ---------------------------------------------------------------------------


def test_enforcement_off_never_touches_the_allowlist(monkeypatch):
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "user@example.com"})
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    uid = asyncio.run(deps.verify_user(authorization="Bearer tok"))

    assert uid == "u1"


def test_enforcement_off_lets_through_a_token_with_no_email_too(monkeypatch):
    """The absent-email refusal is an enforcement-on behavior, not a bare
    property of the token shape."""
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    _patch_decoded(monkeypatch, {"uid": "u1"})
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    uid = asyncio.run(deps.verify_user(authorization="Bearer tok"))

    assert uid == "u1"


# ---------------------------------------------------------------------------
# 3. Enforced: fails closed
# ---------------------------------------------------------------------------


def test_an_allowed_email_passes_through(monkeypatch):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "user@example.com"})
    monkeypatch.setattr(deps, "_client", lambda: object())

    async def fake_is_allowed(db, email):
        assert email == "user@example.com"
        return True

    monkeypatch.setattr(allowlist, "is_allowed", fake_is_allowed)

    uid = asyncio.run(deps.verify_user(authorization="Bearer tok"))

    assert uid == "u1"


def test_a_denied_email_is_refused_with_403(monkeypatch):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "nope@example.com"})
    monkeypatch.setattr(deps, "_client", lambda: object())

    async def fake_is_allowed(db, email):
        return False

    monkeypatch.setattr(allowlist, "is_allowed", fake_is_allowed)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(deps.verify_user(authorization="Bearer tok"))

    assert raised.value.status_code == 403


def test_an_absent_email_claim_fails_closed_with_403_not_500(monkeypatch):
    """Actionable, not a crash: enforcement being on means "prove you're
    allowed", and there has to be an email to prove it with. No Firestore read
    is even attempted — there is nothing to check against."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    _patch_decoded(monkeypatch, {"uid": "u1"})  # no "email" claim
    monkeypatch.setattr(deps, "_client", _explode)
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(deps.verify_user(authorization="Bearer tok"))

    assert raised.value.status_code == 403
    assert raised.value.status_code != 500


def test_the_result_is_cached_for_five_minutes_keyed_by_uid(monkeypatch):
    """Same shape as ``api.routes.discovery._last_tick_check``: a cache, not a
    lock. Worst case a revocation bites up to 5 minutes late."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "user@example.com"})
    monkeypatch.setattr(deps, "_client", lambda: object())

    calls: list[str] = []

    async def counting_is_allowed(db, email):
        calls.append(email)
        return True

    monkeypatch.setattr(allowlist, "is_allowed", counting_is_allowed)

    base = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    clock = {"now": base}
    monkeypatch.setattr(
        deps, "datetime", SimpleNamespace(now=lambda tz=None: clock["now"])
    )

    asyncio.run(deps.verify_user(authorization="Bearer tok"))
    asyncio.run(deps.verify_user(authorization="Bearer tok"))
    assert calls == ["user@example.com"]  # second call served from cache

    clock["now"] = base + timedelta(minutes=6)
    asyncio.run(deps.verify_user(authorization="Bearer tok"))
    assert calls == ["user@example.com", "user@example.com"]  # cache expired
