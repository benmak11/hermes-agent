# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The two halves of "29 of 29 reviewed on an account with no jobs".

The review header sums a server count (jobs still pending) with a browser count
(decisions made, in ``localStorage``). A server-side wipe clears the first and
cannot reach the second, so a wiped account kept reporting a finished review
session for jobs that no longer existed.

Fixed from the server side, because the server is the only side that knows a
wipe happened:

- ``GET /profile`` stamps a ``data_epoch``. The browser stores its counts
  against it and drops them when it changes. A wipe deletes the user document,
  so the replacement gets a new epoch — which is why the stamp is lazy rather
  than written once at onboarding.
- ``GET /jobs/pending`` reports how many pending jobs exist before filtering,
  so an empty queue can say *why* it is empty. Only "all below the threshold"
  is fixed by lowering the threshold; "nothing discovered" and "nothing scored
  yet" are not, and were previously told to lower it anyway.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.jobs as jobs_mod
import api.routes.profile as profile_mod
from api.deps import verify_user

# --- GET /profile: the epoch ------------------------------------------------


class _FakeSnap:
    def __init__(self, data: dict | None):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class _FakeRef:
    def __init__(self, store: dict, uid: str, writes: list):
        self._store, self._uid, self._writes = store, uid, writes

    def get(self):
        return _FakeSnap(self._store.get(self._uid))

    def set(self, data, merge=False):
        self._writes.append(data)
        self._store.setdefault(self._uid, {}).update(data)


class _FakeCollection:
    def __init__(self, store: dict, writes: list):
        self._store, self._writes = store, writes

    def document(self, uid: str):
        return _FakeRef(self._store, uid, self._writes)


class _FakeClient:
    def __init__(self, store: dict, writes: list):
        self._store, self._writes = store, writes

    def collection(self, name):
        assert name == "users"
        return _FakeCollection(self._store, self._writes)


@pytest.fixture
def writes():
    return []


@pytest.fixture
def store():
    return {"u1": {"full_name": "Test User", "onboarding_complete": True}}


@pytest.fixture
def profile_client(store, writes, monkeypatch):
    monkeypatch.setattr(profile_mod, "_client", lambda: _FakeClient(store, writes))
    app = FastAPI()
    app.include_router(profile_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def test_profile_stamps_an_epoch_when_the_document_has_none(profile_client, writes):
    body = profile_client.get("/profile").json()

    epoch = body["profile"]["data_epoch"]
    assert epoch
    # Persisted, not just returned: the browser compares against it on the next
    # load, so an epoch that lived only in the response would differ every time
    # and wipe a live session's counts on every page view.
    assert writes == [{"data_epoch": epoch}]


def test_the_epoch_is_stable_across_reads(profile_client, writes):
    """The load-bearing property. The browser drops its counts whenever this
    value changes, so an epoch that were regenerated per request would clear a
    real review session continuously."""
    first = profile_client.get("/profile").json()["profile"]["data_epoch"]
    second = profile_client.get("/profile").json()["profile"]["data_epoch"]

    assert first == second
    assert len(writes) == 1, "the epoch was rewritten on a read that already had one"


def test_a_wiped_account_gets_a_different_epoch_than_it_had(profile_client, store):
    """What a wipe actually looks like: ``wipe_user_data`` deletes the user
    document outright, so the next profile save creates a fresh one. The new
    document must not inherit the old epoch, or the stale counts survive the
    wipe — which is the whole bug."""
    before = profile_client.get("/profile").json()["profile"]["data_epoch"]

    store.clear()  # the wipe
    store["u1"] = {"full_name": "Test User", "onboarding_complete": True}

    after = profile_client.get("/profile").json()["profile"]["data_epoch"]
    assert after != before


def test_a_user_who_never_onboarded_is_not_stamped(profile_client, store, writes):
    """No profile means the review page is never reached — it redirects to
    onboarding — so there is nothing to reconcile and no reason to write."""
    store["u1"] = {}

    body = profile_client.get("/profile").json()

    assert body == {"profile": None, "onboarding_complete": False}
    assert writes == []


# --- GET /jobs/pending: why the queue is empty ------------------------------


class _JobSnap:
    def __init__(self, doc_id: str, data: dict):
        self.id, self._data = doc_id, data

    def to_dict(self):
        return dict(self._data)


class _JobQuery:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *, filter=None):
        return self

    def stream(self):
        return iter(self._docs)


class _JobsCollection:
    def __init__(self, docs):
        self._docs = docs

    def collection(self, name):
        assert name == "jobs"
        return _JobQuery(self._docs)


class _JobsClient:
    def __init__(self, docs):
        self._docs = docs

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, uid):
        return _JobsCollection(self._docs)


def _jobs_client(docs, monkeypatch):
    monkeypatch.setattr(jobs_mod, "_client", lambda: _JobsClient(docs))
    monkeypatch.setattr(jobs_mod, "tick_user", lambda uid: None)
    app = FastAPI()
    app.include_router(jobs_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def _scored(doc_id: str, score: int) -> _JobSnap:
    return _JobSnap(
        doc_id, {"user_decision": "pending", "match": {"overall_score": score}}
    )


def _unscored(doc_id: str) -> _JobSnap:
    return _JobSnap(doc_id, {"user_decision": "pending"})


def test_an_account_with_no_jobs_reports_zero_not_just_an_empty_list(monkeypatch):
    """The case that shipped: the account had been wiped, so the list was empty
    and the UI told the user to lower a threshold that was not the problem."""
    client = _jobs_client([], monkeypatch)

    body = client.get("/jobs/pending?min_score=80").json()

    assert body["jobs"] == []
    assert body["pending_total"] == 0
    assert body["scored_total"] == 0


def test_jobs_filtered_out_by_the_threshold_are_still_counted(monkeypatch):
    """The one state where lowering the threshold genuinely helps: the jobs
    exist and are scored, they are just below the bar."""
    client = _jobs_client([_scored("a", 40), _scored("b", 55)], monkeypatch)

    body = client.get("/jobs/pending?min_score=80").json()

    assert body["jobs"] == []
    assert body["pending_total"] == 2
    assert body["scored_total"] == 2


def test_unscored_jobs_count_as_pending_but_not_as_scored(monkeypatch):
    """A brand-new account right after discovery. Lowering the threshold to
    zero would still show nothing, because these have no score at all."""
    client = _jobs_client([_unscored("a"), _unscored("b"), _unscored("c")], monkeypatch)

    body = client.get("/jobs/pending?min_score=80").json()

    assert body["jobs"] == []
    assert body["pending_total"] == 3
    assert body["scored_total"] == 0


def test_the_counts_do_not_change_which_jobs_are_returned(monkeypatch):
    """The counts are tallies of a pass this route already made. Adding them
    must not alter the filtering, which is the part users see."""
    client = _jobs_client(
        [_scored("low", 10), _scored("high", 90), _unscored("pending")], monkeypatch
    )

    body = client.get("/jobs/pending?min_score=80").json()

    assert [j["id"] for j in body["jobs"]] == ["high"]
    assert body["pending_total"] == 3
    assert body["scored_total"] == 2
