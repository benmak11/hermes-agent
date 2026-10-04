# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``GET /profile`` through a real route, not ``verify_user`` in isolation.

``test_allowlist_auth.py`` pins the wiring against a synthetic ``/whoami``
route built only to exercise ``Depends(verify_user)``. The frontend pre-flight
probe hangs off ``GET /profile`` specifically, so this file proves the
assumption behind it on the real ``api.routes.profile.router``: that an
ordinary authenticated endpoint enforces the allowlist end to end, with
nothing in the module bypassing it (a leftover ``dependency_overrides``, a
route reading the uid some other way).

``tools.allowlist.is_allowed`` is faked here; its own fail-closed matrix is
pinned in ``test_allowlist.py``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.deps as deps
import api.routes.profile as profile_mod
from tools import allowlist


def _patch_decoded(monkeypatch, decoded: dict) -> None:
    """Same seam ``test_allowlist_auth`` uses: stand in for a verified Firebase ID token
    without touching the real Admin SDK."""
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    monkeypatch.setattr(fb_auth_module, "verify_id_token", lambda token: dict(decoded))


class _FakeSnap:
    def __init__(self, data: dict | None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeRef:
    def __init__(self, data: dict | None):
        self._data = data

    def get(self):
        return _FakeSnap(self._data)

    def set(self, data, merge=False):
        # GET /profile stamps `data_epoch` on a profile that lacks one, so this
        # read path does write, once per user. Not what these tests are about —
        # they assert the allowlist gate — but the route has to survive it.
        if self._data is not None:
            self._data.update(data)


class _FakeCollection:
    def __init__(self, data: dict | None):
        self._data = data

    def document(self, uid: str):
        return _FakeRef(self._data)


class _FakeSyncClient:
    """Stands in for ``api.routes.profile``'s *sync* Firestore client — a
    different client object from ``api.deps``'s async one, which is why both
    have to be patched independently for this route to work end to end."""

    def __init__(self, data: dict | None):
        self._data = data

    def collection(self, name: str):
        assert name == "users"
        return _FakeCollection(self._data)


@pytest.fixture(autouse=True)
def _clear_allowlist_cache(monkeypatch):
    """The TTL cache is module state keyed only by
    uid, and every test here uses ``u1``."""
    monkeypatch.setattr(deps, "_allowlist_cache", {})


@pytest.fixture
def app() -> FastAPI:
    """The real profile router, not a synthetic stand-in — and deliberately
    no ``dependency_overrides`` on ``verify_user``, unlike
    ``test_api_profile.py``, whose fixture overrides it precisely to skip
    auth and test the route's own logic. This file wants the opposite: auth
    fully wired, exercising the real dependency chain a browser request
    would hit."""
    app = FastAPI()
    app.include_router(profile_mod.router)
    return app


def test_profile_403s_through_the_real_route_when_not_allowlisted(monkeypatch, app):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "nope@example.com"})
    monkeypatch.setattr(deps, "_client", lambda: object())
    monkeypatch.setattr(
        profile_mod, "_client", lambda: _FakeSyncClient({"full_name": "Test User"})
    )

    async def fake_is_allowed(db, email):
        assert email == "nope@example.com"
        return False

    monkeypatch.setattr(allowlist, "is_allowed", fake_is_allowed)

    resp = TestClient(app).get("/profile", headers={"Authorization": "Bearer tok"})

    assert resp.status_code == 403


def test_profile_200s_through_the_real_route_when_allowlisted(monkeypatch, app):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "yes@example.com"})
    monkeypatch.setattr(deps, "_client", lambda: object())
    monkeypatch.setattr(
        profile_mod, "_client", lambda: _FakeSyncClient({"full_name": "Test User"})
    )

    async def fake_is_allowed(db, email):
        assert email == "yes@example.com"
        return True

    monkeypatch.setattr(allowlist, "is_allowed", fake_is_allowed)

    resp = TestClient(app).get("/profile", headers={"Authorization": "Bearer tok"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["full_name"] == "Test User"
    assert body["onboarding_complete"] is True


def test_profile_is_unaffected_with_enforcement_off(monkeypatch, app):
    """Positive control: the same email denied above must sail through with
    the flag unset."""
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    _patch_decoded(monkeypatch, {"uid": "u1", "email": "nope@example.com"})
    monkeypatch.setattr(
        profile_mod, "_client", lambda: _FakeSyncClient({"full_name": "Test User"})
    )

    def _explode_sync(*args, **kwargs):
        raise AssertionError("must not have reached Firestore for the allowlist")

    async def _explode_async(*args, **kwargs):
        raise AssertionError("must not have checked the allowlist")

    monkeypatch.setattr(deps, "_client", _explode_sync)
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    resp = TestClient(app).get("/profile", headers={"Authorization": "Bearer tok"})

    assert resp.status_code == 200
