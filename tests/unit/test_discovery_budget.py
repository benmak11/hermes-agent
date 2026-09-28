# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The weekly discovery cap: 14 searches a week, charged at dispatch.

Finding jobs is free of LLM cost but not free of everything — it is a crawl of
~198 boards per run, it is what fills the backlog the scoring budget then has
to cap, and it is the number the product promises. This is its cap, and it is
built as the mirror of ``tools.matching.budget``: a pure core (rollover,
clamping, refund windowing) with a transaction as a thin shell.

Three things are easy to get wrong here and each has its own test:

1. **The week key must carry the ISO year.** ``isocalendar()[1]`` alone
   collapses week 1 of one year onto week 1 of the next, and ``now.year``
   splits a single ISO week across a New Year.
2. **The charge happens at dispatch, never inside the cycle** — the worker's
   task handlers must not charge, or a Cloud Tasks redelivery bills a second
   run for one user-visible search.
3. **A capped tick must cost nothing**: no lease, no write.

Zero LLM calls, zero GCP: the Firestore transaction is faked at the protocol
the real decorators drive, so their own retry behaviour is exercised rather
than stubbed out.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# The one valid MasterProfile body in the suite — a hand-written stand-in
# would 422 and the kickoff under test would never be reached.
from firestore_fakes import _FakeDB, _FakeSyncDB
from test_api_profile import _profile_payload

import api.routes.discovery as discovery
import api.routes.profile as profile
import api.routes.worker as worker
from api.deps import verify_user
from tools.discovery import budget
from tools.discovery import budget as discovery_budget
from tools.discovery.budget import Limits, apply_release, apply_reservation, week_key

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)  # a Wednesday
WEEK = "2026-W40"
LIMITS = Limits(per_week=14)


def _state(**kw) -> dict:
    base = {"week": WEEK, "runs_this_week": 0, "updated_at": NOW.isoformat()}
    base.update(kw)
    return base


def _reserve(state, wanted=1, *, now=NOW, limits=LIMITS):
    return apply_reservation(state, wanted, now=now, limits=limits)


# ------------------------------------------------------- the week boundary


def test_week_key_at_year_boundary():
    """Two failure modes, one function, and they pull in opposite directions.

    ``now.year`` splits an ISO week that spans New Year — 2026-12-31 and
    2027-01-01 are the *same* ISO week (2026-W53), and keying them apart would
    hand out a free week's searches on New Year's Day.

    A bare ``isocalendar()[1]`` does the reverse: 2026-01-02 and 2027-01-04 are
    both "week 1", a year apart, and keying them together would refuse a
    legitimate rollover to a user who came back a year later.
    """
    new_years_eve = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
    new_years_day = datetime(2027, 1, 1, 1, 0, tzinfo=UTC)
    assert week_key(new_years_eve, "UTC") == week_key(new_years_day, "UTC")
    assert week_key(new_years_eve, "UTC") == "2026-W53"

    assert week_key(datetime(2026, 1, 2, tzinfo=UTC), "UTC") == "2026-W01"
    assert week_key(datetime(2027, 1, 4, tzinfo=UTC), "UTC") == "2027-W01"
    assert week_key(datetime(2026, 1, 2, tzinfo=UTC), "UTC") != week_key(
        datetime(2027, 1, 4, tzinfo=UTC), "UTC"
    )

    # And the counter follows the key, which is what makes the above matter:
    # a user who spent New Year's Eve's allowance still has none on New
    # Year's Day, because it is the same ISO week.
    spent = {"week": week_key(new_years_eve, "UTC"), "runs_this_week": 14}
    _new, res = apply_reservation(spent, 1, now=new_years_day, limits=LIMITS)
    assert res.granted == 0


