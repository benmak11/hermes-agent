# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Account deletion: what it erases, what it must not, and the order.

Three claims, and the order is the one that carries the most weight:

1. **The wipe stays inside one user.** Another user's documents, their GCS
   blobs, and the two shared caches (``jd_cache``, ``board_cache/``) are still
   there afterwards. Both caches are content-keyed and cross-user — evicting
   them because one account closed charges everybody a re-parse.
2. **Nothing is destroyed until the inbound paths are closed.** The tombstone
   lands, the Firebase Auth account is deleted, and *only then* does anything
   get erased — with ``users/{uid}`` itself going last, so an interrupted wipe
   leaves an account that is still findable and still refusing work.
3. **A tombstoned account gets no more cycles.** ``run_discovery_cycle``,
   ``run_sweep_cycle`` and ``cron_tick``'s fan-out each refuse before spending
   anything. What that cannot do — and the reason it is a bound rather than a
   fix — is stop a cycle already past the check: its success write recreates
   the very document that was deleted.

No real Firestore, no real GCS, no real Firebase: every seam is faked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from google.cloud import storage

import api.routes.account as account
import api.routes.discovery as discovery
from api.deps import verify_user
from tools.account import delete as account_delete

DELETED_AT = "2026-09-03T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Fakes: enough async Firestore, GCS and Firebase Auth to run the real wipe
# ---------------------------------------------------------------------------
class _Snap:
    def __init__(self, path: str, data: dict | None, ref):
        self.id = path.rsplit("/", 1)[-1]
        self._data = data
        self.reference = ref

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _DocRef:
    def __init__(self, db, path: str):
        self.db = db
        self.path = path

    async def get(self):
        return _Snap(self.path, self.db.docs.get(self.path), self)

    async def set(self, data, merge=False):
        self.db.ops.append(("set", self.path))
        if merge:
            self.db.docs.setdefault(self.path, {}).update(data)
        else:
            self.db.docs[self.path] = dict(data)

    async def delete(self):
        self.db.ops.append(("delete", self.path))
        self.db.docs.pop(self.path, None)

    def collection(self, name: str):
        return _CollRef(self.db, f"{self.path}/{name}")


class _Query:
    def __init__(self, db, path: str, predicate=None):
        self.db = db
        self.path = path
        self._predicate = predicate

    def _members(self):
        prefix = f"{self.path}/"
        for path, data in sorted(self.db.docs.items()):
            if not path.startswith(prefix) or "/" in path[len(prefix) :]:
                continue
            if self._predicate and not self._predicate(data):
                continue
            yield path, data

    async def stream(self):
        self.db.streams.append(self.path)
        for path, data in list(self._members()):
            yield _Snap(path, data, _DocRef(self.db, path))


class _CollRef(_Query):
    def document(self, doc_id: str) -> _DocRef:
        return _DocRef(self.db, f"{self.path}/{doc_id}")

    def where(self, filter=None) -> _Query:
        assert filter.op_string == "==", filter.op_string
        field, value = filter.field_path, filter.value
        return _Query(self.db, self.path, lambda d: d.get(field) == value)


class _Batch:
    def __init__(self, db):
        self.db = db
        self._refs: list[_DocRef] = []

    def delete(self, ref):
        self._refs.append(ref)

    async def commit(self):
        self.db.ops.append(("batch_delete", tuple(r.path for r in self._refs)))
        for ref in self._refs:
            self.db.docs.pop(ref.path, None)


class _FakeDB:
    """A flat ``path -> data`` store, recording every mutation in order."""

    def __init__(self, docs: dict[str, dict]):
        self.docs = dict(docs)
        self.ops: list[tuple] = []
        self.streams: list[str] = []

    def collection(self, name: str) -> _CollRef:
        return _CollRef(self, name)

    def batch(self) -> _Batch:
        return _Batch(self)


class _FakeBlob:
    def __init__(self, bucket, name: str):
        self.bucket = bucket
        self.name = name

    def delete(self):
        self.bucket.names.remove(self.name)
        self.bucket.deleted.append(self.name)


class _FakeBucket:
    def __init__(self, name: str, names: list[str]):
        self.name = name
        self.names = list(names)
        self.deleted: list[str] = []
        self.prefixes: list[str] = []

    def list_blobs(self, prefix: str):
        self.prefixes.append(prefix)
        return [_FakeBlob(self, n) for n in self.names if n.startswith(prefix)]


class _UserNotFound(Exception):
    pass


