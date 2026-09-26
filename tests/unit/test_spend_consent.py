# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The consent seam: a paid route asks first, and one yes buys one run.

Five properties, each of which is a way the seam could fail *open* — quietly
letting a click spend money nobody was asked about:

- no token at all is not a yes (and the 402 has to carry a real quote, or the
  UI has nothing to show and the user is being asked to confirm a blank);
- a token spends once;
- a yes to one action is not a yes to another;
- a yes goes stale — checked in our code, because the Firestore TTL policy
  that would collect these documents is a GCP setting that exists in no file
  in this repo;
- a yes belongs to the account that gave it.

The estimate itself is exercised alongside, because a quote nobody can check
is not a safeguard: it has to come from the *budget grant*, never the backlog.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import api.deps as deps
from api.deps import SpendConfirm, required, verify_user
from tools.matching import budget, rates
from tools.spend import consent, estimate

# --------------------------------------------------------------------------
# A Firestore stand-in: paths in, documents out. Small on purpose — the seam's
# whole storage contract is "a document under users/{uid}/spend_consents".
# --------------------------------------------------------------------------


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    async def set(self, data):
        self._store[self._path] = dict(data)

    async def get(self):
        return _Snap(self._store.get(self._path))

    async def delete(self):
        self._store.pop(self._path, None)

    def collection(self, name):
        return _Collection(self._store, f"{self._path}/{name}")