def test_week_start_is_the_monday_and_reset_is_an_instant():
    friday = datetime(2026, 10, 2, 17, 30, tzinfo=UTC)
    assert budget.week_start(friday, "UTC") == datetime(2026, 9, 28, tzinfo=UTC)
    # An ISO instant, never a formatted local time: the client renders it.
    assert budget.resets_at(friday, "UTC") == "2026-10-05T00:00:00+00:00"
    assert datetime.fromisoformat(budget.resets_at(friday, "UTC")).tzinfo is not None


def test_a_local_timezone_moves_the_boundary_without_a_refactor():
    """The seam, exercised: everything passes ``"UTC"`` today, and the day it
    doesn't, only the argument changes."""
    # 01:00 Monday UTC is still Sunday evening in New York, i.e. last week.
    monday_early = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
    assert week_key(monday_early, "UTC") == "2026-W40"
    assert week_key(monday_early, "America/New_York") == "2026-W39"


# ------------------------------------------------------- apply_reservation()


def test_first_ever_run_is_granted():
    new_state, res = _reserve(None)
    assert res.granted == 1 and res.capped is False
    assert res.remaining_week == 13 and res.week_key == WEEK
    assert new_state == {
        "week": WEEK,
        "runs_this_week": 1,
        "updated_at": NOW.isoformat(),
    }


def test_last_run_is_granted_once():
    """Two ticks racing for the last search of the week. The second applies
    against the state the first returned — which is what the transaction makes
    true — and gets nothing."""
    state = _state(runs_this_week=13)

    after_first, first = _reserve(state)
    _after_second, second = _reserve(after_first)

    assert (first.granted, second.granted) == (1, 0)
    assert (first.capped, second.capped) == (False, True)
    assert after_first["runs_this_week"] == 14


def test_an_exhausted_week_grants_nothing():
    new_state, res = _reserve(_state(runs_this_week=14))
    assert res.granted == 0 and res.capped is True and res.remaining_week == 0
    assert new_state["runs_this_week"] == 14


def test_last_weeks_key_grants_a_full_week():
    """Rollover is lazy and at read time: no cron resets this, a stored week
    that isn't this week simply reads as zero."""
    stale = {"week": "2026-W39", "runs_this_week": 14}

    new_state, res = _reserve(stale)

    assert res.granted == 1 and res.remaining_week == 13
    assert new_state == {
        "week": WEEK,
        "runs_this_week": 1,
        "updated_at": NOW.isoformat(),
    }


def test_garbage_counters_never_grant_more_than_the_limit():
    for bad in ({"runs_this_week": -50}, {"runs_this_week": "many"}):
        _new_state, res = _reserve(_state(**bad))
        assert res.granted == 1 and res.remaining_week == 13


def test_a_hand_raised_counter_still_grants_zero_not_negative():
    _new_state, res = _reserve(_state(runs_this_week=99))
    assert res.granted == 0 and res.remaining_week == 0


# ----------------------------------------------------------- remaining/used


def test_remaining_never_moves_a_counter():
    state = _state(runs_this_week=9)
    assert budget.remaining(state, now=NOW, limits=LIMITS) == 5
    assert budget.used(state, now=NOW) == 9
    # The read left the map exactly as it found it.
    assert state == _state(runs_this_week=9)


def test_remaining_applies_the_same_rollover_as_the_reservation():
    stale = {"week": "2026-W39", "runs_this_week": 14}
    assert budget.remaining(stale, now=NOW, limits=LIMITS) == 14
    assert budget.used(stale, now=NOW) == 0


def test_remaining_of_a_user_who_has_never_searched():
    assert budget.remaining(None, now=NOW, limits=LIMITS) == 14
    assert budget.used(None, now=NOW) == 0


# ------------------------------------------------------------ apply_release()


def test_release_gives_the_run_back():
    refunded = apply_release(_state(runs_this_week=3), now=NOW, week=WEEK)
    assert refunded is not None and refunded["runs_this_week"] == 2


def test_release_will_not_credit_a_different_week():
    """The refund is windowed: a dispatch charged last week must not credit
    this week's counter, or a cap becomes a way of earning searches."""
    assert apply_release(_state(runs_this_week=3), now=NOW, week="2026-W39") is None


