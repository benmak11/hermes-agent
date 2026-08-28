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
    """Without a queue the kickoff stays a background task: ``dispatch_cycle``
    would run the whole discovery-and-scoring cycle, which is minutes of work
    and cannot happen inside the request."""
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    calls = []

    async def fake_dispatch(kind, user_id, *, trigger):
        calls.append((kind, user_id, trigger))

    monkeypatch.setattr("api.routes.discovery.dispatch_cycle", fake_dispatch)

    resp = client.put("/profile", json=_profile_payload())

    assert resp.status_code == 200
    assert calls == [("discovery", "u1", "onboarding")]


def test_first_completion_enqueues_inside_the_request(client, monkeypatch):
    """With a queue the kickoff is one RPC, and it must not be the part of this
    route that depends on the instance still having CPU after the response.
    It is the only discovery run a brand-new user ever gets — deferring it is
    the shape of the original "discovery never runs" bug."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    enqueued = []
    monkeypatch.setattr(
        "api.routes.discovery.queues.enqueue",
        lambda q, p, b, *, task_id=None: enqueued.append((q, p, b, task_id)) or True,
    )

    def never(*args, **kwargs):
        raise AssertionError("the kickoff must not be deferred past the response")

    monkeypatch.setattr("api.routes.discovery.dispatch_cycle", never)

    resp = client.put("/profile", json=_profile_payload())

    assert resp.status_code == 200
    queue, path, payload, task_id = enqueued[0]
    assert (queue, path) == ("discovery", "/tasks/discovery")
    assert payload == {"user_id": "u1", "trigger": "onboarding"}
    assert task_id.startswith("onboarding-discovery-u1-")


def test_a_kickoff_that_cannot_be_enqueued_does_not_burn_itself(
    client, store, monkeypatch
):
    """``onboarding_complete`` is the only thing that makes this kickoff fire
    again, so a failed enqueue must not leave it set.

    Otherwise the failure *is* the bug this route exists to fix, one level down:
    the user is onboarded, saw an error, and the retry finds
    ``first_completion`` False and never dispatches — discovery never runs for
    them, permanently, with nothing to alert on.
    """
    monkeypatch.setenv("QUEUE_MODE", "1")

    def unreachable(*args, **kwargs):
        raise RuntimeError("Cloud Tasks is unreachable")

    monkeypatch.setattr("api.routes.discovery.queues.enqueue", unreachable)

    resp = client.put("/profile", json=_profile_payload())

    assert resp.status_code == 503
    # The profile itself is saved — only the flag is given back.
    assert store["u1"]["onboarding_complete"] is False
    assert store["u1"]["full_name"] == "Test User"


def test_the_retry_after_a_failed_kickoff_really_retries(client, store, monkeypatch):
    """The other half: giving the flag back is only worth anything if the next
    save fires the kickoff, since nothing else in the app ever will."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    enqueued = []
    failing = {"now": True}

    def sometimes(queue, path, payload, *, task_id=None):
        if failing["now"]:
            raise RuntimeError("Cloud Tasks is unreachable")
        enqueued.append((queue, path, payload, task_id))
        return True

    monkeypatch.setattr("api.routes.discovery.queues.enqueue", sometimes)

    assert client.put("/profile", json=_profile_payload()).status_code == 503
    failing["now"] = False
    assert client.put("/profile", json=_profile_payload()).status_code == 200

    assert store["u1"]["onboarding_complete"] is True
    assert [(q, p) for q, p, _b, _t in enqueued] == [("discovery", "/tasks/discovery")]


def test_a_deduped_kickoff_is_not_a_failure(client, store, monkeypatch):
    """An hour-granular name already queued for this user is the outcome we
    wanted, not an error to hand the user."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    monkeypatch.setattr("api.routes.discovery.queues.enqueue", lambda *a, **k: False)

    assert client.put("/profile", json=_profile_payload()).status_code == 200
    assert store["u1"]["onboarding_complete"] is True


def test_repeat_edit_does_not_repeat_kickoff(client, store, monkeypatch):
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    store["u1"] = {"onboarding_complete": True}
    calls = []

    async def fake_dispatch(kind, user_id, *, trigger):
        calls.append((kind, user_id, trigger))

    monkeypatch.setattr("api.routes.discovery.dispatch_cycle", fake_dispatch)

    resp = client.put("/profile", json=_profile_payload())

    assert resp.status_code == 200
    assert calls == []
