# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Trial and paid plans: the scoring caps, the trial's unattended week, the
new-account defaults, and the free scored "Find new jobs" for trial users.

Zero LLM calls, zero GCP: Firestore is faked, the queue is spied, and the one
chain test drives the real cycle into the real scoring reservation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firestore_fakes import FakeStoreDB, _FakeDB
from test_api_profile import _FakeClient, _profile_payload

import api.routes.discovery as discovery
import api.routes.profile as profile
from api.deps import verify_user
from tools.account import plan
from tools.discovery import budget as discovery_budget
from tools.discovery.pipeline import PersistResult
from tools.matching import batch_runs
from tools.matching import budget as matching_budget
from tools.matching.budget import Limits

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

_BUDGET_VARS = (
    "SCORING_BUDGET_PER_DAY",
    "SCORING_BUDGET_PER_CYCLE",
    "SCORING_BUDGET_PER_DAY_TRIAL",
    "SCORING_BUDGET_PER_DAY_PAID",
)


@pytest.fixture(autouse=True)
def clean_budget_env(monkeypatch):
    for var in _BUDGET_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(matching_budget, "_warned", set())


def _plan(tier: str = "trial", *, days_ago: float | None = None) -> dict:
    stored: dict = {"tier": tier}
    if days_ago is not None:
        stored["trial_started_at"] = (NOW - timedelta(days=days_ago)).isoformat()
    return {plan.FIELD: stored}


# --------------------------------------------------------------- the plan


@pytest.mark.parametrize(
    "doc",
    [None, {}, {"plan": None}, {"plan": "paid"}, {"plan": {"tier": "gold"}}],
)
def test_anything_but_an_explicit_paid_tier_is_a_trial(doc):
    assert plan.tier_of(doc) == plan.TRIAL


def test_an_existing_account_without_a_plan_is_a_trial_whose_week_has_not_started():
    """No stored start counts from now, so the week is open, not already over."""
    doc = {"full_name": "Test User"}
    assert plan.tier_of(doc) == plan.TRIAL
    assert plan.auto_allowed(doc, now=NOW)
    assert plan.auto_until(doc, now=NOW) == NOW + timedelta(days=7)


@pytest.mark.parametrize(
    "days_ago, allowed", [(0, True), (6.9, True), (7, False), (30, False)]
)
def test_a_trial_runs_unattended_for_seven_days(days_ago, allowed):
    assert plan.auto_allowed(_plan(days_ago=days_ago), now=NOW) is allowed


def test_a_paid_plan_runs_unattended_however_old_its_trial():
    doc = _plan("paid", days_ago=90)
    assert plan.auto_allowed(doc, now=NOW)
    assert plan.auto_until(doc, now=NOW) is None


def test_onboarding_fields_stamp_once_and_never_downgrade():
    assert plan.onboarding_fields({}, now=NOW) == {
        "plan": {"tier": "trial", "trial_started_at": NOW.isoformat()}
    }
    assert plan.onboarding_fields(_plan(days_ago=2), now=NOW) == {}
    assert plan.onboarding_fields({"plan": {"tier": "paid"}}, now=NOW) == {
        "plan": {"tier": "paid", "trial_started_at": NOW.isoformat()}
    }


def test_the_lazy_trial_start_stamps_once_and_leaves_the_tier_alone():
    db = _FakeDB()
    db.store.update({"full_name": "Test User", "plan": {"tier": "paid"}})

    first = asyncio.run(plan.ensure_trial_start(db, "u1", now=NOW))
    later = asyncio.run(plan.ensure_trial_start(db, "u1", now=NOW + timedelta(days=3)))

    assert first == later == NOW
    assert db.store["plan"] == {"tier": "paid", "trial_started_at": NOW.isoformat()}


def test_the_lazy_trial_start_will_not_recreate_a_deleted_account():
    db = _FakeDB(exists=False)
    assert asyncio.run(plan.ensure_trial_start(db, "u1", now=NOW)) is None
    assert db.store == {}


# --------------------------------------------------------------- the caps


