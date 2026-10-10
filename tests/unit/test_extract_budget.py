# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The daily cap on ``POST /profile/extract`` — five résumé extractions per
user per UTC day.

What matters: the slot is charged in a transaction *before* Gemini, a refused
request never reaches Gemini, a pre-model failure gives the slot back, and a
failure after the model call does not. Gemini is patched in every test.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firestore_fakes import FakeSyncTransaction, _FakeSyncDB, apply_set
from google.api_core.exceptions import Aborted
from test_api_profile import _profile_payload

import api.routes.profile as profile_mod
from api.deps import verify_user
from models.profile import MasterProfile
from tools.profile import extract_budget
from tools.profile.extract_budget import Limits, apply_charge, apply_refund

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
LIMITS = Limits(per_day=5)


def _today() -> str:
    return extract_budget.day_key(datetime.now(UTC))


# ------------------------------------------------------------------ pure core


def test_the_first_charge_of_the_day_is_granted():
    state, charge = apply_charge(None, now=NOW, limits=LIMITS)
    assert charge.granted and charge.charged and charge.used == 1
    assert state == {"day": "2026-10-09", "count": 1, "updated_at": NOW.isoformat()}


def test_the_fifth_is_granted_and_the_sixth_refused_without_a_write():
    state, charge = apply_charge(
        {"day": "2026-10-09", "count": 4}, now=NOW, limits=LIMITS
    )
    assert charge.granted and state is not None and state["count"] == 5

    state, charge = apply_charge(state, now=NOW, limits=LIMITS)
    assert not charge.granted and not charge.charged
    assert state is None
    assert charge.used == 5 and charge.per_day == 5
    assert charge.resets_at == "2026-10-10T00:00:00+00:00"


def test_the_counter_rolls_over_at_utc_midnight():
    last_second = datetime(2026, 10, 9, 23, 59, 59, tzinfo=UTC)
    spent = {"day": "2026-10-09", "count": 5}
    assert not apply_charge(spent, now=last_second, limits=LIMITS)[1].granted

    midnight = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    state, charge = apply_charge(spent, now=midnight, limits=LIMITS)
    assert charge.granted and charge.used == 1
    assert state == {
        "day": "2026-10-10",
        "count": 1,
        "updated_at": midnight.isoformat(),
    }


def test_the_day_is_utc_not_the_callers_offset():
    """23:30 in New York on the 9th is already the 10th in UTC."""
    from zoneinfo import ZoneInfo

    local = datetime(2026, 10, 9, 23, 30, tzinfo=ZoneInfo("America/New_York"))
    assert extract_budget.day_key(local) == "2026-10-10"


@pytest.mark.parametrize("garbage", ["x", None, -7, {"a": 1}])
def test_garbage_counters_never_grant_more(garbage):
    _, charge = apply_charge(
        {"day": "2026-10-09", "count": garbage}, now=NOW, limits=LIMITS
    )
    assert charge.used == 1


def test_a_refund_gives_the_slot_back_and_floors_at_zero():
    assert (
        apply_refund({"day": "2026-10-09", "count": 3}, now=NOW, day="2026-10-09")[
            "count"
        ]
        == 2
    )
    assert (
        apply_refund({"day": "2026-10-09", "count": 0}, now=NOW, day="2026-10-09")[
            "count"
        ]
        == 0
    )


def test_a_refund_after_the_day_rolled_credits_nothing():
    assert (
        apply_refund({"day": "2026-10-10", "count": 1}, now=NOW, day="2026-10-09")
        is None
    )
    assert apply_refund(None, now=NOW, day="2026-10-09") is None


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 5), ("", 5), ("8", 8), ("0", 0), ("-3", 0), ("lots", 5)],
)
def test_limits_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PROFILE_EXTRACT_PER_DAY", raising=False)
    else:
        monkeypatch.setenv("PROFILE_EXTRACT_PER_DAY", raw)
    assert Limits.from_env().per_day == expected


# ------------------------------------------------------- transaction shells


def _db(state: dict | None = None, **kw) -> _FakeSyncDB:
    db = _FakeSyncDB(**kw)
    if state is not None:
        db.store[extract_budget.FIELD] = state
    return db


def test_charge_persists_the_counter():
    db = _db()
    assert extract_budget.charge(db, "u1", limits=LIMITS).granted
    assert db.store[extract_budget.FIELD]["count"] == 1
    assert db.store[extract_budget.FIELD]["day"] == _today()