class _FakeAuth:
    """Just the two calls this feature makes on ``firebase_admin.auth``."""

    UserNotFoundError = _UserNotFound

    def __init__(self, email: str | None, ops: list, *, exists: bool = True):
        self.email = email
        self.ops = ops
        self.exists = exists

    def get_user(self, uid: str):
        if not self.exists:
            raise _UserNotFound(f"no user record for {uid}")
        return SimpleNamespace(uid=uid, email=self.email)

    def delete_user(self, uid: str):
        self.ops.append(("auth_delete", uid))
        if not self.exists:
            raise _UserNotFound(f"no user record for {uid}")
        self.exists = False


@pytest.fixture
def world(monkeypatch):
    """One user with data in every place the wipe reaches, plus a bystander."""
    db = _FakeDB(
        {
            "users/u1": {"email": "User@Example.com", "full_name": "Test User"},
            "users/u1/jobs/j1": {"title": "a"},
            "users/u1/jobs/j2": {"title": "b"},
            "users/u1/applications/a1": {"status": "ready_for_review"},
            "users/u1/discarded_jobs/d1": {"score": 12},
            "users/u1/runs/r1": {"cost_usd": 0.03},
            "users/u1/company_prefs/greenhouse:stripe": {"state": "excluded"},
            "users/u2": {"email": "other@example.com"},
            "users/u2/jobs/j9": {"title": "not mine"},
            "batch_runs/b1": {"user_id": "u1", "state": "running"},
            "batch_runs/b2": {"user_id": "u2", "state": "running"},
            # Shared, content-keyed, and nobody's personal data.
            "jd_cache/sha-abc": {"parsed": {}},
        }
    )
    bucket = _FakeBucket(
        "test-resumes",
        [
            "users/u1/resume.docx",
            "users/u1/screenshots/j1.png",
            "users/u2/resume.docx",
            # Outside users/, which is the whole reason PR B put it there.
            "board_cache/greenhouse/stripe.json",
        ],
    )
    monkeypatch.setenv("RESUME_BUCKET", bucket.name)
    monkeypatch.setattr(
        storage, "Client", lambda: SimpleNamespace(bucket=lambda n: bucket)
    )
    return SimpleNamespace(db=db, bucket=bucket)


def _wipe(world, *, execute: bool):
    return asyncio.run(account_delete.wipe_user_data(world.db, "u1", execute=execute))


# ---------------------------------------------------------------------------
# is_deleted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (None, False),
        ({}, False),
        ({"email": "a@b.c"}, False),
        ({"deleted_at": DELETED_AT}, True),
        # An empty/absent value is not a tombstone: the field is written once,
        # with a timestamp, and never blanked.
        ({"deleted_at": ""}, False),
        ({"deleted_at": None}, False),
    ],
)
def test_is_deleted_reads_the_tombstone(doc, expected):
    assert account_delete.is_deleted(doc) is expected


# ---------------------------------------------------------------------------
# wipe_user_data
# ---------------------------------------------------------------------------
def test_a_dry_run_counts_everything_and_writes_nothing(world):
    """The CLI's default, and the reason it is safe to type."""
    counts = _wipe(world, execute=False)

    assert counts.as_dict() == {
        "jobs": 2,
        "applications": 1,
        "discarded_jobs": 1,
        "runs": 1,
        "company_prefs": 1,
        "batch_runs": 1,
        "gcs_blobs": 2,
        "user_doc_existed": True,
    }
    assert world.db.ops == []
    assert world.bucket.deleted == []
    assert "users/u1/jobs/j1" in world.db.docs


def test_an_executed_wipe_erases_this_users_data(world):
    counts = _wipe(world, execute=True)

    assert counts.jobs == 2 and counts.gcs_blobs == 2
    assert counts.user_doc_existed is True
    assert not [p for p in world.db.docs if p.startswith("users/u1")]
    assert "batch_runs/b1" not in world.db.docs
    assert sorted(world.bucket.deleted) == [
        "users/u1/resume.docx",
        "users/u1/screenshots/j1.png",
    ]


def test_it_stops_at_this_users_boundary(world):
    """Another user keeps their documents, their ``batch_runs`` row (matched on
    the ``user_id`` field, not a prefix) and their blobs."""
    _wipe(world, execute=True)

    assert world.db.docs["users/u2"] == {"email": "other@example.com"}
    assert "users/u2/jobs/j9" in world.db.docs
    assert world.db.docs["batch_runs/b2"]["user_id"] == "u2"
    assert "users/u2/resume.docx" in world.bucket.names