def test_the_trial_scores_three_a_day_and_paid_ten():
    assert Limits.from_env() == Limits(per_cycle=3, per_day=3)
    assert Limits.from_env(plan.TRIAL) == Limits(per_cycle=3, per_day=3)
    assert Limits.from_env(plan.PAID) == Limits(per_cycle=10, per_day=10)
    assert Limits.for_doc({}) == Limits(3, 3)
    assert Limits.for_doc(_plan("paid")) == Limits(10, 10)


def test_the_per_plan_vars_set_each_cap(monkeypatch):
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY_TRIAL", "2")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY_PAID", "20")
    assert Limits.from_env(plan.TRIAL) == Limits(2, 2)
    assert Limits.from_env(plan.PAID) == Limits(20, 20)


def test_a_global_var_meant_for_paid_cannot_lift_the_trial(monkeypatch):
    """``SCORING_BUDGET_PER_DAY`` is a ceiling: setting it to 10 for paid users
    leaves the trial at 3."""
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "10")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "10")
    assert Limits.from_env(plan.TRIAL) == Limits(3, 3)


def test_a_trial_override_above_paid_is_clamped_to_paid(monkeypatch, caplog):
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY_TRIAL", "40")
    with caplog.at_level(logging.WARNING):
        assert Limits.from_env(plan.TRIAL) == Limits(10, 10)
    assert "matching.budget_trial_above_paid" in caplog.text


def test_raising_the_trial_cap_is_never_silent(monkeypatch, caplog):
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY_TRIAL", "5")
    with caplog.at_level(logging.WARNING):
        assert Limits.from_env(plan.TRIAL) == Limits(5, 5)
    assert "matching.budget_trial_raised" in caplog.text


def test_an_unparseable_trial_override_keeps_the_default(monkeypatch):
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY_TRIAL", "lots")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "nope")
    assert Limits.from_env(plan.TRIAL) == Limits(3, 3)


@pytest.mark.parametrize("tier", [plan.TRIAL, plan.PAID])
@pytest.mark.parametrize("huge", ["49", "50", "400", "100000"])
def test_no_configuration_grants_a_batch_sized_slice(monkeypatch, tier, huge):
    """Every grant stays below ``BATCH_MIN_PENDING``, whatever the env says."""
    assert matching_budget.MAX_GRANT < batch_runs.BATCH_MIN_PENDING
    for var in _BUDGET_VARS:
        monkeypatch.setenv(var, huge)
    limits = Limits.from_env(tier)
    assert limits.per_day < batch_runs.BATCH_MIN_PENDING
    assert limits.per_cycle <= limits.per_day

    db = _FakeDB()
    db.store.update(_plan(tier))
    res = asyncio.run(matching_budget.reserve(db, "u1", 10_000, cycle_id="c"))
    assert 0 < res.granted < batch_runs.BATCH_MIN_PENDING


@pytest.mark.parametrize("tier, cap", [(plan.TRIAL, 3), (plan.PAID, 10), (None, 3)])
def test_reserve_reads_the_plan_off_the_document_it_already_reads(tier, cap):
    """One transactional read of ``users/u1``, and nothing else: the plan
    rides along with the counters."""
    user = _plan(tier) if tier else {}
    db = FakeStoreDB({"users": {"u1": dict(user)}})

    res = asyncio.run(matching_budget.reserve(db, "u1", 100, cycle_id="c"))

    assert res.granted == cap
    assert db.gets == ["users/u1"]
    assert db.transactional_reads == ["users/u1"]


def test_the_trial_cap_holds_across_cycles_in_one_day():
    """Each discovery cycle opens its own window; the day still caps at 3."""
    db = _FakeDB()
    grants = [
        asyncio.run(matching_budget.reserve(db, "u1", 50, cycle_id=f"c{i}")).granted
        for i in range(5)
    ]
    assert grants == [3, 0, 0, 0, 0]


# ----------------------------------------------- onboarding: plan + defaults


