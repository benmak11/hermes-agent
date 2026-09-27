# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The incident test: clicking "run now" must not start a paid batch unasked.

2026-09-26. The user clicked *Run now* on the discovery settings card. The run
persisted 9,219 jobs and then, with no estimate and no confirmation, submitted
a **paid Vertex batch** on gemini-2.5-flash. The chain was not subtle:

    POST /settings/discovery/run
      -> dispatch_cycle("discovery", ..., trigger="manual")   # QUEUE_MODE on
      -> /tasks/discovery on hermes-worker
      -> run_discovery_cycle(...)
      -> batch_runs.score_or_start_run(...)                   # queues.enabled()
      -> batch_runs.start(...)                                # >= BATCH_MIN_PENDING

``BATCH_MIN_PENDING`` is 50 and a fresh account always exceeds it, so **the
first click could never not spend.** The scoring budget capped the damage
(~$90 of backlog down to ~$2-3) — it is the thing that worked, and this test is
not about it. What was missing is consent: nothing anywhere in that chain asked.

The test drives the real chain rather than a stand-in for it, because the bug
lived in the *seams*: the route hands the queue a task, the worker route hands
it to the cycle, and the cycle decides on its own to spend. Only the two ends
are faked — Cloud Tasks (the enqueue is recorded and the test plays the worker
delivering it) and ``batch_runs.start`` itself, which is the paid call and the
thing being asserted about.

**No mutation is needed to prove this test can fail: it fails on the tree as it
was before the spend-safeguards PR.** That failure is the incident.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.discovery as discovery
import api.routes.worker as worker
from api.deps import verify_user
from tools import queues, spend
from tools.matching import batch_runs

#: Comfortably over ``batch_runs.BATCH_MIN_PENDING`` (50), which is the
#: threshold that turns a backlog into a paid batch submission.
BACKLOG = batch_runs.BATCH_MIN_PENDING * 2


@pytest.fixture
def paid_calls(monkeypatch):
    """Record every call to the paid batch submitter instead of making one."""
    calls: list[dict] = []

    async def fake_start(user_id, **kwargs):
        calls.append({"user_id": user_id, **kwargs})
        return {
            "started": True,
            "run": "20260926-191040-abc123",
            "stage": "parse",
            "pending": BACKLOG,
            "counts": {"scored": 0, "discarded": 0, "failed": 0},
        }

    monkeypatch.setattr(batch_runs, "start", fake_start)
    return calls