def test_release_of_an_absent_counter_writes_nothing():
    """**The account-resurrection guard.** ``delete_account`` removes the user
    document; a refund from the cycle's deleted-account refusal that wrote
    anyway would recreate ``users/{uid}`` *without* its ``deleted_at``
    tombstone."""
    assert apply_release(None, now=NOW, week=WEEK) is None
    assert apply_release({}, now=NOW, week=WEEK) is None


def test_release_of_nothing_is_a_noop():
    assert apply_release(_state(runs_this_week=3), 0, now=NOW, week=WEEK) is None
    assert apply_release(_state(runs_this_week=3), -2, now=NOW, week=WEEK) is None


def test_release_floors_at_zero():
    refunded = apply_release(_state(runs_this_week=1), 5, now=NOW, week=WEEK)
    assert refunded is not None and refunded["runs_this_week"] == 0


# --------------------------------------------------------------- Limits


def test_limits_default_to_fourteen(monkeypatch):
    monkeypatch.delenv("DISCOVERY_RUNS_PER_WEEK", raising=False)
    assert Limits.from_env() == Limits(per_week=budget.DEFAULT_PER_WEEK)
    assert budget.DEFAULT_PER_WEEK == 14


def test_limits_read_the_env(monkeypatch):
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "11")
    assert Limits.from_env() == Limits(per_week=11)


def test_limits_are_clamped_to_the_designed_range(monkeypatch):
    """Below the floor the product stops working; above the ceiling the cap
    stops being one. A *positive* env value outside 10-20 is clamped, not
    honoured. Zero is not clamped — it is the kill switch, tested below."""
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "3")
    assert Limits.from_env() == Limits(per_week=10)
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "9999")
    assert Limits.from_env() == Limits(per_week=20)


# ------------------------------------------------------------ the kill switch
#
# The cap goes live at its default the moment the code deploys, with no ops
# step. Without an off switch, backing it out of a running service would mean
# reverting code — so zero means off, and off means no Firestore work at all.


def test_zero_turns_the_cap_off(monkeypatch):
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "0")
    limits = Limits.from_env()
    assert limits == Limits(per_week=budget.OFF)
    assert limits.enforced is False


def test_a_negative_value_is_also_off(monkeypatch):
    """Someone reaching for "off" may well type -1; honour the intent rather
    than clamping it up to a cap they were trying to remove."""
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "-1")
    assert Limits.from_env().enforced is False


def test_the_default_is_still_a_cap():
    """The positive control: the switch must not be on by accident."""
    assert Limits(per_week=budget.DEFAULT_PER_WEEK).enforced is True


def test_an_uncapped_reserve_grants_without_touching_firestore():
    """Off means off: not one read, not one write. ``_BrokenDB`` raises on any
    access, so this passing is proof the short-circuit is ahead of the client
    rather than merely ahead of the write."""

    class _BrokenDB:
        def collection(self, name):
            raise AssertionError("the cap is off; Firestore must not be touched")

    res = asyncio.run(budget.reserve(_BrokenDB(), "u1", limits=Limits(per_week=0)))
    assert res.granted == 1
    assert res.capped is False


def test_an_uncapped_sync_reserve_also_touches_nothing():
    class _BrokenDB:
        def collection(self, name):
            raise AssertionError("the cap is off; Firestore must not be touched")

    res = budget.reserve_sync(_BrokenDB(), "u1", limits=Limits(per_week=0))
    assert res.granted == 1 and res.capped is False


def test_an_uncapped_remaining_is_none_not_a_number():
    """``None`` rather than a large integer: "unlimited" is not a quantity, and
    a display handed 999 would invent a figure nobody chose."""
    assert budget.remaining({}, now=NOW, limits=Limits(per_week=0)) is None
    assert budget.remaining({}, now=NOW, limits=LIMITS) == 14