def test_the_shared_caches_are_not_evicted(world):
    """``jd_cache`` and ``board_cache/`` are content-keyed and cross-user: a JD
    parsed once is reused by whoever sees that posting next. Deleting one
    account must not charge every remaining user a re-parse."""
    _wipe(world, execute=True)

    assert "jd_cache/sha-abc" in world.db.docs
    assert "board_cache/greenhouse/stripe.json" in world.bucket.names
    # Not "we happened not to delete it": nothing even looked at those spaces.
    assert not any(s.startswith("jd_cache") for s in world.db.streams)
    assert world.bucket.prefixes == ["users/u1/"]


def test_the_user_document_is_deleted_last(world):
    """It is the index into everything else — the profile, the settings, the
    tombstone the loops read. A wipe interrupted halfway has to leave an account
    that is still findable and still refusing work, so a re-run can finish it.
    """
    _wipe(world, execute=True)

    destructive = [op for op in world.db.ops if op[0] in {"delete", "batch_delete"}]
    assert destructive[-1] == ("delete", "users/u1")
    assert len(destructive) > 1, "nothing else was deleted — this pins nothing"


def test_a_wipe_of_an_already_wiped_account_is_a_no_op(world):
    """Re-runnable, because that is the whole mitigation for the window
    ``delete_account`` cannot close."""
    _wipe(world, execute=True)
    world.db.ops.clear()

    counts = _wipe(world, execute=True)

    assert counts.as_dict() == {
        "jobs": 0,
        "applications": 0,
        "discarded_jobs": 0,
        "runs": 0,
        "company_prefs": 0,
        "batch_runs": 0,
        "gcs_blobs": 0,
        "user_doc_existed": False,
    }
    assert world.db.ops == []


def test_deletes_are_chunked_under_the_firestore_batch_cap(world):
    """500 writes per batch is a hard limit; a demo account holds thousands of
    jobs (one of the real ones holds 1,261)."""
    for i in range(501):
        world.db.docs[f"users/u1/jobs/big{i:04d}"] = {"title": "x"}

    counts = _wipe(world, execute=True)

    assert counts.jobs == 503
    sizes = [len(paths) for op, paths in world.db.ops if op == "batch_delete"]
    assert sizes[:2] == [500, 3]
    assert max(sizes) <= 500


# ---------------------------------------------------------------------------
# delete_account: the ordering
# ---------------------------------------------------------------------------
def _delete_account(world, *, auth: _FakeAuth | None = None):
    auth = auth or _FakeAuth("user@example.com", world.db.ops)
    counts = asyncio.run(
        account_delete.delete_account(
            world.db, "u1", close_auth=auth.delete_user, now=None
        )
    )
    return counts, auth


def test_the_tombstone_and_the_auth_close_come_before_any_destruction(world):
    """The order *is* the design. The tombstone stops new cycles being
    dispatched and closing the Auth account stops new API calls; both have to
    land before anything is erased, or the account keeps refilling behind the
    wipe."""
    _delete_account(world)

    ops = world.db.ops
    assert ops[0] == ("set", "users/u1"), "the tombstone was not written first"
    assert ops[1] == ("auth_delete", "u1"), "data was destroyed before the login"
    first_destructive = next(
        i for i, op in enumerate(ops) if op[0] in {"delete", "batch_delete"}
    )
    assert first_destructive > 1
    assert ops[-1] == ("delete", "users/u1")


def test_the_tombstone_is_a_merge_not_a_replacement(world):
    """It is written onto a live document — one a cycle may be reading — and it
    only adds the field the loops check."""
    tombstoned: list[dict] = []

    def capture(uid):
        tombstoned.append(dict(world.db.docs["users/u1"]))

    asyncio.run(account_delete.delete_account(world.db, "u1", close_auth=capture))

    assert tombstoned[0]["full_name"] == "Test User"
    assert account_delete.is_deleted(tombstoned[0])


def test_the_wipe_still_runs_when_the_login_is_already_gone(world):
    """A Firebase ID token stays verifiable for up to an hour after the account
    it names is deleted, so retrying a half-finished deletion is a reachable
    path. ``close_auth`` owns the idempotence; this pins that a caller which
    swallows "already gone" gets the rest of the wipe."""
    auth = _FakeAuth("user@example.com", world.db.ops, exists=False)

    def close(uid):
        try:
            auth.delete_user(uid)
        except _UserNotFound:
            pass

    asyncio.run(account_delete.delete_account(world.db, "u1", close_auth=close))

    assert "users/u1" not in world.db.docs
    assert not [p for p in world.db.docs if p.startswith("users/u1/")]


