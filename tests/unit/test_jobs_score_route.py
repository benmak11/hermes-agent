# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``POST /jobs/score`` — the second, priced click.

``POST /settings/discovery/run`` now finds jobs without scoring them. This is
the other half of that split, and unlike the discovery route it has no free
verb to fall back to: every path through it spends, so the consent seam is a
hard dependency rather than an optional branch.

What is pinned here is the pair of properties that make the split worth
having at all — it asks first, **and** the thing it eventually runs is still
the same budget-capped scorer it always was. The seam does not grant slots;
the budget is untouched by this PR and this is where that is checked from the
HTTP side.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_spend_consent import _DB

import api.deps as deps
import api.routes.discovery as discovery
import api.routes.jobs as jobs
from api.deps import verify_user
from tools import queues
from tools.matching import budget
from tools.spend import estimate


@pytest.fixture
def client(monkeypatch):
    """The route, with Firestore and the queue faked and nothing paid."""
    db = _DB()
    monkeypatch.setattr(deps, "spend_client", lambda: db)

    enqueued: list[tuple] = []

    def fake_enqueue(queue, path, payload, *, task_id=None):
        enqueued.append((queue, path, payload, task_id))
        return True

    monkeypatch.setattr(queues, "enqueue", fake_enqueue)

    scored: list[tuple] = []

    async def fake_score_pending_jobs(user_id, **kwargs):
        scored.append((user_id, kwargs))
        return {"scored": 1, "discarded": 0, "failed": 0, "pending": 1}

    async def noop_cost(*a, **kw):
        return None

    monkeypatch.setattr(jobs, "score_pending_jobs", fake_score_pending_jobs)
    monkeypatch.setattr(jobs, "persist_run_cost", noop_cost)

    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), db, enqueued, scored


def test_the_score_route_is_gated_and_budget_capped(client, monkeypatch):
    """T10. Two halves, and both are load-bearing.

    *Gated*: an unconfirmed click gets 402 with a quote and does no work.
    *Budget-capped*: the confirmed click still goes through the same scorer
    with ``cycle_id=None``, so it draws down the window discovery opened
    rather than opening a fresh one. A consent seam that also handed out slots
    would be a way to reset "200 per cycle" by clicking twice.
    """
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    http, _db, enqueued, scored = client

    refused = http.post("/jobs/score", json={})

    assert refused.status_code == 402
    detail = refused.json()["detail"]
    assert detail["action"] == estimate.SCORE_BACKLOG
    assert detail["estimate"]["units"] > 0
    assert (enqueued, scored) == ([], []), "a refused click did work anyway"

    ok = http.post("/jobs/score", json={"confirm": detail["confirm_token"]})

    assert ok.status_code == 200
    assert ok.json()["estimate"]["units"] == detail["estimate"]["units"]
    # TestClient runs background tasks synchronously, so the work has already
    # happened by the time the response lands.
    assert [uid for uid, _ in scored] == ["u1"]
    assert scored[0][1] == {"cycle_id": None}


def test_the_quote_never_exceeds_the_per_day_cap(client, monkeypatch):
    """The number shown is a reading of the grant, so it cannot promise more
    work than the budget will allow."""
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "200")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "400")
    http, _db, _enqueued, _scored = client

    quote = http.post("/jobs/score", json={}).json()["detail"]["estimate"]

    assert quote["units"] <= budget.Limits.from_env().per_cycle
    assert quote["units"] <= budget.Limits.from_env().per_day
    assert quote["caps"]["per_cycle"] == 200 and quote["caps"]["per_day"] == 400


def test_a_confirmed_click_goes_to_the_batch_capable_worker_route(client, monkeypatch):
    """**Not ``/tasks/score``**, which is online-only.

    Before the spend safeguards, the user asking for their backlog to be
    scored reached ``batch_runs.score_or_start_run``, which sends a backlog
    over ``BATCH_MIN_PENDING`` to a half-price Vertex batch. Pointing the new
    button at the online scorer would have silently doubled the cost of the
    exact workflow this PR replaced — ~$1.96 per 200 jobs where it had been
    ~$0.98 — and made the ``BATCH_MULTIPLIER`` floor of every quote a price
    the product structurally cannot reach.
    """
    monkeypatch.setenv("QUEUE_MODE", "1")
    http, _db, enqueued, scored = client
    token = http.post("/jobs/score", json={}).json()["detail"]["confirm_token"]

    resp = http.post("/jobs/score", json={"confirm": token})

    assert resp.status_code == 200 and resp.json()["mode"] == "queued"
    assert [(q, p) for q, p, _, _ in enqueued] == [
        ("score", jobs.SCORE_BACKLOG_TASK_PATH)
    ]
    assert jobs.SCORE_BACKLOG_TASK_PATH == "/tasks/score/backlog"
    # Nothing ran in this process — the worker owns the spend from here.
    assert scored == []


def test_a_local_process_cannot_score_a_backlog_against_production(client, monkeypatch):
    """The same guard the discovery routes carry, for the same reason: an
    enqueue from a laptop hands the real worker the same paid work, one
    process further away.

    **And it runs before the seam, which is a real ordering claim and not a
    turn of phrase.** FastAPI consumes — and deletes — the consent token
    while solving dependencies, before any line of the handler body. A 403
    raised in the body would therefore refuse the request *and* burn the
    confirmation, so the developer would have to confirm again to be refused
    again. It is a route-level dependency instead, which FastAPI inserts
    ahead of the signature's own. The surviving token is what proves it.
    """
    http, _db, enqueued, scored = client
    token = http.post("/jobs/score", json={}).json()["detail"]["confirm_token"]
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    resp = http.post("/jobs/score", json={"confirm": token})

    assert resp.status_code == 403
    assert discovery.LIVE_RUN_OVERRIDE in resp.json()["detail"]
    assert (enqueued, scored) == ([], [])

    # The token was not spent by the refusal: the same one still works once
    # the bypass is off. If the 403 moved into the handler body this would
    # 402 instead.
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    assert http.post("/jobs/score", json={"confirm": token}).status_code == 200