def test_an_uncapped_grant_reports_no_remainder():
    res = budget.unlimited(now=NOW)
    assert res.remaining_week is None and res.capped is False


def test_unparseable_limits_fall_back_to_the_default(monkeypatch):
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "lots")
    assert Limits.from_env() == Limits(per_week=budget.DEFAULT_PER_WEEK)


# ------------------------------------------------- reserve() / release() txn


def test_reserve_persists_the_new_counter():
    db = _FakeDB()
    res = asyncio.run(budget.reserve(db, "u1", limits=LIMITS))
    assert res.granted == 1
    assert db.budget_state["runs_this_week"] == 1
    assert db.budget_state["week"] == week_key(datetime.now(UTC), "UTC")


def test_two_concurrent_reserves_cannot_both_take_the_last_run():
    db = _FakeDB(
        state={"week": week_key(datetime.now(UTC), "UTC"), "runs_this_week": 13}
    )
    first = asyncio.run(budget.reserve(db, "u1", limits=LIMITS))
    second = asyncio.run(budget.reserve(db, "u1", limits=LIMITS))
    assert (first.granted, second.granted) == (1, 0)
    assert db.budget_state["runs_this_week"] == 14


def test_a_contended_reserve_retries_without_double_charging():
    db = _FakeDB(abort_once=True)
    res = asyncio.run(budget.reserve(db, "u1", limits=LIMITS))
    assert res.granted == 1
    assert db.budget_state["runs_this_week"] == 1  # not 2


def test_reserve_failures_propagate():
    """Fail closed: a dispatch that cannot reserve must not happen."""

    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    with pytest.raises(RuntimeError):
        asyncio.run(budget.reserve(_BrokenDB(), "u1"))


def test_release_refunds_through_the_transaction():
    key = week_key(datetime.now(UTC), "UTC")
    db = _FakeDB(state={"week": key, "runs_this_week": 4})
    asyncio.run(budget.release(db, "u1", week=key))
    assert db.budget_state["runs_this_week"] == 3


def test_release_never_raises():
    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    asyncio.run(budget.release(_BrokenDB(), "u1", week=WEEK))


def test_release_of_zero_never_touches_firestore():
    class _ExplodingDB:
        def collection(self, name):
            raise AssertionError("wrote for a zero refund")

    asyncio.run(budget.release(_ExplodingDB(), "u1", 0, week=WEEK))


def test_the_sync_shell_charges_the_same_counter():
    """``PUT /profile`` is a synchronous route that enqueues inside the
    request; its charge goes through the sync transaction rather than an
    ``asyncio.run`` that would strand an async client on a dead loop."""
    db = _FakeSyncDB()
    res = budget.reserve_sync(db, "u1", limits=LIMITS)
    assert res.granted == 1 and db.budget_state["runs_this_week"] == 1

    budget.release_sync(db, "u1", week=res.week_key)
    assert db.budget_state["runs_this_week"] == 0


def test_the_sync_release_never_raises():
    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    budget.release_sync(_BrokenDB(), "u1", week=WEEK)


# --------------------------------------------------------- the charge sites


@pytest.fixture
def adb(monkeypatch):
    """The async client the discovery routes charge through."""
    db = _FakeDB()
    monkeypatch.setattr(discovery, "_async_client", lambda: db)
    return db


@pytest.fixture
def dispatched(monkeypatch):
    """Spy on ``dispatch_cycle``: nothing crawls, and the test can make the
    queue answer "deduped" — or **raise**, which is what a Cloud Tasks 503 or
    a missing IAM binding does and what no fixture here could express until a
    charged-but-never-dispatched run turned out to be lost forever."""
    calls: list[dict] = []
    outcome: dict = {"queued": True, "raises": None}

    async def fake_dispatch_cycle(kind, user_id, *, trigger, score=True):
        calls.append({"kind": kind, "trigger": trigger, "score": score})
        if outcome["raises"] is not None:
            raise outcome["raises"]
        return outcome["queued"]

    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch_cycle)
    return SimpleNamespace(calls=calls, outcome=outcome)