def test_a_close_auth_that_raises_stops_the_wipe(world):
    """The other side of it: if the login could not be closed for a reason that
    is *not* "already gone", the account is still reachable, and erasing its
    data underneath a live session is worse than stopping."""

    def unreachable(uid):
        raise RuntimeError("the Admin SDK is unreachable")

    with pytest.raises(RuntimeError):
        asyncio.run(
            account_delete.delete_account(world.db, "u1", close_auth=unreachable)
        )

    assert "users/u1/jobs/j1" in world.db.docs
    # The tombstone did land, so the loops are already refusing this account.
    assert account_delete.is_deleted(world.db.docs["users/u1"])


# ---------------------------------------------------------------------------
# The typed confirmation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("", ""),  # the pair that would confirm itself
        ("   ", "  "),  # ...and its whitespace-only twin
        ("", "user@example.com"),
        ("user@example.com", ""),
        ("someone@else.com", "user@example.com"),
        ("user@example.com.uk", "user@example.com"),
    ],
)
def test_nothing_empty_or_different_confirms_anything(typed, expected):
    """Empty must never compare equal to empty. It is the default value of an
    untouched input *and* what an account with no address on file would supply
    as the thing to match — two empties meeting would turn this endpoint into a
    bare POST."""
    assert account._confirms(typed, expected) is False


@pytest.mark.parametrize(
    "typed", ["user@example.com", "  User@Example.COM  ", "USER@EXAMPLE.COM"]
)
def test_the_address_the_user_read_off_the_screen_confirms(typed):
    assert account._confirms(typed, "User@Example.com") is True