@pytest.fixture
def onboard(monkeypatch):
    """``PUT /profile`` with the kickoff queued into a spy."""
    store: dict = {}
    monkeypatch.setattr(profile, "_client", lambda: _FakeClient(store))
    monkeypatch.setenv("QUEUE_MODE", "1")
    outcome = {"raise": False}
    enqueued: list = []

    def fake_enqueue(queue, path, payload, *, task_id=None):
        if outcome["raise"]:
            raise RuntimeError("Cloud Tasks is unreachable")
        enqueued.append(path)
        return True

    monkeypatch.setattr("api.routes.discovery.queues.enqueue", fake_enqueue)
    app = FastAPI()
    app.include_router(profile.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(
        http=TestClient(app), store=store, outcome=outcome, enqueued=enqueued
    )


def test_first_completion_starts_the_trial_and_turns_both_loops_on(onboard):
    resp = onboard.http.put("/profile", json=_profile_payload())

    assert resp.status_code == 200
    user = onboard.store["u1"]
    assert user["plan"]["tier"] == "trial"
    started = datetime.fromisoformat(user["plan"]["trial_started_at"])
    assert abs(datetime.now(UTC) - started) < timedelta(minutes=1)
    assert user["discovery_settings"] == {
        "auto_discovery": True,
        "discovery_interval_hours": 24,
        "liveness_sweep": True,
        "sweep_interval_hours": 24,
    }
    # Held so a page load during the kickoff does not dispatch a second cycle.
    lease = user["discovery_state"]["discovery_lease"]
    assert datetime.fromisoformat(lease["expires_at"]) > datetime.now(UTC)
    assert onboard.enqueued == ["/tasks/discovery"]


def test_the_trial_is_stamped_exactly_once(onboard):
    """Neither a retry after a failed kickoff nor a later edit restarts it."""
    onboard.outcome["raise"] = True
    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 503
    first = dict(onboard.store["u1"]["plan"])

    onboard.outcome["raise"] = False
    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 200
    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 200

    assert onboard.store["u1"]["plan"] == first


def test_existing_discovery_settings_are_never_overwritten(onboard):
    mine = {
        "auto_discovery": False,
        "discovery_interval_hours": 72,
        "liveness_sweep": False,
        "sweep_interval_hours": 12,
    }
    onboard.store["u1"] = {"discovery_settings": dict(mine)}

    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 200

    assert onboard.store["u1"]["discovery_settings"] == mine
    assert "discovery_state" not in onboard.store["u1"]


def test_defaults_are_written_once_not_on_later_edits(onboard):
    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 200
    # The user turns discovery off; a later profile edit must not undo it.
    onboard.store["u1"]["discovery_settings"]["auto_discovery"] = False

    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 200

    assert onboard.store["u1"]["discovery_settings"]["auto_discovery"] is False


def test_onboarding_never_downgrades_a_paid_plan(onboard):
    onboard.store["u1"] = {"plan": {"tier": "paid"}}
    assert onboard.http.put("/profile", json=_profile_payload()).status_code == 200
    assert onboard.store["u1"]["plan"]["tier"] == "paid"


def test_get_profile_carries_the_plan(onboard):
    onboard.http.put("/profile", json=_profile_payload())
    body = onboard.http.get("/profile").json()
    assert body["plan"]["tier"] == "trial"
    assert body["plan"]["auto_active"] is True
    assert body["plan"]["scoring_per_day"] == 3


# ------------------------------------------------------- the trial's week


@pytest.fixture
def tick(monkeypatch):
    """``tick_user`` with the slot claim spied and dispatch recorded."""
    db = _FakeDB()
    monkeypatch.setattr(discovery, "_async_client", lambda: db)
    monkeypatch.setattr(discovery, "_last_tick_check", {})
    monkeypatch.setattr(discovery, "_now", lambda: NOW)
    claimed: list[str] = []
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        discovery,
        "_claim_slot",
        lambda uid, kind, hours, now: bool(claimed.append(kind)) or True,
    )

    async def fake_dispatch(kind, user_id, *, trigger, score=True):
        dispatched.append((kind, score))
        return True

    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch)
    return SimpleNamespace(db=db, claimed=claimed, dispatched=dispatched)


def _user(**extra) -> dict:
    doc = {
        "full_name": "Test User",
        "discovery_settings": {"auto_discovery": True, "liveness_sweep": True},
    }
    doc.update(extra)
    return doc


def _run_tick(fixture, doc):
    fixture.db.store.update(doc)
    asyncio.run(discovery.tick_user("u1", force_check=True, doc=dict(doc)))