@pytest.fixture
def run_now(monkeypatch, adb, dispatched):
    """``POST /settings/discovery/run`` with every seam past the cap faked."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(http=TestClient(app), db=adb, dispatched=dispatched)


def _spend_week(db, runs=14):
    db.store[budget.FIELD] = {
        "week": week_key(datetime.now(UTC), "UTC"),
        "runs_this_week": runs,
    }


def test_the_kill_switch_uncaps_a_spent_week_at_the_route(run_now, monkeypatch):
    """The point of the switch, end to end: the same user who gets 429 above
    gets served once it is thrown. Without this the switch could be right in
    the module and still not reach the route that refuses people."""
    _spend_week(run_now.db)
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "0")

    resp = run_now.http.post("/settings/discovery/run", json={})

    assert resp.status_code == 200, resp.text
    assert run_now.dispatched.calls, "the switch is off but nothing dispatched"


def test_the_tick_screen_passes_when_the_cap_is_off(monkeypatch):
    """``_allowance_left`` reads ``None`` as "no cap", not as "none left".

    ``None > 0`` is a TypeError and ``not None`` reads as capped, so this is
    the branch where an uncapped user would silently stop being ticked.
    """
    spent = {discovery_budget.FIELD: {"week": WEEK, "runs_this_week": 14}}
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "14")
    assert discovery._allowance_left("u1", spent) is False
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "0")
    assert discovery._allowance_left("u1", spent) is True


def test_run_now_at_cap_is_429_and_dispatches_nothing(run_now):
    """**429, never 402.** 402 means "confirm the spend" to this client and
    hands back a token that makes the action go through; a cap has no token
    and only time changes the answer.

    And the refusal is *ahead* of the dispatch: a search that is going to be
    refused must not have been started first.
    """
    _spend_week(run_now.db)

    resp = run_now.http.post("/settings/discovery/run", json={})

    assert resp.status_code == 429, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "discovery_cap"
    assert detail["runs_this_week"] == 14 and detail["per_week"] == 14
    assert datetime.fromisoformat(detail["resets_at"]) > datetime.now(UTC)
    assert run_now.dispatched.calls == [], "a capped click dispatched a search"
    # The refusal did not move the counter either.
    assert run_now.db.budget_state["runs_this_week"] == 14


def test_run_now_below_the_cap_charges_exactly_one_run(run_now):
    """The positive control: the cap is a cap, not an off switch."""
    resp = run_now.http.post("/settings/discovery/run", json={})

    assert resp.status_code == 200 and resp.json()["scored"] is False
    assert run_now.dispatched.calls == [
        {"kind": "discovery", "trigger": "manual", "score": False}
    ]
    assert run_now.db.budget_state["runs_this_week"] == 1


def test_dedupe_refunds_the_week_counter(run_now):
    """A double-click the queue collapses into one task ran one search, so it
    owes one charge — not two."""
    run_now.http.post("/settings/discovery/run", json={})
    run_now.dispatched.outcome["queued"] = False

    resp = run_now.http.post("/settings/discovery/run", json={})

    assert resp.status_code == 200 and resp.json()["deduped"] is True
    assert run_now.db.budget_state["runs_this_week"] == 1


def test_the_fourteenth_run_is_the_last_one(run_now):
    """End to end through the route: 14 clicks go through, the 15th is 429."""
    for i in range(14):
        assert (
            run_now.http.post("/settings/discovery/run", json={}).status_code == 200
        ), i

    assert run_now.http.post("/settings/discovery/run", json={}).status_code == 429
    assert len(run_now.dispatched.calls) == 14


# ------------------------------------------------------------ the tick


@pytest.fixture
def tick(monkeypatch, adb, dispatched):
    """``tick_user`` with the slot machinery spied rather than faked away."""
    claimed: list[str] = []
    released: list[str] = []

    monkeypatch.setattr(discovery, "_last_tick_check", {})
    monkeypatch.setattr(
        discovery,
        "_claim_slot",
        lambda uid, kind, hours, now: bool(claimed.append(kind)) or True,
    )
    monkeypatch.setattr(
        discovery,
        "_release_slot",
        lambda uid, kind, trigger, began: bool(released.append(kind)) or True,
    )
    return SimpleNamespace(
        claimed=claimed, released=released, db=adb, dispatched=dispatched
    )


def _doc(budget_state=None) -> dict:
    doc: dict = {
        "discovery_settings": {"auto_discovery": True, "liveness_sweep": False}
    }
    if budget_state is not None:
        doc[budget.FIELD] = budget_state
    return doc


def test_capped_tick_takes_no_lease(tick):
    """A capped user must cost **nothing**: the allowance is screened on the
    document the caller already holds, before ``_claim_slot``, so there is no
    Firestore write, no lease, and no window in which another tick sees the
    slot as busy."""
    state = {"week": week_key(datetime.now(UTC), "UTC"), "runs_this_week": 14}
    tick.db.store[budget.FIELD] = state

    asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc(state)))

    assert tick.claimed == [], "a capped tick claimed a slot"
    assert tick.released == []
    assert tick.dispatched.calls == []
    # Nothing was reserved, so no transaction ran at all.
    assert tick.db.transactions == []


def test_a_tick_under_the_cap_claims_charges_and_dispatches(tick):
    """The positive control, and the ordering claim: the claim comes first,
    then the charge, then the dispatch."""
    asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))

    assert tick.claimed == ["discovery"]
    assert tick.released == []
    assert tick.dispatched.calls == [
        {"kind": "discovery", "trigger": "cron", "score": True}
    ]
    assert tick.db.budget_state["runs_this_week"] == 1


def test_a_tick_that_loses_the_allowance_race_gives_the_lease_back(tick):
    """The screen ran on a document seconds old and the reservation — the one
    that actually binds — came back empty. The lease must not be left held for
    a cycle that is not going to run."""
    _spend_week(tick.db)

    asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))

    assert tick.claimed == ["discovery"]
    assert tick.released == ["discovery"]
    assert tick.dispatched.calls == []


def test_a_deduped_tick_dispatch_refunds_the_run(tick):
    tick.dispatched.outcome["queued"] = False

    asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))

    assert tick.dispatched.calls  # it did try
    assert tick.db.budget_state["runs_this_week"] == 0


# ------------------------------------------------- the worker must not charge


@pytest.fixture
def worker_client(monkeypatch, adb):
    """The worker's task handlers, with the cycle itself faked away."""
    monkeypatch.setenv("WORKER_MODE", "1")
    ran: list[dict] = []

    async def fake_cycle(user_id, *, trigger="scheduled", score=True):
        ran.append({"user_id": user_id, "trigger": trigger, "score": score})

    monkeypatch.setattr(worker, "run_discovery_cycle", fake_cycle)
    app = FastAPI()
    app.include_router(worker.router)
    return SimpleNamespace(http=TestClient(app), ran=ran, db=adb)