class _Collection:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _Doc(self._store, f"{self._path}/{doc_id}")

    def order_by(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    async def stream(self):
        for path in sorted(self._store):
            head, _, tail = path.rpartition("/")
            if head == self._path and tail:
                yield _Snap(self._store[path])


class _DB:
    """Keyed by full document path, so scoping bugs are visible in the keys."""

    def __init__(self, docs=None):
        self.store = dict(docs or {})

    def collection(self, name):
        return _Collection(self.store, name)


@pytest.fixture
def db():
    return _DB()


def _estimate(action=estimate.SCORE_BACKLOG, units=200) -> estimate.Estimate:
    return estimate.quote(
        action,
        remaining_cycle=units,
        remaining_day=units * 2,
        rate=rates.MEASURED,
        limits=budget.Limits(per_cycle=200, per_day=400),
    )


# --------------------------------------------------------------------------
# The estimate
# --------------------------------------------------------------------------


def test_the_quote_counts_the_budget_grant_and_not_the_backlog():
    """A fresh account has thousands of pending jobs and a 200-job grant.
    Quoting the backlog would be terrifying *and* wrong."""
    quote = _estimate(units=200)

    assert quote.units == 200 and quote.unit == "job"
    assert quote.caps["per_cycle"] == 200 and quote.caps["per_day"] == 400
    # A range, never a figure, and both ends move with the units.
    assert 0 < quote.usd_low < quote.usd_high
    assert _estimate(units=50).usd_high < quote.usd_high


def test_the_quote_is_capped_by_whichever_limit_binds():
    tight_day = estimate.quote(
        estimate.SCORE_BACKLOG,
        remaining_cycle=200,
        remaining_day=17,
        rate=rates.MEASURED,
        limits=budget.Limits(per_cycle=200, per_day=400),
    )
    assert tight_day.units == 17


def test_the_quote_says_where_its_rate_came_from():
    """ "based on your last 3 runs" and "using the 2026-08-23 measurement" are
    different claims, and the user is entitled to know which one they see."""
    fallback = _estimate()
    assert fallback.rate_source == rates.SOURCE_MEASURED
    assert fallback.rate_sample == 0

    mine = estimate.quote(
        estimate.SCORE_BACKLOG,
        remaining_cycle=200,
        remaining_day=400,
        rate=rates.Rate(0.0104, rates.SOURCE_OBSERVED, 312),
        limits=budget.Limits(per_cycle=200, per_day=400),
    )
    assert mine.rate_source == rates.SOURCE_OBSERVED and mine.rate_sample == 312


def test_a_spent_cycle_window_quotes_zero_for_the_ad_hoc_score():
    """``/jobs/score`` draws on the open window (``cycle_id=None``), so a
    window already spent grants nothing — and the quote has to say so rather
    than promising 200 jobs the budget will refuse."""
    state = {
        "day": datetime.now(UTC).date().isoformat(),
        "jobs_scored_today": 200,
        "cycle_id": "abc",
        "jobs_scored_this_cycle": 200,
    }
    limits = budget.Limits(per_cycle=200, per_day=400)

    cycle, day = estimate.available(state, action=estimate.SCORE_BACKLOG, limits=limits)
    assert (cycle, day) == (0, 200)

    # A fresh discovery cycle opens a new window, so its cycle counter is full
    # — but the *daily* counter still binds, which is the cap that matters.
    cycle, day = estimate.available(
        state, action=estimate.DISCOVERY_SCAN, limits=limits
    )
    assert (cycle, day) == (200, 200)


def test_yesterdays_counters_do_not_bind_today():
    state = {
        "day": "2020-01-01",
        "jobs_scored_today": 400,
        "cycle_id": "abc",
        "jobs_scored_this_cycle": 200,
    }
    _, day = estimate.available(
        state,
        action=estimate.SCORE_BACKLOG,
        limits=budget.Limits(per_cycle=200, per_day=400),
    )
    assert day == 400


def test_an_observed_rate_needs_enough_jobs_behind_it(db):
    """A two-job run divides by a number small enough to quote anything, so
    below the threshold the documented constant wins."""
    db.store["users/u1/runs/r1"] = {
        "ended_at": "2026-09-01T00:00:00+00:00",
        "llm": {"cost_usd": 1.04},
        "jobs": {"scored": 5},
    }
    thin = asyncio.run(rates.observed_rate(db, "u1"))
    assert thin is rates.MEASURED

    db.store["users/u1/runs/r2"] = {
        "ended_at": "2026-09-02T00:00:00+00:00",
        "llm": {"cost_usd": 2.00},
        "jobs": {"scored": 200},
    }
    mine = asyncio.run(rates.observed_rate(db, "u1"))
    assert mine.source == rates.SOURCE_OBSERVED
    assert mine.sample == 205
    assert mine.usd_per_job == pytest.approx(3.04 / 205)


def test_a_run_that_banked_cost_without_counts_is_skipped_not_averaged_in(db):
    """An ingest that raised banks the leg's spend with no ``jobs.scored`` (see
    ``batch_runs.resume``'s flush). Counting it as zero jobs would divide real
    dollars by nothing and quote the user a rate that never existed."""
    db.store["users/u1/runs/good"] = {
        "ended_at": "2026-09-02T00:00:00+00:00",
        "llm": {"cost_usd": 1.00},
        "jobs": {"scored": 100},
    }
    db.store["users/u1/runs/raised"] = {
        "ended_at": "2026-09-03T00:00:00+00:00",
        "llm": {"cost_usd": 9.00},
        "jobs": {"scored": 0},
    }
    mine = asyncio.run(rates.observed_rate(db, "u1"))
    assert mine.usd_per_job == pytest.approx(0.01)


# --------------------------------------------------------------------------
# preflight / consume — the seam itself
# --------------------------------------------------------------------------


def _mint(db, user_id="u1", action=estimate.SCORE_BACKLOG) -> str:
    return asyncio.run(consent.preflight(db, user_id, action, _estimate(action)))


def test_a_token_round_trips_with_the_estimate_it_was_minted_for(db):
    token = _mint(db)
    got = asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))
    assert got == _estimate()


def test_a_token_is_single_use(db):
    """T2. Without the delete, one confirmation would authorise every click
    that followed it."""
    token = _mint(db)
    asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))

    with pytest.raises(consent.ConsentRequired):
        asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))
    assert db.store == {}  # and the document really is gone


def test_a_token_minted_for_another_action_is_rejected(db):
    """T3. A yes to "find jobs" is not a yes to "score 200 of them"."""
    token = _mint(db, action=estimate.DISCOVERY_SCAN)

    with pytest.raises(consent.ConsentRequired):
        asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))

    # Burnt anyway: a mismatched token must not be retriable against the route
    # it *would* have fitted.
    assert db.store == {}


