# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``/journeys``: the four CRUD routes behind the journey board.

The server owns ``id``, ``user_id`` and the timestamps and enforces two
invariants (unique stage ids, at most one ``current`` stage); everything else
is the client's. No real Firestore: the store is a flat ``path -> dict``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.journeys as journeys_mod
from api.deps import verify_user

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    """How ``model_dump(mode="json")`` writes a UTC datetime (``Z``, not ``+00:00``)."""
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Fakes: sync Firestore, nested ``users/{uid}/journeys/{id}`` paths
# ---------------------------------------------------------------------------
class _Snap:
    def __init__(self, path: str, data: dict | None):
        self.id = path.rsplit("/", 1)[-1]
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _DocRef:
    def __init__(self, store: dict, path: str):
        self._store = store
        self.path = path

    def get(self):
        return _Snap(self.path, self._store.get(self.path))

    def set(self, data, merge=False):
        if merge:
            self._store.setdefault(self.path, {}).update(data)
        else:
            self._store[self.path] = dict(data)

    def delete(self):
        self._store.pop(self.path, None)

    def collection(self, name: str):
        return _CollRef(self._store, f"{self.path}/{name}")


class _CollRef:
    def __init__(self, store: dict, path: str):
        self._store = store
        self.path = path

    def document(self, doc_id: str) -> _DocRef:
        return _DocRef(self._store, f"{self.path}/{doc_id}")

    def stream(self):
        prefix = f"{self.path}/"
        for path, data in sorted(self._store.items()):
            if path.startswith(prefix) and "/" not in path[len(prefix) :]:
                yield _Snap(path, data)


class _FakeClient:
    def __init__(self, store: dict):
        self._store = store

    def collection(self, name: str) -> _CollRef:
        return _CollRef(self._store, name)


def _stage(stage_id: str, name: str, status: str) -> dict:
    return {"id": stage_id, "name": name, "status": status}


def _journey(**over) -> dict:
    """A valid POST body (the server-owned fields left out)."""
    body = {
        "company": "Shopify",
        "role": "Staff Engineer",
        "source": "manual",
        "stages": [
            _stage("applied", "Applied", "done"),
            _stage("recruiter", "Recruiter call", "current"),
        ],
    }
    body.update(over)
    return body


def _stored(uid: str, journey_id: str, **over) -> dict:
    """A document as the route would have written it."""
    doc = _journey(
        id=journey_id,
        user_id=uid,
        created_at=_iso(T0),
        updated_at=_iso(T0),
        outcome="in_progress",
        application_id=None,
        job_url=None,
        ended_at_stage_id=None,
        retro=None,
    )
    doc.update(over)
    return doc


@pytest.fixture
def store():
    return {}


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(journeys_mod, "_client", lambda: _FakeClient(store))
    monkeypatch.setattr(journeys_mod, "_now", lambda: T1)
    app = FastAPI()
    app.include_router(journeys_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


# ---------------------------------------------------------------------------
# Server-owned fields
# ---------------------------------------------------------------------------
def test_post_assigns_id_user_and_timestamps(client, store):
    """The client's ``id``/``user_id`` are ignored, not trusted."""
    resp = client.post("/journeys", json=_journey(id="client-chosen", user_id="u2"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    uuid.UUID(body["id"])  # server-assigned, parses
    assert body["id"] != "client-chosen"
    assert body["user_id"] == "u1"
    assert body["created_at"] == body["updated_at"] == _iso(T1)
    assert store[f"users/u1/journeys/{body['id']}"] == body


def test_put_forces_user_id_from_token(client, store):
    store["users/u1/journeys/j1"] = _stored("u1", "j1")

    resp = client.put("/journeys/j1", json=_journey(user_id="u2", id="other"))

    assert resp.status_code == 200, resp.text
    doc = store["users/u1/journeys/j1"]
    assert doc["user_id"] == "u1"
    assert doc["id"] == "j1"
    assert not [p for p in store if p.startswith("users/u2/")]
    assert "users/u1/journeys/other" not in store


def test_put_preserves_created_at_and_moves_updated_at(client, store):
    """Both client timestamps are wrong on purpose: ``created_at`` comes from
    the stored document, ``updated_at`` from the clock."""
    store["users/u1/journeys/j1"] = _stored("u1", "j1")

    resp = client.put(
        "/journeys/j1", json=_journey(created_at=_iso(T1), updated_at=_iso(T0))
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created_at"] == _iso(T0)
    assert body["updated_at"] == _iso(T1)
    doc = store["users/u1/journeys/j1"]
    assert doc["created_at"] == _iso(T0)
    assert doc["updated_at"] == _iso(T1)


def test_put_is_a_full_replace(client, store):
    """A removed stage must disappear — ``set`` without ``merge``. A merge
    would also keep a key the new document does not carry, so plant one."""
    store["users/u1/journeys/j1"] = _stored(
        "u1",
        "j1",
        stages=[
            _stage("applied", "Applied", "done"),
            _stage("recruiter", "Recruiter call", "done"),
            _stage("onsite", "Onsite", "current"),
        ],
        stray=1,
    )

    resp = client.put(
        "/journeys/j1", json=_journey(stages=[_stage("applied", "Applied", "done")])
    )

    assert resp.status_code == 200, resp.text
    doc = store["users/u1/journeys/j1"]
    assert [s["id"] for s in doc["stages"]] == ["applied"]
    assert "stray" not in doc


# ---------------------------------------------------------------------------
# Listing and ownership
# ---------------------------------------------------------------------------
def test_get_lists_only_this_users_journeys_newest_first(client, store):
    store["users/u1/journeys/j1"] = _stored("u1", "j1", updated_at=_iso(T0))
    store["users/u1/journeys/j2"] = _stored("u1", "j2", updated_at=_iso(T1))
    store["users/u2/journeys/j9"] = _stored("u2", "j9", updated_at=_iso(T1))

    resp = client.get("/journeys")

    assert resp.status_code == 200, resp.text
    assert [j["id"] for j in resp.json()["journeys"]] == ["j2", "j1"]


def test_put_and_delete_404_on_a_foreign_or_missing_id(client, store):
    """Another user's id does not exist under this user's subcollection, so
    it is indistinguishable from a missing one — and PUT must not create it."""
    store["users/u2/journeys/j9"] = _stored("u2", "j9")
    before = dict(store)

    assert client.put("/journeys/j9", json=_journey()).status_code == 404
    assert client.delete("/journeys/j9").status_code == 404
    assert client.put("/journeys/nope", json=_journey()).status_code == 404
    assert store == before


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------
def test_two_current_stages_are_rejected(client, store):
    body = _journey(
        stages=[
            _stage("a", "Recruiter call", "current"),
            _stage("b", "Onsite", "current"),
        ]
    )

    resp = client.post("/journeys", json=body)

    assert resp.status_code == 422, resp.text
    assert "at most one stage may be current" in resp.text
    assert store == {}


def test_duplicate_stage_ids_are_rejected(client, store):
    body = _journey(
        stages=[_stage("x", "Recruiter call", "done"), _stage("x", "Onsite", "current")]
    )

    resp = client.post("/journeys", json=body)

    assert resp.status_code == 422, resp.text
    assert "stage ids must be unique" in resp.text
    assert store == {}


def test_delete_removes_the_doc(client, store):
    store["users/u1/journeys/j1"] = _stored("u1", "j1")

    resp = client.delete("/journeys/j1")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert "users/u1/journeys/j1" not in store