@pytest.mark.parametrize("path", ["/tasks/discovery", "/tasks/discovery/scan"])
def test_the_worker_task_handlers_never_charge(worker_client, path):
    """**A Cloud Tasks redelivery must not cost a second search.** The charge
    is at dispatch; the handler that receives the delivered task runs the
    crawl and nothing else.

    Delivered twice on purpose: that is the failure being excluded.
    """
    for _ in range(2):
        resp = worker_client.http.post(path, json={"user_id": "u1", "trigger": "cron"})
        assert resp.status_code == 200, resp.text

    assert len(worker_client.ran) == 2
    assert worker_client.db.store == {}, "the worker charged the weekly allowance"


# ------------------------------------------------- the onboarding kickoff


@pytest.fixture
def kickoff(monkeypatch):
    """``PUT /profile``'s first-completion kickoff, with the queue faked."""
    db = _FakeSyncDB()
    stored: dict = {}

    monkeypatch.setattr(
        profile,
        "_user_ref",
        lambda uid: SimpleNamespace(
            get=lambda: SimpleNamespace(to_dict=lambda: dict(stored)),
            set=lambda data, merge=False: stored.update(data),
        ),
    )
    monkeypatch.setattr(profile, "_client", lambda: db)
    enqueued: list[dict] = []
    outcome = {"queued": True}

    def fake_enqueue_cycle(kind, user_id, *, trigger, score=True):
        enqueued.append({"kind": kind, "trigger": trigger})
        return outcome["queued"]

    monkeypatch.setattr(discovery, "enqueue_cycle", fake_enqueue_cycle)
    monkeypatch.setenv("QUEUE_MODE", "1")
    return SimpleNamespace(db=db, enqueued=enqueued, outcome=outcome, stored=stored)


