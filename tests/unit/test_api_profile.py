# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""PUT /profile: onboarding-completion discovery kickoff.

Pins the fix for "discovery never runs for a new user" — the first time a
user's profile transitions to onboarding_complete, save_profile schedules one
discovery cycle; later edits (already complete) must not repeat it. No real
Firestore/LLM calls: everything is faked/mocked.
"""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.profile as profile_mod
from api.deps import verify_user
from models.profile import Bullet, Education, Experience, JobPreferences, MasterProfile


def _profile_payload() -> dict:
    profile = MasterProfile(
        user_id="u1",
        full_name="Test User",
        email="test@example.com",
        location="Remote",
        objective_template="{seniority} professional seeking a {role} role at {company}.",
        experience=[
            Experience(
                company="Acme",
                role="Software Engineer",
                start=date(2020, 1, 1),
                bullets=[Bullet(text="Built a thing", tags=["python"])],
            )
        ],
        education=[
            Education(
                institution="State University",
                degree="BS",
                field="Computer Science",
                start_year=2012,
                end_year=2016,
            )
        ],
        skills={"technical": ["python"]},
        preferences=JobPreferences(
            target_role_families=["engineering"],
            target_titles=["Staff Software Engineer"],
            target_seniorities=["staff"],
        ),
    )
    return profile.model_dump(mode="json")


class _FakeSnap:
    def __init__(self, data: dict | None):
        self._data = data

    def to_dict(self):
        return self._data


class _FakeRef:
    def __init__(self, store: dict, uid: str):
        self._store = store
        self._uid = uid

    def get(self):
        return _FakeSnap(self._store.get(self._uid))

    def set(self, data, merge=False):
        current = self._store.setdefault(self._uid, {})
        current.update(data)


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store

    def document(self, uid: str):
        return _FakeRef(self._store, uid)


class _FakeClient:
    def __init__(self, store: dict):
        self._store = store

    def collection(self, name):
        assert name == "users"
        return _FakeCollection(self._store)


@pytest.fixture
def store():
    return {}


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(profile_mod, "_client", lambda: _FakeClient(store))
    app = FastAPI()
    app.include_router(profile_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def test_first_completion_schedules_discovery_kickoff(client, monkeypatch):
    calls = []

    async def fake_dispatch(kind, user_id, *, trigger):
        calls.append((kind, user_id, trigger))

    monkeypatch.setattr("api.routes.discovery.dispatch_cycle", fake_dispatch)

    resp = client.put("/profile", json=_profile_payload())

    assert resp.status_code == 200
    assert calls == [("discovery", "u1", "onboarding")]


def test_repeat_edit_does_not_repeat_kickoff(client, store, monkeypatch):
    store["u1"] = {"onboarding_complete": True}
    calls = []

    async def fake_dispatch(kind, user_id, *, trigger):
        calls.append((kind, user_id, trigger))

    monkeypatch.setattr("api.routes.discovery.dispatch_cycle", fake_dispatch)

    resp = client.put("/profile", json=_profile_payload())

    assert resp.status_code == 200
    assert calls == []