def test_a_refused_charge_writes_nothing():
    db = _db({"day": _today(), "count": 5, "updated_at": "earlier"})
    charge = extract_budget.charge(db, "u1", limits=LIMITS)
    assert not charge.granted
    assert db.store[extract_budget.FIELD]["updated_at"] == "earlier"
    assert db.transactions[0].commits == 1  # a read-only commit, nothing buffered


def test_a_contended_charge_retries_without_double_charging():
    db = _db(abort_once=True)
    assert extract_budget.charge(db, "u1", limits=LIMITS).granted
    assert db.store[extract_budget.FIELD]["count"] == 1


def test_charge_with_the_cap_off_touches_no_firestore():
    class _ExplodingDB:
        def collection(self, name):
            raise AssertionError("touched Firestore with the cap off")

    charge = extract_budget.charge(_ExplodingDB(), "u1", limits=Limits(per_day=0))
    assert charge.granted and not charge.charged and charge.per_day is None


def test_charge_failures_propagate():
    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    with pytest.raises(RuntimeError):
        extract_budget.charge(_BrokenDB(), "u1", limits=LIMITS)


def test_refund_never_raises():
    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    extract_budget.refund(_BrokenDB(), "u1", day=_today())


# -------------------------------------------------------------- concurrency


class _VersionedStore:
    """One user document with optimistic concurrency, as Firestore has: a
    transaction whose read is stale by commit time aborts and retries."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.version = 0
        self.lock = threading.Lock()


class _VersionedSnap:
    exists = True

    def __init__(self, doc):
        self._doc = doc

    def to_dict(self):
        return dict(self._doc)


class _VersionedDoc:
    def __init__(self, store: _VersionedStore, barrier: threading.Barrier):
        self._store = store
        self._barrier = barrier

    def get(self, transaction=None):
        with self._store.lock:
            snap = _VersionedSnap(dict(self._store.doc))
            if transaction is not None:
                transaction.read_version = self._store.version
        if transaction is not None and not transaction.waited:
            # Every first attempt reads before any commits: the race itself.
            transaction.waited = True
            self._barrier.wait(timeout=5)
        return snap

    def set(self, data, merge=False):
        self._store.doc = apply_set(self._store.doc, data, merge)
        self._store.version += 1


class _VersionedTransaction(FakeSyncTransaction):
    _max_attempts = 20

    def __init__(self, store: _VersionedStore):
        super().__init__()
        self._store = store
        self.read_version = -1
        self.waited = False

    def _apply(self):
        with self._store.lock:
            if self._buffered and self.read_version != self._store.version:
                self._buffered = []
                raise Aborted("contended")
            return super()._apply()


class _VersionedDB:
    def __init__(self, store: _VersionedStore, threads: int):
        self._store = store
        self._barrier = threading.Barrier(threads)

    def collection(self, name):
        assert name == "users"
        doc = _VersionedDoc(self._store, self._barrier)
        return type("_Coll", (), {"document": lambda self, uid: doc})()

    def transaction(self):
        return _VersionedTransaction(self._store)


def test_concurrent_charges_never_exceed_the_cap():
    """Seven uploads read the same count at once; exactly five get through."""
    store = _VersionedStore({extract_budget.FIELD: {"day": _today(), "count": 0}})
    db = _VersionedDB(store, threads=7)
    results: list[bool] = []

    def worker():
        results.append(extract_budget.charge(db, "u1", limits=LIMITS).granted)

    threads = [threading.Thread(target=worker) for _ in range(7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [False, False, True, True, True, True, True]
    assert store.doc[extract_budget.FIELD]["count"] == 5


# --------------------------------------------------------------- the route


@pytest.fixture
def route(monkeypatch):
    """``POST /profile/extract`` on a fake user doc, with Gemini patched to
    record its calls and the run ledger stubbed out."""
    monkeypatch.delenv("PROFILE_EXTRACT_PER_DAY", raising=False)
    db = _db()
    monkeypatch.setattr(profile_mod, "_client", lambda: db)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(profile_mod, "open_run", _noop)
    monkeypatch.setattr(profile_mod, "persist_run_cost", _noop)

    calls: list[str] = []
    outcome: dict = {"raise": None}

    def fake_extract(text, user_id):
        calls.append(text)
        if outcome["raise"] is not None:
            raise outcome["raise"]
        return MasterProfile.model_validate(_profile_payload())

    monkeypatch.setattr(profile_mod, "extract_profile", fake_extract)

    app = FastAPI()
    app.include_router(profile_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"

    class _Route:
        client = TestClient(app, raise_server_exceptions=False)

        def __init__(self):
            self.db = db
            self.calls = calls
            self.outcome = outcome

        def post(self, text="Jane Doe, engineer", **kw):
            if "files" in kw:
                return self.client.post("/profile/extract", files=kw["files"])
            return self.client.post("/profile/extract", data={"text": text})

        @property
        def count(self) -> int:
            return self.db.store.get(extract_budget.FIELD, {}).get("count", 0)

    return _Route()


def test_a_successful_extract_charges_one_slot(route):
    resp = route.post()
    assert resp.status_code == 200
    assert route.count == 1
    assert len(route.calls) == 1


def test_the_sixth_extract_of_the_day_is_429_and_never_reaches_gemini(route):
    for _ in range(5):
        assert route.post().status_code == 200
    assert len(route.calls) == 5

    before = datetime.now(UTC)
    resp = route.post()
    after = datetime.now(UTC)

    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["reason"] == "extract_cap"
    assert detail["used"] == 5 and detail["per_day"] == 5
    # Either side of the request, so a run straddling UTC midnight still passes.
    resets = {
        f"{(t + timedelta(days=1)).date().isoformat()}T00:00:00+00:00"
        for t in (before, after)
    }
    assert detail["resets_at"] in resets
    assert len(route.calls) == 5  # Gemini not called for the refusal
    assert route.count == 5


def test_a_refused_request_does_not_read_the_upload_or_call_gemini(route, monkeypatch):
    route.db.store[extract_budget.FIELD] = {"day": _today(), "count": 5}

    def explode(*args, **kwargs):
        raise AssertionError("read the upload on a refused request")

    monkeypatch.setattr(profile_mod, "read_resume_text", explode)
    route.outcome["raise"] = AssertionError("Gemini called on a refused request")

    resp = route.post(files={"file": ("cv.pdf", b"%PDF", "application/pdf")})

    assert resp.status_code == 429
    assert route.calls == []


def test_yesterdays_spent_counter_does_not_block_today(route):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    route.db.store[extract_budget.FIELD] = {"day": yesterday, "count": 5}

    assert route.post().status_code == 200
    assert route.db.store[extract_budget.FIELD] == {
        "day": _today(),
        "count": 1,
        "updated_at": route.db.store[extract_budget.FIELD]["updated_at"],
    }


@pytest.mark.parametrize(
    "kwargs, status",
    [
        ({"text": "   "}, 400),  # nothing to read
        ({"files": {"file": ("cv.txt", b"   \n ", "text/plain")}}, 422),  # empty
    ],
)
def test_a_pre_model_failure_refunds_the_slot(route, kwargs, status):
    route.db.store[extract_budget.FIELD] = {"day": _today(), "count": 2}

    resp = route.post(**kwargs)

    assert resp.status_code == status
    assert route.count == 2
    assert route.calls == []


def test_an_oversized_upload_refunds(route, monkeypatch):
    monkeypatch.setattr(profile_mod, "MAX_RESUME_BYTES", 4)
    resp = route.post(files={"file": ("cv.txt", b"far too long", "text/plain")})
    assert resp.status_code == 413
    assert route.count == 0


def test_a_parser_crash_refunds(route, monkeypatch):
    def broken(raw, filename):
        raise ValueError("not a PDF")

    monkeypatch.setattr(profile_mod, "read_resume_text", broken)

    resp = route.post(files={"file": ("cv.pdf", b"junk", "application/pdf")})

    assert resp.status_code == 500
    assert route.count == 0
    assert route.calls == []


def test_a_failure_after_the_model_call_keeps_the_charge(route):
    """Gemini was called, so the money is spent and the slot stays used."""
    route.outcome["raise"] = ValueError("schema validation failed")

    resp = route.post()

    assert resp.status_code == 422
    assert len(route.calls) == 1
    assert route.count == 1


def test_zero_means_uncapped(route, monkeypatch):
    monkeypatch.setenv("PROFILE_EXTRACT_PER_DAY", "0")
    route.db.store[extract_budget.FIELD] = {"day": _today(), "count": 99}

    resp = route.post()

    assert resp.status_code == 200
    assert route.count == 99  # not charged, not even read in a transaction
    assert route.db.transactions == []