# ---------------------------------------------------------------------------
# POST /account/delete
# ---------------------------------------------------------------------------
@pytest.fixture
def api(world, monkeypatch):
    auth = _FakeAuth("User@Example.com", world.db.ops)
    monkeypatch.setattr(account, "_client", lambda: world.db)
    monkeypatch.setattr(account, "firebase_auth", lambda: auth)
    app = FastAPI()
    app.include_router(account.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(client=TestClient(app), auth=auth, world=world)


def test_the_typed_confirmation_deletes_the_account(api):
    resp = api.client.post("/account/delete", json={"confirm": "User@Example.com"})

    assert resp.status_code == 200
    assert resp.json()["deleted"]["jobs"] == 2
    assert "users/u1" not in api.world.db.docs
    assert api.auth.exists is False


def test_the_confirmation_tolerates_case_and_whitespace(api):
    """The user is retyping something they read off the screen."""
    resp = api.client.post("/account/delete", json={"confirm": "  user@EXAMPLE.com "})

    assert resp.status_code == 200
    assert "users/u1" not in api.world.db.docs


def test_a_wrong_confirmation_deletes_nothing(api):
    resp = api.client.post("/account/delete", json={"confirm": "someone@else.com"})

    assert resp.status_code == 400
    assert api.world.db.ops == [], "a refused request must not write at all"
    assert api.auth.exists is True
    assert "users/u1/jobs/j1" in api.world.db.docs


def test_an_empty_confirmation_is_not_a_confirmation(api):
    """The default value of an untouched input. A comparison that let this
    through would make the endpoint a bare POST."""
    resp = api.client.post("/account/delete", json={"confirm": "   "})

    assert resp.status_code == 400
    assert api.world.db.ops == []
    assert api.auth.exists is True


@pytest.mark.parametrize("stored", [None, "", "   "])
def test_an_account_with_no_address_anywhere_cannot_be_typed_open(api, stored):
    """Nothing to type means nothing to confirm. A blank profile ``email`` is
    the interesting one: it is a *string*, so it survives a "did we find an
    address?" check that only tests for ``None``, and then arrives at the
    comparison as an empty expected value."""
    api.auth.email = None
    if stored is None:
        api.world.db.docs["users/u1"].pop("email")
    else:
        api.world.db.docs["users/u1"]["email"] = stored

    resp = api.client.post("/account/delete", json={"confirm": ""})

    assert resp.status_code == 400
    # Specifically the "nothing to confirm against" answer, not the mismatch
    # one: a blank stored address that reaches the comparison at all is the bug.
    assert "no email address" in resp.json()["detail"]
    assert api.world.db.ops == []
    assert "users/u1/jobs/j1" in api.world.db.docs


def test_a_retry_after_the_login_was_already_closed_finishes_the_wipe(api):
    """The token outlives the account by up to an hour, so this is the shape of
    a deletion that failed partway and is being retried. The address comes off
    the profile document, and ``delete_user`` raising "already gone" does not
    stop the wipe behind it."""
    api.auth.exists = False

    resp = api.client.post("/account/delete", json={"confirm": "user@example.com"})

    assert resp.status_code == 200
    assert ("auth_delete", "u1") in api.world.db.ops
    assert not [p for p in api.world.db.docs if p.startswith("users/u1")]


def test_a_second_delete_finds_nothing_left(api):
    api.client.post("/account/delete", json={"confirm": "User@Example.com"})
    api.world.db.ops.clear()

    resp = api.client.post("/account/delete", json={"confirm": "User@Example.com"})

    assert resp.status_code == 404
    assert api.world.db.ops == []


# ---------------------------------------------------------------------------
# The background loops refuse a tombstoned account
# ---------------------------------------------------------------------------
def _ref(doc: dict | None):
    """A sync ``users/{uid}`` reference, as ``discovery._user_ref`` returns."""
    return SimpleNamespace(get=lambda: SimpleNamespace(to_dict=lambda: doc))


def _explode(*args, **kwargs):
    raise AssertionError("a deleted account must not reach this")


async def _explode_async(*args, **kwargs):
    raise AssertionError("a deleted account must not reach this")


@pytest.fixture
def tombstoned(monkeypatch):
    """Every seam past the refusal wired to fail, for both cycles."""
    monkeypatch.setattr(
        discovery, "_user_ref", lambda uid: _ref({"deleted_at": DELETED_AT})
    )
    monkeypatch.setattr(discovery, "_extend_slot", _explode)
    monkeypatch.setattr(discovery, "_release_slot", _explode)
    monkeypatch.setattr(discovery, "run_discovery", _explode_async)
    monkeypatch.setattr(discovery, "sweep_postings", _explode_async)
    # In the cycle's ``finally``: reaching it would mean the refusal came too
    # late and a run context had already been opened.
    monkeypatch.setattr(discovery, "persist_run_cost", _explode_async)


def test_a_deleted_account_gets_no_discovery_cycle(tombstoned):
    """A task already on the queue when the user deleted themselves arrives at
    the cycle holding nothing but a user id, so the check has to be here — and
    ahead of every write, because the cycle's own success write is a
    ``set(..., merge=True)`` that would recreate the deleted document."""
    assert asyncio.run(discovery.run_discovery_cycle("u1", trigger="cron")) is None


def test_a_deleted_account_gets_no_sweep_cycle(tombstoned):
    assert asyncio.run(discovery.run_sweep_cycle("u1", trigger="cron")) is None


@pytest.mark.parametrize(
    "cycle", [discovery.run_discovery_cycle, discovery.run_sweep_cycle]
)
def test_a_live_account_still_gets_its_cycle(cycle, monkeypatch):
    """Positive control: the guard is the tombstone, not the cycle."""
    reached: list[str] = []

    async def fake_work(user_id, *args, **kwargs):
        reached.append(user_id)
        raise RuntimeError("far enough")

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        pass

    monkeypatch.setattr(discovery, "_user_ref", lambda uid: _ref({"email": "a@b.c"}))
    monkeypatch.setattr(discovery, "_extend_slot", lambda *a, **kw: True)
    monkeypatch.setattr(discovery, "_release_slot", lambda *a, **kw: True)
    monkeypatch.setattr(discovery, "run_discovery", fake_work)
    monkeypatch.setattr(discovery, "sweep_postings", fake_work)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)

    asyncio.run(cycle("u1", trigger="cron"))

    assert reached == ["u1"]


def test_a_tick_on_a_deleted_account_claims_no_slot(monkeypatch):
    """Checked on the document the caller already holds — a claim is a write and
    a dispatch is a crawl, so neither should be reached."""
    monkeypatch.setattr(discovery, "_claim_slot", _explode)
    monkeypatch.setattr(discovery, "dispatch_cycle", _explode_async)
    monkeypatch.setattr(discovery, "_last_tick_check", {})
    doc = {
        "deleted_at": DELETED_AT,
        "discovery_settings": {"auto_discovery": True, "liveness_sweep": True},
    }

    asyncio.run(discovery.tick_user("u1", force_check=True, doc=doc))