def test_a_trial_in_its_first_week_runs_both_loops(tick):
    _run_tick(tick, _user(**_plan(days_ago=3)))
    assert tick.dispatched == [("discovery", True), ("sweep", True)]


def test_a_trial_past_day_seven_runs_nothing_and_keeps_its_toggles(tick):
    doc = _user(**_plan(days_ago=8))
    _run_tick(tick, doc)

    assert tick.dispatched == []
    assert tick.claimed == [], "a gated tick took a lease"
    assert tick.db.transactions == [], "a gated tick charged a search"
    assert tick.db.store["discovery_settings"] == doc["discovery_settings"]


def test_a_paid_plan_runs_both_loops_however_old_its_trial(tick):
    _run_tick(tick, _user(**_plan("paid", days_ago=60)))
    assert tick.dispatched == [("discovery", True), ("sweep", True)]


def test_a_paid_plan_with_its_toggles_off_runs_nothing(tick):
    doc = _user(**_plan("paid", days_ago=60))
    doc["discovery_settings"] = {"auto_discovery": False, "liveness_sweep": False}
    _run_tick(tick, doc)
    assert tick.dispatched == []


def test_an_existing_account_gets_its_week_from_the_first_tick(tick, monkeypatch):
    """No plan at all: stamped "now" once, then gated like any trial."""
    _run_tick(tick, _user())

    assert tick.db.store["plan"] == {"trial_started_at": NOW.isoformat()}
    assert tick.dispatched == [("discovery", True), ("sweep", True)]

    # Eight days on, the stored start ends the week.
    tick.dispatched.clear()
    discovery._last_tick_check.clear()
    later = NOW + timedelta(days=8)
    monkeypatch.setattr(discovery, "_now", lambda: later)
    asyncio.run(discovery.tick_user("u1", force_check=True, doc=dict(tick.db.store)))
    assert tick.dispatched == []
    assert tick.db.store["plan"] == {"trial_started_at": NOW.isoformat()}


# ------------------------------------------------- GET /settings/discovery


@pytest.fixture
def settings_client(monkeypatch):
    stored: dict = {}
    monkeypatch.setattr(
        discovery,
        "_user_ref",
        lambda uid: SimpleNamespace(
            get=lambda: SimpleNamespace(to_dict=lambda: dict(stored))
        ),
    )
    monkeypatch.setattr(discovery, "_now", lambda: NOW)

    async def no_tick(*a, **kw):
        return None

    monkeypatch.setattr(discovery, "tick_user", no_tick)
    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), stored


def test_the_settings_response_carries_the_plan_and_drops_next_runs_after_day_seven(
    settings_client,
):
    http, stored = settings_client
    stored.update(_user(**_plan(days_ago=8)))

    body = http.get("/settings/discovery").json()

    assert body["plan"]["tier"] == "trial"
    assert body["plan"]["auto_active"] is False
    assert body["plan"]["auto_until"] == (NOW + timedelta(days=-1)).isoformat()
    assert body["next_discovery_at"] is None and body["next_sweep_at"] is None
    # The toggles are reported as stored: the gate is the plan's, not theirs.
    assert body["settings"]["auto_discovery"] is True


def test_the_settings_response_in_the_first_week_schedules_runs(settings_client):
    http, stored = settings_client
    last = (NOW - timedelta(hours=3)).isoformat()
    stored.update(
        _user(**_plan(days_ago=2), discovery_state={"last_discovery_at": last})
    )

    body = http.get("/settings/discovery").json()

    assert body["plan"]["auto_active"] is True
    assert body["next_discovery_at"] == (NOW + timedelta(hours=21)).isoformat()


# ------------------------------------------------- the manual run


class _NoConsent:
    """A consent store that fails the test if the trial path touches it."""

    def collection(self, name):
        raise AssertionError("the trial manual run reached the consent seam")


@pytest.fixture
def run_now(monkeypatch):
    adb = _FakeDB()
    monkeypatch.setattr(discovery, "_async_client", lambda: adb)
    monkeypatch.setattr(discovery, "spend_client", lambda: _NoConsent())
    dispatched: list[bool] = []

    async def fake_dispatch(kind, user_id, *, trigger, score=True):
        dispatched.append(score)
        return True

    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch)
    monkeypatch.setenv("QUEUE_MODE", "1")
    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(http=TestClient(app), db=adb, dispatched=dispatched)


