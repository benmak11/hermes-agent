# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The company-management endpoints, now that they write a per-user overlay.

This file used to test ``tools.companies``' YAML mutators — ``promote_to_known``
and friends. They passed, and the feature did not work: they edited
``data/companies/*.yaml`` on the container serving the API request, while under
``QUEUE_MODE=1`` the crawl reads that file on ``hermes-worker``, and a deploy
replaced the filesystem regardless. Passing tests over a write nobody reads.

So the claims here are about the seam that actually decides the fetch set:

1. A click writes **one document, at the key the reader reads**, and the crawl's
   composed fetch set changes as a result. The last test walks the whole path
   rather than trusting that the write shape and the read shape agree.
2. The write is a blind ``set()`` of a document whose id is the key — never a
   read-modify-write of a list, which is what the old mutators did and what
   loses one of two concurrent clicks.
3. The pool and the overlay stay distinguishable in the response. The global
   blocklist applies to everyone; an exclusion applies to one user. A UI that
   showed them as one thing would be lying about both.
4. ``promote`` is gone, and a slug that cannot be a document id is a 4xx.
"""

import asyncio

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.companies as companies_mod
import tools.companies as tc
import tools.company_prefs as prefs
from api.deps import verify_user


# ---------------------------------------------------------------------------
# Fakes: just enough async Firestore for users/{uid}/company_prefs, with writes
# ---------------------------------------------------------------------------
class _FakeSnap:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeDoc:
    def __init__(self, db, user_id: str, doc_id: str):
        self._db = db
        self._user_id = user_id
        self._doc_id = doc_id

    async def set(self, data, **kwargs):
        # A precondition or a merge here would be a different design; record the
        # kwargs so a test can say so rather than only implying it.
        self._db.set_calls.append((self._user_id, self._doc_id, data, kwargs))
        self._db.docs.setdefault(self._user_id, {})[self._doc_id] = dict(data)

    async def get(self):
        raise AssertionError(
            "the write path must not read before writing — that is the "
            "read-modify-write shape it exists to avoid"
        )


class _FakePrefsCollection:
    def __init__(self, db, user_id: str):
        self._db = db
        self._user_id = user_id

    def document(self, doc_id: str):
        return _FakeDoc(self._db, self._user_id, doc_id)

    async def stream(self):
        self._db.stream_calls += 1
        for doc_id, data in self._db.docs.get(self._user_id, {}).items():
            yield _FakeSnap(doc_id, data)


class _FakeUserDoc:
    def __init__(self, db, user_id: str):
        self._db = db
        self._user_id = user_id

    def collection(self, name):
        assert name == prefs.COLLECTION, name
        return _FakePrefsCollection(self._db, self._user_id)


class _FakeUsers:
    def __init__(self, db):
        self._db = db

    def document(self, user_id):
        return _FakeUserDoc(self._db, user_id)


class _FakeDB:
    """``docs`` maps user_id -> {doc_id: stored dict}."""

    def __init__(self, docs: dict | None = None):
        self.docs = docs or {}
        self.set_calls: list[tuple] = []
        self.stream_calls = 0

    def collection(self, name):
        assert name == "users", name
        return _FakeUsers(self)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A small, known global pool in place of data/companies."""
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(
        yaml.safe_dump(
            {
                "greenhouse": [{"slug": "stripe", "added": "2026-06-01"}],
                "lever": [{"slug": "stripe"}],
            }
        )
    )
    (d / "unvetted.yaml").write_text(
        yaml.safe_dump({"greenhouse": [{"slug": "newco", "added": "2026-06-20"}]})
    )
    (d / "blocklist.yaml").write_text(
        yaml.safe_dump(
            {
                "blocked": [
                    {
                        "platform": "greenhouse",
                        "slug": "spamco",
                        "blocked_at": "2026-05-01",
                        "reason": "ghost jobs",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(tc, "DATA_DIR", d)
    return d


@pytest.fixture
def db():
    return _FakeDB()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(companies_mod, "_client", lambda: db)
    app = FastAPI()
    app.include_router(companies_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def _block(client, slug="stripe", platform="greenhouse", **extra):
    return client.post(
        "/companies/action",
        json={"platform": platform, "slug": slug, "action": "block", **extra},
    )


# ---------------------------------------------------------------------------
# POST /companies/action — the write
# ---------------------------------------------------------------------------
def test_an_action_writes_one_overlay_document_for_this_user(client, db) -> None:
    assert _block(client, reason="onsite only").status_code == 200

    assert list(db.docs) == ["u1"]
    (doc_id,) = db.docs["u1"]
    assert doc_id == "greenhouse:stripe"

    doc = db.docs["u1"][doc_id]
    assert doc["platform"] == "greenhouse"
    assert doc["slug"] == "stripe"
    assert doc["state"] == "excluded"
    assert doc["action"] == "block"
    assert doc["reason"] == "onsite only"
    assert doc["updated_at"].startswith("20")


def test_the_user_comes_from_the_token_not_the_body(client, db) -> None:
    """A client must not be able to write someone else's exclusions."""
    resp = client.post(
        "/companies/action",
        json={
            "platform": "greenhouse",
            "slug": "stripe",
            "action": "block",
            "user_id": "someone-else",
        },
    )

    assert resp.status_code == 200
    assert list(db.docs) == ["u1"]


@pytest.mark.parametrize("action", ["block", "dismiss", "pause"])
def test_every_action_produces_the_excluded_state(client, db, action) -> None:
    """``state`` is what the pipeline reads and ``action`` is what was clicked.

    All three buttons mean "stop fetching this for me"; if one of them wrote a
    different state, that board would keep being crawled and the button would
    look like it worked.
    """
    resp = client.post(
        "/companies/action",
        json={"platform": "greenhouse", "slug": "stripe", "action": action},
    )

    assert resp.status_code == 200
    doc = db.docs["u1"]["greenhouse:stripe"]
    assert (doc["state"], doc["action"]) == ("excluded", action)


def test_the_write_is_blind_no_merge_no_precondition(client, db) -> None:
    """One document per key, set whole. ``_FakeDoc.get`` raises, so a
    read-modify-write cannot pass this file at all; this pins the other half —
    the write carries no ``merge`` and no ``option``, because the whole document
    is derived from this one click and there is no prior state to preserve."""
    _block(client)

    (_uid, _doc_id, _data, kwargs) = db.set_calls[0]
    assert kwargs == {}


def test_two_concurrent_identical_clicks_converge(client, db) -> None:
    """The reason no compare-and-swap is needed: the id *is* the key, so the
    second write overwrites the first with the same value. The old mutators read
    a list, appended, and wrote it back — under two requests the second silently
    dropped the first."""
    assert _block(client).status_code == 200
    assert _block(client).status_code == 200

    assert len(db.docs["u1"]) == 1
    assert len(db.set_calls) == 2


def test_two_companies_are_two_documents_not_one_growing_list(client, db) -> None:
    _block(client, slug="stripe")
    _block(client, slug="newco")

    assert sorted(db.docs["u1"]) == ["greenhouse:newco", "greenhouse:stripe"]


def test_the_same_slug_on_two_platforms_is_two_documents(client, db) -> None:
    _block(client, slug="stripe", platform="greenhouse")
    _block(client, slug="stripe", platform="lever")

    assert sorted(db.docs["u1"]) == ["greenhouse:stripe", "lever:stripe"]


# ---------------------------------------------------------------------------
# POST /companies/action — refusals
# ---------------------------------------------------------------------------
def test_a_slug_that_cannot_be_a_document_id_is_a_4xx_not_a_500(client, db) -> None:
    """``google_jobs``/``meta_jobs`` reuse the slug slot as a free-text search
    query (``known.yaml`` really contains ``slug: software engineer``), so a
    slash in it is reachable user input. In a document id a slash addresses a
    *different* subcollection rather than failing, so it is refused — and
    refused cleanly, before anything is written."""
    resp = client.post(
        "/companies/action",
        json={
            "platform": "google_jobs",
            "slug": "software engineer (backend/infra)",
            "action": "block",
        },
    )

    assert 400 <= resp.status_code < 500
    assert db.docs == {}
    assert db.set_calls == []


def test_promote_is_no_longer_an_action(client, db) -> None:
    """It was a global operator action that never changed the fetch set
    (``all_active_companies`` returns known *and* unvetted), and there is no
    global write path left for it to use. Promotion is a git edit."""
    resp = client.post(
        "/companies/action",
        json={"platform": "greenhouse", "slug": "newco", "action": "promote"},
    )

    assert resp.status_code == 422
    assert db.docs == {}


def test_an_unknown_action_is_rejected(client, db) -> None:
    resp = client.post(
        "/companies/action",
        json={"platform": "greenhouse", "slug": "newco", "action": "bogus"},
    )

    assert resp.status_code == 422
    assert db.docs == {}


def test_an_unknown_platform_is_rejected(client, db) -> None:
    resp = client.post(
        "/companies/action",
        json={"platform": "workday", "slug": "newco", "action": "block"},
    )

    assert resp.status_code == 422
    assert db.docs == {}


# ---------------------------------------------------------------------------
# GET /companies — pool + overlay, kept distinguishable
# ---------------------------------------------------------------------------
def test_the_pool_comes_back_unexcluded_when_the_overlay_is_empty(
    client, data_dir
) -> None:
    body = client.get("/companies").json()

    assert [c["slug"] for c in body["known"]["greenhouse"]] == ["stripe"]
    assert body["excluded"] == []
    assert all(
        not entry["excluded"]
        for group in ("known", "unvetted")
        for entries in body[group].values()
        for entry in entries
    )


def test_an_excluded_company_is_flagged_but_still_listed(client, data_dir) -> None:
    """Annotated, not filtered. If the row vanished, the user would have no way
    to see what they had excluded — and no way to tell it from a company that
    dropped out of the pool."""
    _block(client, slug="stripe", platform="greenhouse")

    body = client.get("/companies").json()
    rows = {c["slug"]: c for c in body["known"]["greenhouse"]}

    assert rows["stripe"]["excluded"] is True
    assert body["excluded"] == [{"platform": "greenhouse", "slug": "stripe"}]


def test_the_flag_lands_on_exactly_the_excluded_pair(client, data_dir) -> None:
    """The same slug on another platform is a different company."""
    _block(client, slug="stripe", platform="greenhouse")

    body = client.get("/companies").json()

    assert body["known"]["greenhouse"][0]["excluded"] is True
    assert body["known"]["lever"][0]["excluded"] is False


def test_an_unvetted_company_can_be_excluded(client, data_dir) -> None:
    _block(client, slug="newco", platform="greenhouse")

    body = client.get("/companies").json()

    assert body["unvetted"]["greenhouse"][0]["excluded"] is True


def test_the_global_blocklist_is_not_this_users_exclusions(client, data_dir) -> None:
    """Two different things: ``blocklist`` is git-shipped and applies to
    everyone, ``excluded`` is one user's overlay. Merging them in the response
    would misreport both — a user would think they had blocked ``spamco``, and
    that un-excluding it was theirs to do."""
    _block(client, slug="stripe", platform="greenhouse")

    body = client.get("/companies").json()

    assert [b["slug"] for b in body["blocklist"]] == ["spamco"]
    assert body["excluded"] == [{"platform": "greenhouse", "slug": "stripe"}]


def test_one_users_exclusions_do_not_leak_into_anothers(
    db, data_dir, monkeypatch
) -> None:
    db.docs["u2"] = {
        "greenhouse:stripe": {
            "platform": "greenhouse",
            "slug": "stripe",
            "state": "excluded",
            "action": "block",
            "reason": None,
            "updated_at": "2026-09-03T00:00:00+00:00",
        }
    }
    monkeypatch.setattr(companies_mod, "_client", lambda: db)
    app = FastAPI()
    app.include_router(companies_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"

    body = TestClient(app).get("/companies").json()

    assert body["excluded"] == []
    assert body["known"]["greenhouse"][0]["excluded"] is False


def test_the_overlay_is_read_once_per_request(client, data_dir, db) -> None:
    client.get("/companies")
    assert db.stream_calls == 1


# ---------------------------------------------------------------------------
# The whole path: a click changes what the crawl fetches
# ---------------------------------------------------------------------------
def test_a_click_removes_the_board_from_the_composed_fetch_set(
    client, data_dir, db
) -> None:
    """The claim the old tests could not make. The write shape and the read
    shape are defined in different modules; this is what would catch them
    drifting apart — a key written as ``greenhouse/stripe`` or a state written
    as ``blocked`` passes every test above and silently changes nothing.
    """
    before = tc.all_active_companies()
    assert ("greenhouse", "stripe", "known") in before

    assert _block(client, slug="stripe", platform="greenhouse").status_code == 200

    exclusions = asyncio.run(prefs.load_exclusions(db, "u1"))
    after = tc.all_active_companies(exclusions)

    assert ("greenhouse", "stripe", "known") not in after
    assert [c for c in before if c not in after] == [("greenhouse", "stripe", "known")]


def test_an_exclusion_does_not_shrink_the_pool_on_disk(client, data_dir) -> None:
    """Nothing on the request path writes YAML any more. The pool is global and
    reviewed; one user's click must not remove a board for everyone — which is
    exactly what ``block_company`` used to do."""
    known_before = (data_dir / "known.yaml").read_text()
    unvetted_before = (data_dir / "unvetted.yaml").read_text()
    blocklist_before = (data_dir / "blocklist.yaml").read_text()

    _block(client, slug="stripe")
    client.post(
        "/companies/action",
        json={"platform": "greenhouse", "slug": "newco", "action": "dismiss"},
    )

    assert (data_dir / "known.yaml").read_text() == known_before
    assert (data_dir / "unvetted.yaml").read_text() == unvetted_before
    assert (data_dir / "blocklist.yaml").read_text() == blocklist_before