def test_an_expired_token_is_rejected(db):
    """T4. Enforced here, in code. Firestore's TTL policy is a per-collection
    GCP setting that lives in no file in this repo — until someone enables it
    nothing is ever deleted, and even then collection lags by hours."""
    token = _mint(db)
    path = next(iter(db.store))
    stale = datetime.now(UTC) - consent._TTL - timedelta(seconds=1)
    db.store[path]["issued_at"] = stale.isoformat()

    with pytest.raises(consent.ConsentRequired):
        asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))


def test_a_token_with_no_issue_time_is_rejected(db):
    """The expiry check must not be skippable by a document that simply has
    no timestamp — a hand-written or half-written doc is not a fresh one."""
    token = _mint(db)
    db.store[next(iter(db.store))].pop("issued_at")

    with pytest.raises(consent.ConsentRequired):
        asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))


def test_a_token_is_scoped_to_the_user_who_minted_it(db):
    """T5. The lookup is a *path* under ``users/{uid}``, never a
    collection-group query — otherwise a token's scope would be "anyone who
    knows the uuid" rather than "the account that minted it"."""
    token = _mint(db, user_id="u1")
    assert any(path.startswith("users/u1/spend_consents/") for path in db.store)

    with pytest.raises(consent.ConsentRequired):
        asyncio.run(consent.consume(db, "u2", estimate.SCORE_BACKLOG, token))

    # u1's token survives u2's attempt — it was never theirs to burn.
    assert asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, token))


def test_a_missing_token_is_refused(db):
    for bad in (None, "", "not-a-real-token"):
        with pytest.raises(consent.ConsentRequired):
            asyncio.run(consent.consume(db, "u1", estimate.SCORE_BACKLOG, bad))


# --------------------------------------------------------------------------
# The HTTP shape — the 402 the client actually branches on
# --------------------------------------------------------------------------


@pytest.fixture
def gated(monkeypatch, db):
    """A minimal route carrying the seam, so the 402 body is pinned
    independently of whatever ``/jobs/score`` grows into."""
    monkeypatch.setattr(deps, "spend_client", lambda: db)
    gate = Depends(required(estimate.SCORE_BACKLOG))

    app = FastAPI()

    @app.post("/paid")
    async def paid(
        body: SpendConfirm | None = None,
        quote: estimate.Estimate = gate,
    ) -> dict:
        return {"spent": True, "units": quote.units}

    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), db


def test_a_paid_route_without_a_token_answers_402_with_an_estimate(gated):
    """T1. And the estimate has to be *usable*: a 402 with no numbers asks the
    user to confirm a blank."""
    client, _ = gated

    resp = client.post("/paid", json={})

    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["needs_confirmation"] is True
    assert detail["action"] == estimate.SCORE_BACKLOG
    assert detail["confirm_token"]
    quote = detail["estimate"]
    assert quote["units"] > 0 and quote["unit"] == "job"
    assert quote["usd_low"] < quote["usd_high"]
    assert quote["rate_source"] and quote["caps"]["per_day"]


def test_the_token_the_402_hands_back_is_the_one_that_works(gated):
    client, _ = gated
    token = client.post("/paid", json={}).json()["detail"]["confirm_token"]

    resp = client.post("/paid", json={"confirm": token})

    assert resp.status_code == 200 and resp.json()["spent"] is True


def test_a_replayed_token_gets_a_fresh_402_not_a_second_run(gated):
    client, _ = gated
    token = client.post("/paid", json={}).json()["detail"]["confirm_token"]
    assert client.post("/paid", json={"confirm": token}).status_code == 200

    again = client.post("/paid", json={"confirm": token})

    assert again.status_code == 402
    assert again.json()["detail"]["confirm_token"] != token


def test_an_invented_token_does_not_spend(gated):
    client, _ = gated
    assert client.post("/paid", json={"confirm": "deadbeef"}).status_code == 402


def test_the_seam_refuses_an_unknown_action():
    """A typo in an action name must not silently create a second, ungated
    namespace of consents."""
    with pytest.raises(ValueError):
        required("score_backlogs")
    with pytest.raises(ValueError):
        estimate.quote(
            "score_backlogs",
            remaining_cycle=1,
            remaining_day=1,
            rate=rates.MEASURED,
        )