@pytest.fixture
def cycle_pipeline(monkeypatch):
    """Everything ``run_discovery_cycle`` touches that isn't the spend decision.

    The crawl, the Firestore writes and the ledger flush are faked; the
    ``queues.enabled()`` branch that chooses between online scoring and a paid
    batch run is emphatically *not*, because that branch is the bug.
    """
    jobs = [SimpleNamespace(id=f"j{i}") for i in range(BACKLOG)]

    async def fake_run_discovery(user_id):
        return {
            "jobs": jobs,
            "jobs_by_platform": {"greenhouse": len(jobs)},
            "failures": [],
            "empty_boards": [],
            "boards_cached": 0,
            "boards_fetched": 1,
        }

    async def fake_load_job_preferences(user_id):
        return None

    async def fake_persist_new_jobs(items):
        return len(items)

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        return None

    async def refuse_online_scoring(*args, **kwargs):
        raise AssertionError(
            "this test is about the batch path; QUEUE_MODE must be on for it "
            "to be testing what it claims to test"
        )

    monkeypatch.setattr(discovery, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(discovery, "load_job_preferences", fake_load_job_preferences)
    monkeypatch.setattr(discovery, "prefilter_jobs", lambda items, prefs: (items, {}))
    monkeypatch.setattr(discovery, "persist_new_jobs", fake_persist_new_jobs)

    async def fake_backlog(user_id):
        return BACKLOG

    monkeypatch.setattr(discovery, "_backlog", fake_backlog)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    monkeypatch.setattr(discovery, "score_pending_jobs", refuse_online_scoring)
    monkeypatch.setattr(batch_runs, "score_pending_jobs", refuse_online_scoring)
    monkeypatch.setattr(discovery, "_extend_slot", lambda *a, **kw: True)
    monkeypatch.setattr(discovery, "_release_slot", lambda *a, **kw: True)
    monkeypatch.setattr(
        discovery,
        "_user_ref",
        lambda uid: SimpleNamespace(
            get=lambda: SimpleNamespace(to_dict=lambda: {}),
            set=lambda *a, **kw: None,
        ),
    )


@pytest.fixture
def enqueued(monkeypatch):
    """Stand in for Cloud Tasks: record the task instead of creating it.

    Recorded rather than delivered inline — the test plays the worker itself,
    after the request is over. A nested ``TestClient`` call from inside a
    request handler deadlocks on the portal.
    """
    tasks: list[tuple[str, dict]] = []

    def fake_enqueue(queue, path, payload, *, task_id=None):
        tasks.append((path, payload))
        return True

    monkeypatch.setattr(queues, "enqueue", fake_enqueue)
    return tasks


@pytest.fixture
def app_client(monkeypatch):
    """One client serving both halves of the chain: the API route and the
    worker route the queue would push to."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    monkeypatch.setenv("WORKER_MODE", "1")
    app = FastAPI()
    app.include_router(discovery.router)
    app.include_router(worker.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def test_the_run_now_button_cannot_start_a_paid_batch_unasked(
    app_client, enqueued, cycle_pipeline, paid_calls
):
    resp = app_client.post("/settings/discovery/run")
    assert resp.status_code == 200, resp.text

    # Play Cloud Tasks: deliver whatever the route enqueued to the worker.
    assert enqueued, "the manual run enqueued nothing at all"
    for path, payload in enqueued:
        delivered = app_client.post(path, json=payload)
        assert delivered.status_code == 200, (path, delivered.text)

    assert paid_calls == [], (
        "clicking 'run now' submitted a paid Vertex batch with no estimate and "
        f"no confirmation: {paid_calls}"
    )


# ---------------------------------------------------------------------------
# The consent check on the incident route itself
#
# ``/jobs/score`` carries the seam as the ``required()`` dependency and is
# covered by test_jobs_score_route.py. **This route hand-rolls it**, because
# it is the one paid route with a legitimate free verb to fall back to — no
# token means "find only", not 402. That hand-rolling is exactly why it needs
# its own tests: a review found the whole block could be replaced with
#
#     score = bool(body is not None and body.confirm)
#
# — any non-empty string authorising a paid Vertex batch — with the entire
# suite still green, because nothing anywhere sent this route a ``confirm``
# field. An unguarded guard, on the route the incident happened on.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_now(monkeypatch):
    """``POST /settings/discovery/run`` with a fake consent store."""
    from test_spend_consent import _DB

    db = _DB()
    monkeypatch.setattr(discovery, "spend_client", lambda: db)

    dispatched: list[dict] = []

    async def fake_dispatch_cycle(kind, user_id, *, trigger, score=True):
        dispatched.append({"kind": kind, "trigger": trigger, "score": score})
        return True

    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch_cycle)
    monkeypatch.setenv("QUEUE_MODE", "1")

    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), db, dispatched


def _mint(db, action):
    """A real token, through the real seam — never a hand-written document."""
    from tools.matching import rates
    from tools.spend import consent, estimate

    quote = estimate.quote(
        action, remaining_cycle=200, remaining_day=400, rate=rates.MEASURED
    )
    return asyncio.run(consent.preflight(db, "u1", action, quote))


def test_a_run_now_with_no_token_finds_without_spending(run_now):
    client, _db, dispatched = run_now

    resp = client.post("/settings/discovery/run", json={})

    assert resp.status_code == 200 and resp.json()["scored"] is False
    assert dispatched == [{"kind": "discovery", "trigger": "manual", "score": False}]


def test_a_run_now_with_a_valid_token_scores_in_one_go(run_now):
    """The positive control the guard needs to be a guard and not an off
    switch: a real token really does buy the paid verb."""
    client, db, dispatched = run_now
    token = _mint(db, spend.DISCOVERY_SCAN)

    resp = client.post("/settings/discovery/run", json={"confirm": token})

    assert resp.status_code == 200 and resp.json()["scored"] is True
    assert dispatched == [{"kind": "discovery", "trigger": "manual", "score": True}]
    # Spent, not reusable: the second click would have to ask again.
    assert db.store == {}


def test_an_invalid_token_on_run_now_answers_402_and_spends_nothing(run_now):
    """**The fail-open shape this PR exists to close.** Any non-empty string
    must not authorise a batch.

    402 rather than a quiet downgrade to find-only: the user asked for the
    paid thing, and silently doing something cheaper instead is the same class
    of surprise in the other direction — they would be left believing their
    jobs were scored.
    """
    client, _db, dispatched = run_now

    for bogus in ("yes", "true", "deadbeef", "x" * 32):
        resp = client.post("/settings/discovery/run", json={"confirm": bogus})

        assert resp.status_code == 402, (bogus, resp.text)
        detail = resp.json()["detail"]
        assert detail["needs_confirmation"] is True
        assert detail["action"] == spend.DISCOVERY_SCAN
        assert detail["estimate"]["units"] > 0
        assert detail["confirm_token"] and detail["confirm_token"] != bogus

    assert dispatched == [], "an unconsented click dispatched a cycle anyway"


def test_the_402_from_run_now_hands_back_a_token_that_works(run_now):
    client, _db, dispatched = run_now
    token = client.post("/settings/discovery/run", json={"confirm": "no"}).json()[
        "detail"
    ]["confirm_token"]

    resp = client.post("/settings/discovery/run", json={"confirm": token})

    assert resp.status_code == 200 and resp.json()["scored"] is True
    assert dispatched[-1]["score"] is True


def test_a_score_backlog_token_does_not_authorise_a_discovery_scan(run_now):
    """Two actions, two consents. A yes to "score the jobs you already have"
    is not a yes to "crawl 198 boards and then score whatever turns up" —
    different work, different money, and the estimate the user saw was
    attached to the other one."""
    client, db, dispatched = run_now
    token = _mint(db, spend.SCORE_BACKLOG)

    resp = client.post("/settings/discovery/run", json={"confirm": token})

    assert resp.status_code == 402
    assert dispatched == []
    # The wrong-action token is burnt, so it cannot be retried against the
    # route it *would* have fitted. The store is not empty afterwards — the
    # 402 minted a fresh one — so the check is on the old token specifically.
    assert f"users/u1/spend_consents/{token}" not in db.store
    fresh = resp.json()["detail"]["confirm_token"]
    assert db.store[f"users/u1/spend_consents/{fresh}"]["action"] == (
        spend.DISCOVERY_SCAN
    )


def test_a_replayed_run_now_token_does_not_buy_a_second_batch(run_now):
    client, db, dispatched = run_now
    token = _mint(db, spend.DISCOVERY_SCAN)
    assert (
        client.post("/settings/discovery/run", json={"confirm": token}).status_code
        == 200
    )

    again = client.post("/settings/discovery/run", json={"confirm": token})

    assert again.status_code == 402
    assert [d["score"] for d in dispatched] == [True]