def test_a_tick_on_a_live_account_still_claims(monkeypatch):
    claimed: list[str] = []
    monkeypatch.setattr(discovery, "_last_tick_check", {})
    monkeypatch.setattr(
        discovery,
        "_claim_slot",
        lambda uid, kind, hours, now: bool(claimed.append(kind)),
    )
    doc = {"discovery_settings": {"auto_discovery": True, "liveness_sweep": True}}

    asyncio.run(discovery.tick_user("u1", force_check=True, doc=doc))

    assert claimed == ["discovery", "sweep"]


@pytest.fixture
def cron(monkeypatch):
    """``cron_tick`` over one live user and one tombstoned one."""
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.setenv("QUEUE_MODE", "1")
    ticked: list[str] = []
    reaped: list[str] = []

    async def fake_tick(user_id, *, force_check=False, doc=None):
        ticked.append(user_id)

    async def fake_reap(user_id, *, background_tasks):
        reaped.append(user_id)
        return {"recovered": 0, "truncated": 0}

    docs = {"u1": {"deleted_at": DELETED_AT}, "u2": {"discovery_settings": {}}}
    users = SimpleNamespace(
        collection=lambda name: SimpleNamespace(
            stream=lambda: [
                SimpleNamespace(id=uid, to_dict=lambda d=d: d)
                for uid, d in docs.items()
            ]
        )
    )
    monkeypatch.setattr(discovery, "tick_user", fake_tick)
    monkeypatch.setattr(discovery, "reap_user", fake_reap)
    monkeypatch.setattr(discovery, "maybe_enqueue_batch_resume", lambda: False)
    monkeypatch.setattr(discovery, "_client", lambda: users)
    return SimpleNamespace(ticked=ticked, reaped=reaped, docs=docs)


def test_the_cron_fan_out_skips_a_deleted_account_whole(cron):
    """This fan-out is the one caller that reaches a user without passing
    ``verify_user``: it streams *every* document in ``users``. The reaper is
    skipped with the tick — it would otherwise dispatch tailoring for a user who
    is leaving."""
    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert cron.ticked == ["u2"]
    assert cron.reaped == ["u2"]
    assert result["deleted"] == 1
    assert result["users"] == 2 and result["failed"] == 0


def test_a_deleted_account_cannot_make_a_dead_fan_out_look_alive(cron, monkeypatch):
    """The 500 is counted against the users the tick actually *tried*. Counting
    a skipped account as a success would mask a fan-out where every real user's
    tick failed — the "hourly 200 that does nothing" this endpoint answers 5xx
    to avoid."""

    async def explode(user_id, *, force_check=False, doc=None):
        raise RuntimeError("TASKS_SA_EMAIL is not set")

    monkeypatch.setattr(discovery, "tick_user", explode)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert raised.value.status_code == 500


# --------------------------------------------------------------------------
# The list of subcollections is hand-maintained, and that is the risk.
# --------------------------------------------------------------------------


def test_a_deleted_account_leaves_no_company_prefs_behind(world):
    """``company_prefs`` was added by a later PR than the wipe and was missed.

    Nothing failed and no test went red: the wipe simply walked a list that
    predated the subcollection, and a deleted account kept its exclusions.
    Firestore keeps subcollections alive when the parent document is deleted,
    so those documents would have survived every trace of the account that
    owned them.
    """
    assert "users/u1/company_prefs/greenhouse:stripe" in world.db.docs

    counts = _wipe(world, execute=True)

    assert "users/u1/company_prefs/greenhouse:stripe" not in world.db.docs
    assert counts.company_prefs == 1


def test_every_subcollection_the_code_writes_is_one_the_wipe_deletes():
    """Guards the *class* of bug, not the one instance of it.

    A new ``users/{uid}/<name>`` collection is added by whichever feature needs
    it, and nothing links that back to here — so this asserts the wipe's list
    against the constants those modules export. Adding a subcollection without
    adding it to ``USER_SUBCOLLECTIONS`` fails this, rather than quietly
    orphaning documents on every future deletion.
    """
    from tools.account.delete import USER_SUBCOLLECTIONS
    from tools.company_prefs import COLLECTION as COMPANY_PREFS
    from tools.run_costs import COLLECTION as RUN_COSTS

    for owned in (RUN_COSTS, COMPANY_PREFS):
        assert owned in USER_SUBCOLLECTIONS, (
            f"{owned!r} is written under users/{{uid}} but the wipe would "
            f"leave it behind"
        )