@pytest.mark.parametrize("body", [{}, {"confirm": "bogus"}, None])
def test_a_trial_manual_run_scores_with_no_402(run_now, body):
    run_now.db.store.update(_plan(days_ago=12))

    resp = run_now.http.post("/settings/discovery/run", json=body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["scored"] is True
    assert run_now.dispatched == [True]


def test_an_existing_account_without_a_plan_scores_like_a_trial(run_now):
    resp = run_now.http.post("/settings/discovery/run", json={})
    assert resp.status_code == 200 and resp.json()["scored"] is True


def test_a_paid_manual_run_without_consent_stays_find_only(run_now):
    """The seam is unchanged for every plan but the trial."""
    run_now.db.store.update(_plan("paid"))
    resp = run_now.http.post("/settings/discovery/run", json={})
    assert resp.status_code == 200 and resp.json()["scored"] is False
    assert run_now.dispatched == [False]


def test_the_weekly_search_cap_still_binds_a_trial(run_now):
    run_now.db.store[discovery_budget.FIELD] = {
        "week": discovery_budget.week_key(datetime.now(UTC), "UTC"),
        "runs_this_week": discovery_budget.Limits.from_env().per_week,
    }
    resp = run_now.http.post("/settings/discovery/run", json={})
    assert resp.status_code == 429
    assert resp.json()["detail"]["reason"] == "discovery_cap"
    assert run_now.dispatched == []


@pytest.fixture
def chain(monkeypatch):
    """The manual run in-process, through the real cycle, into a real scoring
    reservation on the same user document. Only the crawl is faked."""
    adb = _FakeDB()
    monkeypatch.setattr(discovery, "_async_client", lambda: adb)
    monkeypatch.setattr(discovery, "spend_client", lambda: _NoConsent())
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    jobs = [SimpleNamespace(id=f"j{i}") for i in range(200)]

    async def fake_run_discovery(user_id):
        return {
            "jobs": jobs,
            "jobs_by_platform": {"greenhouse": len(jobs)},
            "failures": [],
            "empty_boards": [],
            "boards_cached": 0,
            "boards_fetched": 1,
            "boards_not_found": 0,
            "boards_failing": 0,
        }

    async def none(*a, **kw):
        return None

    async def not_deleted(user_id):
        return False

    async def persist(items):
        return PersistResult(new=len(items))

    async def backlog(user_id):
        return len(jobs)

    grants: list[int] = []

    async def score_pending_jobs(user_id):
        # What the real scorer does first: one reservation, before any load.
        res = await matching_budget.reserve(adb, user_id, len(jobs))
        grants.append(res.granted)
        return {"scored": res.granted, "discarded": 0, "failed": 0, "pending": 0}

    monkeypatch.setattr(discovery, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(discovery, "load_job_preferences", none)
    monkeypatch.setattr(discovery, "prefilter_jobs", lambda items, prefs: (items, {}))
    monkeypatch.setattr(discovery, "persist_new_jobs", persist)
    monkeypatch.setattr(discovery, "_backlog", backlog)
    monkeypatch.setattr(discovery, "_account_deleted", not_deleted)
    monkeypatch.setattr(discovery, "open_run", none)
    monkeypatch.setattr(discovery, "persist_run_cost", none)
    monkeypatch.setattr(discovery, "score_pending_jobs", score_pending_jobs)
    monkeypatch.setattr(
        discovery,
        "_user_ref",
        lambda uid: SimpleNamespace(set=lambda *a, **kw: None),
    )
    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(http=TestClient(app), db=adb, grants=grants)


def test_repeated_trial_clicks_never_score_past_the_daily_cap(chain):
    chain.db.store.update(_plan(days_ago=12))

    for _ in range(5):
        resp = chain.http.post("/settings/discovery/run", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["scored"] is True

    assert chain.grants == [3, 0, 0, 0, 0]
    assert chain.db.store["scoring_budget"]["jobs_scored_today"] == 3
    assert chain.db.store[discovery_budget.FIELD]["runs_this_week"] == 5