def _save_profile(kickoff_fixture):
    app = FastAPI()
    app.include_router(profile.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app).put("/profile", json=_profile_payload())


def test_the_onboarding_kickoff_charges_a_run(kickoff):
    assert _save_profile(kickoff).status_code == 200
    assert kickoff.enqueued == [{"kind": "discovery", "trigger": "onboarding"}]
    assert kickoff.db.budget_state["runs_this_week"] == 1


def test_a_deduped_onboarding_kickoff_refunds(kickoff):
    kickoff.outcome["queued"] = False
    assert _save_profile(kickoff).status_code == 200
    assert kickoff.db.budget_state["runs_this_week"] == 0


def test_an_onboarding_kickoff_at_the_cap_starts_nothing(kickoff):
    _spend_week(kickoff.db)
    assert _save_profile(kickoff).status_code == 200
    assert kickoff.enqueued == []


# ------------------------------------------------- the refund is windowed


def test_a_refund_a_week_late_credits_nothing():
    """The dispatch was charged last week; this week's counter is not its
    window, and crediting it would be a free search."""
    last_week = week_key(datetime.now(UTC) - timedelta(days=7), "UTC")
    db = _FakeDB(
        state={"week": week_key(datetime.now(UTC), "UTC"), "runs_this_week": 5}
    )

    asyncio.run(budget.release(db, "u1", week=last_week))

    assert db.budget_state["runs_this_week"] == 5


def test_a_refund_for_an_untracked_trigger_is_not_taken(monkeypatch):
    """Only the three dispatch sites charge, so only their triggers may
    refund. A CLI ``scheduled`` run never took a search; crediting one back
    would hand out an allowance nobody spent."""
    db = _FakeDB(
        state={"week": week_key(datetime.now(UTC), "UTC"), "runs_this_week": 5}
    )
    monkeypatch.setattr(discovery, "_async_client", lambda: db)

    asyncio.run(discovery._refund_run("u1", "scheduled"))
    assert db.budget_state["runs_this_week"] == 5

    asyncio.run(discovery._refund_run("u1", "cron"))
    assert db.budget_state["runs_this_week"] == 4


# ------------------------------- a dispatch that raises, not merely dedupes


class _CloudTasksDown(RuntimeError):
    """What ``queues.enqueue`` does on a 503, a missing IAM binding, or an
    unset ``WORKER_URL``/``TASKS_SA_EMAIL``."""


def test_a_tick_whose_dispatch_raises_refunds_and_frees_the_lease(tick, monkeypatch):
    """**The failure that turns an hour-long outage into a week-long one.**

    The charge commits, ``enqueue_cycle`` raises, and ``cron_tick``'s per-user
    try/except swallows it as one failed user. Unrefunded, fourteen hourly
    ticks spend the whole allowance on *zero* searches — and from then on the
    pre-claim screen turns the user away for the rest of the calendar week,
    long after Cloud Tasks came back. That is the "discovery never runs" bug
    this module exists to prevent, wearing the cap as a disguise.

    The lease goes back with it: holding one for a cycle that was never
    dispatched keeps the next tick off the slot for its full TTL.
    """
    monkeypatch.setenv("QUEUE_MODE", "1")
    tick.dispatched.outcome["raises"] = _CloudTasksDown("cloud tasks 503")

    with pytest.raises(_CloudTasksDown):
        asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))

    assert tick.dispatched.calls, "it never even tried to dispatch"
    assert tick.db.budget_state["runs_this_week"] == 0, (
        "a search was charged for a dispatch that never happened"
    )
    assert tick.released == ["discovery"]


def test_fourteen_failed_dispatches_do_not_cost_the_week(tick, monkeypatch):
    """The same thing at the scale that makes it matter: an outage lasting
    longer than the allowance must not exhaust it."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    tick.dispatched.outcome["raises"] = _CloudTasksDown("cloud tasks 503")

    for _ in range(14):
        with pytest.raises(_CloudTasksDown):
            asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))

    assert budget.used(tick.db.store.get(budget.FIELD)) == 0
    # And the user is still allowed to search once the queue comes back.
    tick.dispatched.outcome["raises"] = None
    asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))
    assert tick.db.budget_state["runs_this_week"] == 1


def test_an_in_process_cycle_that_fails_keeps_its_charge(tick, monkeypatch):
    """The other side of the same line, and the reason the refund is gated on
    ``queues.enabled()``. With no queue, ``dispatch_cycle`` **is** the cycle:
    a raise means a crawl ran and then failed, and a crawl that ran keeps its
    charge — otherwise a reliably-failing cycle retries forever for free."""
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    tick.dispatched.outcome["raises"] = _CloudTasksDown("the crawl died")

    with pytest.raises(_CloudTasksDown):
        asyncio.run(discovery.tick_user("u1", force_check=True, doc=_doc()))

    assert tick.db.budget_state["runs_this_week"] == 1


def test_a_run_now_whose_dispatch_raises_refunds_the_run(run_now):
    """Same shape on the manual route: the user gets a 500 and will click
    again, so the search they did not get must not be charged."""
    run_now.dispatched.outcome["raises"] = _CloudTasksDown("cloud tasks 503")

    with pytest.raises(_CloudTasksDown):
        run_now.http.post("/settings/discovery/run", json={})

    assert run_now.dispatched.calls
    assert budget.used(run_now.db.store.get(budget.FIELD)) == 0


# --------------------------------- the charge will not recreate a wiped doc


def test_a_charge_against_a_missing_document_grants_nothing_and_writes_nothing():
    """``reserve`` writes with ``merge=True``, which **creates** a missing
    document — and ``delete_account`` removes ``users/{uid}`` outright. A
    request racing the delete, or arriving on an ID token that has not expired
    yet, would otherwise leave a document holding a ``discovery_budget`` and no
    ``deleted_at``: an account that reads as live to every loop that screens on
    the tombstone.

    This is the charge-side twin of the guard ``apply_release`` already had.
    """
    db = _FakeDB(exists=False)

    res = asyncio.run(budget.reserve(db, "u1", limits=LIMITS))

    assert res.granted == 0 and res.capped is True
    assert db.store == {}, "the charge recreated a deleted user's document"


def test_the_sync_charge_will_not_recreate_a_missing_document():
    db = _FakeSyncDB(exists=False)

    res = budget.reserve_sync(db, "u1", limits=LIMITS)

    assert res.granted == 0
    assert db.store == {}


def test_an_existing_but_empty_document_is_still_charged():
    """The positive control, and the distinction the fake has to honour: an
    *empty* user document is a live account that has never searched, not a
    wiped one. Failing closed on it would mean nobody's first search ever
    happened."""
    db = _FakeDB()

    res = asyncio.run(budget.reserve(db, "u1", limits=LIMITS))

    assert res.granted == 1 and db.budget_state["runs_this_week"] == 1
