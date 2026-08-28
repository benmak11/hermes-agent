# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Worker task handlers + the queue/in-process dispatch seam.

Pins the security contract (task routes 404 without WORKER_MODE, so the
public API service never exposes them) and the dispatch behavior on both
sides of QUEUE_MODE.

The Phase 2 funnel routes (``/tasks/tailor``, ``/tasks/apply``) add two more
properties to that contract: they ship with **no caller anywhere in the repo**
(the deploy ordering seam — one merge for the handler, the next for the
enqueue, so the API can never enqueue to a route the worker doesn't have yet),
and a redelivered task is a no-op that spends nothing rather than a second LLM
run or a duplicate real job application.
"""

import ast
import asyncio
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.transforms import ArrayUnion
from pydantic import BaseModel

import api.routes.applications as applications
import api.routes.discovery as discovery
import api.routes.worker as worker
from obs.logging import current_run_id
from tools.applications import state
from tools.matching import budget
from tools.queues import KNOWN_QUEUES

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(worker.router)
    return TestClient(app)


@pytest.fixture
def cost_flushes(monkeypatch):
    """Capture the ledger flush instead of building a real Firestore client."""
    flushes: list[tuple] = []

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        flushes.append((user_id, run_id, meta))

    monkeypatch.setattr(worker, "persist_run_cost", fake_persist_run_cost)
    return flushes


def test_task_routes_404_without_worker_mode(client, monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)
    for path in ("/tasks/discovery", "/tasks/sweep"):
        assert client.post(path, json={"user_id": "u1"}).status_code == 404
    for path in ("/tasks/score", "/tasks/batch/start"):
        assert client.post(path, json={"user_id": "u1"}).status_code == 404
    assert client.post("/tasks/batch/resume").status_code == 404
    # The funnel routes are on the same contract: on hermes-api they don't
    # exist, and the check runs before anything touches Firestore.
    assert (
        client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "j1"}).status_code
        == 404
    )
    assert (
        client.post("/tasks/apply", json={"user_id": "u1", "app_id": "a1"}).status_code
        == 404
    )


def test_task_discovery_runs_cycle_inline(client, monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    calls = []

    async def fake_cycle(user_id, *, trigger):
        calls.append((user_id, trigger))

    monkeypatch.setattr(worker, "run_discovery_cycle", fake_cycle)
    resp = client.post("/tasks/discovery", json={"user_id": "u1", "trigger": "manual"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert calls == [("u1", "manual")]


def test_task_score_returns_counts(client, monkeypatch, cost_flushes):
    monkeypatch.setenv("WORKER_MODE", "1")
    bound = []

    async def fake_score(user_id, *, limit=None, cycle_id=budget.CURRENT_RUN):
        assert (user_id, limit) == ("u1", 50)
        # Measured under its own run_id, but spending out of the window
        # discovery opened — otherwise the per-cycle cap resets on demand for
        # anything that can reach this route.
        assert cycle_id is None
        bound.append(current_run_id())
        return {"scored": 3, "discarded": 1, "failed": 0, "pending": 4}

    monkeypatch.setattr(worker, "score_pending_jobs", fake_score)
    resp = client.post("/tasks/score", json={"user_id": "u1", "limit": 50})
    assert resp.status_code == 200
    assert resp.json()["scored"] == 3
    # A run context, or this handler's spend is invisible to the ledger, its
    # job docs land with a null scored_run_id, and its budget reservation has
    # no cycle key.
    assert bound[0] is not None
    user_id, run_id, meta = cost_flushes[0]
    assert (user_id, run_id) == ("u1", bound[0])
    assert meta["runner"] == "score_task"
    assert meta["jobs"] == {"pending": 4, "scored": 3, "discarded": 1, "failed": 0}


def test_task_score_flushes_cost_even_when_scoring_dies(
    client, monkeypatch, cost_flushes
):
    monkeypatch.setenv("WORKER_MODE", "1")

    async def explode(user_id, *, limit=None, cycle_id=budget.CURRENT_RUN):
        raise RuntimeError("pro call died mid-run")

    monkeypatch.setattr(worker, "score_pending_jobs", explode)
    with pytest.raises(RuntimeError):
        client.post("/tasks/score", json={"user_id": "u1"})
    # The run still paid for whatever it got through.
    assert cost_flushes[0][2]["runner"] == "score_task"


def test_score_task_cannot_turn_off_the_budget():
    """The scoring cap must not be settable over HTTP — the only escape hatch
    is cli/run_matching --ignore-budget."""
    assert "ignore_budget" not in worker.ScoreTask.model_fields
    task = worker.ScoreTask.model_validate({"user_id": "u1", "ignore_budget": True})
    assert not hasattr(task, "ignore_budget")


def test_task_batch_start_and_resume_call_through(client, monkeypatch, cost_flushes):
    monkeypatch.setenv("WORKER_MODE", "1")
    calls = []

    async def fake_start(user_id, *, limit=None, cycle_id=budget.CURRENT_RUN):
        assert cycle_id is None  # same contract as /tasks/score
        calls.append(("start", user_id, limit))
        return {"started": True, "run": "r1", "stage": "parse", "pending": 9}

    async def fake_resume():
        calls.append(("resume",))
        return {"checked": 1, "running": 0, "advanced": 0, "completed": 1, "failed": 0}

    monkeypatch.setattr(worker.batch_runs, "start", fake_start)
    monkeypatch.setattr(worker.batch_runs, "resume", fake_resume)

    resp = client.post("/tasks/batch/start", json={"user_id": "u1", "limit": 9})
    assert resp.status_code == 200 and resp.json()["run"] == "r1"
    resp = client.post("/tasks/batch/resume")
    assert resp.status_code == 200 and resp.json()["completed"] == 1
    assert calls == [("start", "u1", 9), ("resume",)]
    # The submitting task banks a ledger doc under the run_id the batch run
    # records as origin_run_id, so the resume pass's late pricing joins it.
    user_id, _run_id, meta = cost_flushes[0]
    assert (user_id, meta["runner"], meta["batch_run"]) == ("u1", "batch_start", "r1")
    # Only /tasks/batch/start flushes; the resume pass banks per batch run.
    assert len(cost_flushes) == 1


def test_cron_tick_enqueues_batch_resume_only_when_runs_in_flight(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.setenv("QUEUE_MODE", "1")
    enqueued = []
    monkeypatch.setattr(
        discovery.queues,
        "enqueue",
        lambda q, p, b, *, task_id=None: enqueued.append((q, p, task_id)) or True,
    )

    class _FakeQuery:
        def __init__(self, docs):
            self._docs = docs

        def where(self, filter=None):
            return self

        def limit(self, n):
            return self

        def get(self):
            return self._docs

    class _FakeClient:
        def __init__(self, docs):
            self._docs = docs

        def collection(self, name):
            assert name == "batch_runs"
            return _FakeQuery(self._docs)

    monkeypatch.setattr(discovery, "_client", lambda: _FakeClient([]))
    assert discovery.maybe_enqueue_batch_resume() is False
    assert enqueued == []

    monkeypatch.setattr(discovery, "_client", lambda: _FakeClient([object()]))
    assert discovery.maybe_enqueue_batch_resume() is True
    queue, path, task_id = enqueued[0]
    assert (queue, path) == ("score", "/tasks/batch/resume")
    assert task_id.startswith("batch-resume-") and len(task_id.split("-")[-1]) == 10

    # Not on the worker (or queues off): the tick never touches Firestore.
    monkeypatch.delenv("WORKER_MODE")
    monkeypatch.setattr(
        discovery, "_client", lambda: (_ for _ in ()).throw(AssertionError)
    )
    assert discovery.maybe_enqueue_batch_resume() is False


def test_dispatch_cycle_enqueues_when_queue_mode_on(monkeypatch):
    monkeypatch.setenv("QUEUE_MODE", "1")
    enqueued = []

    def fake_enqueue(queue, path, payload, *, task_id=None):
        enqueued.append((queue, path, payload, task_id))
        return True

    monkeypatch.setattr(discovery.queues, "enqueue", fake_enqueue)

    ok = asyncio.run(discovery.dispatch_cycle("discovery", "u1", trigger="cron"))

    assert ok is True
    queue, path, payload, task_id = enqueued[0]
    assert (queue, path) == ("discovery", "/tasks/discovery")
    assert payload == {"user_id": "u1", "trigger": "cron"}
    # Scheduled work dedupes at hour granularity: cron-discovery-u1-YYYYMMDDHH
    assert (
        task_id.startswith("cron-discovery-u1-") and len(task_id.split("-")[-1]) == 10
    )


def test_dispatch_cycle_manual_dedupes_at_minute_granularity(monkeypatch):
    monkeypatch.setenv("QUEUE_MODE", "1")
    enqueued = []
    monkeypatch.setattr(
        discovery.queues,
        "enqueue",
        lambda q, p, b, *, task_id=None: enqueued.append(task_id) or True,
    )

    asyncio.run(discovery.dispatch_cycle("sweep", "u1", trigger="manual"))

    assert len(enqueued[0].split("-")[-1]) == 12  # YYYYMMDDHHMM


def test_dispatch_cycle_runs_inline_without_queue_mode(monkeypatch):
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    calls = []

    async def fake_cycle(user_id, *, trigger):
        calls.append((user_id, trigger))

    monkeypatch.setattr(discovery, "run_discovery_cycle", fake_cycle)

    ok = asyncio.run(discovery.dispatch_cycle("discovery", "u1", trigger="cron"))

    assert ok is True
    assert calls == [("u1", "cron")]


# --------------------------------------------------------------------------
# Phase 2: the funnel routes.
#
# Fakes are a trimmed copy of tests/unit/test_application_state.py's — same
# rule, and for the same reason: this suite is *about* the compare-and-swap, so
# the fake honours update_time for real. Every write bumps a version and a
# write carrying a stale one raises FailedPrecondition, exactly as Firestore
# does.
# --------------------------------------------------------------------------


def _resolve(target: dict, fields: dict) -> None:
    for key, value in fields.items():
        if value is firestore.DELETE_FIELD:
            target.pop(key, None)
        elif isinstance(value, ArrayUnion):
            target[key] = list(target.get(key) or []) + list(value.values)
        else:
            target[key] = value


class _FakeSnap:
    def __init__(self, doc_id, data, update_time):
        self.id = doc_id
        self.update_time = update_time
        self.exists = data is not None
        self._data = None if data is None else dict(data)

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDoc:
    def __init__(self, data=None, doc_id="app-job1"):
        self.id = doc_id
        self._data = None if data is None else dict(data)
        self._version = 1
        self.updates: list[tuple[dict, object]] = []

    @property
    def data(self) -> dict | None:
        return None if self._data is None else dict(self._data)

    def get(self):
        return _FakeSnap(self.id, self._data, self._version)

    def update(self, fields, option=None):
        self.updates.append((fields, option))
        if self._data is None:
            raise NotFound("no such document")
        if option is not None and option._last_update_time != self._version:
            raise FailedPrecondition("stale last_update_time")
        _resolve(self._data, fields)
        self._version += 1


class _FakeCollection:
    def __init__(self, doc: _FakeDoc):
        self._doc = doc

    def document(self, doc_id):
        return self._doc


class _FakeUser:
    """A user document with an ``applications`` and a ``jobs`` subcollection."""

    def __init__(self, app_doc: _FakeDoc, job_doc: _FakeDoc, profile: dict | None):
        self._app, self._job, self._profile = app_doc, job_doc, profile

    def get(self):
        return _FakeSnap("u1", self._profile, 1)

    def collection(self, name):
        return _FakeCollection(self._app if name == "applications" else self._job)


class _FakeDb:
    def __init__(self, user: _FakeUser):
        self._user = user

    def collection(self, name):
        assert name == "users"
        return _FakeCollection(self._user)


def _app_doc(status: str, **extra) -> _FakeDoc:
    return _FakeDoc(
        {
            "id": "app-job1",
            "user_id": "u1",
            "job_id": "job1",
            "status": status,
            "timeline": [{"at": "2026-08-01T00:00:00+00:00", "status": "queued"}],
            **extra,
        }
    )


@pytest.fixture
def tailoring_world(monkeypatch):
    """``run_tailoring`` for real, over fake Firestore, with the paid call wired
    to a recorder so 'did this spend an LLM run?' is directly observable."""
    monkeypatch.setenv("WORKER_MODE", "1")
    tailorings: list = []

    async def fake_tailor_application(job, profile, upload=True):
        tailorings.append(job)
        raise AssertionError("a claimed run should not get this far in this test")

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        pass

    monkeypatch.setattr(applications, "tailor_application", fake_tailor_application)
    monkeypatch.setattr(applications, "persist_run_cost", fake_persist_run_cost)

    def install(app_doc: _FakeDoc):
        user = _FakeUser(app_doc, _FakeDoc(None, doc_id="job1"), None)
        monkeypatch.setattr(applications, "_client", lambda: _FakeDb(user))
        return app_doc

    return install, tailorings


def test_task_tailor_runs_the_tailoring_task(client, monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    calls = []

    async def fake_run_tailoring(user_id, job_id):
        calls.append((user_id, job_id))

    monkeypatch.setattr(worker, "run_tailoring", fake_run_tailoring)
    resp = client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "job1"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert calls == [("u1", "job1")]


def test_a_duplicate_tailor_delivery_spends_nothing(client, tailoring_world):
    """Cloud Tasks delivers at least once. The second delivery must not buy a
    second LLM run — and the claim that stops it lives in ``run_tailoring``,
    not in the handler, so the handler adds none of its own."""
    install, tailorings = tailoring_world
    doc = install(_app_doc("tailoring"))  # delivery #1 is already running

    resp = client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "job1"})

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert tailorings == []  # nothing paid for
    assert doc.updates == []  # and nothing written
    assert doc.data["status"] == "tailoring"


def test_the_first_tailor_delivery_does_claim(client, tailoring_world):
    """Positive control for the test above: from ``queued`` the same fixture
    claims, so the no-op is the claim losing, not an inert fake."""
    install, tailorings = tailoring_world
    doc = install(_app_doc("queued"))

    assert (
        client.post(
            "/tasks/tailor", json={"user_id": "u1", "job_id": "job1"}
        ).status_code
        == 200
    )

    # Claimed (queued → tailoring), then failed on the missing job document —
    # which is as far as this fixture goes on purpose; the point is the claim.
    assert [e["status"] for e in doc.data["timeline"]] == [
        "queued",
        "tailoring",
        "failed",
    ]
    assert tailorings == []


def test_the_tailoring_claim_carries_its_lease_in_the_same_write(
    client, tailoring_world
):
    """A claim with no lease is a claim nothing can recover from.

    The tailor queue retries (``max_attempts = 2``), and a retry correctly
    refuses the now-illegal ``queued → tailoring`` edge — so a worker killed
    mid-run leaves the document in ``tailoring`` forever and the work is
    silently dropped, the user's only clue being a page that never changes.
    The lease is what makes that recoverable, and it has to land in the *same*
    write as the status: a claim that is briefly unleased is a claim a reaper
    can see as abandoned while it is still being taken.
    """
    install, _ = tailoring_world
    doc = install(_app_doc("queued"))

    client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "job1"})

    claim, _option = doc.updates[0]
    assert claim["status"] == "tailoring"
    assert claim["lease"]["status"] == "tailoring" and claim["lease"]["owner"]
    assert state.lease_is_held({"lease": claim["lease"]})


def test_a_finished_tailoring_run_hands_its_lease_back(client, monkeypatch):
    """...and every exit has to, or the lease outlives the run that took it.

    A tailoring lease left on a ``ready_for_review`` document is still live 31
    minutes later when the user clicks Submit. The status write succeeds, the
    worker's own claim on ``submitting`` then finds a lease held by a run that
    ended long ago, refuses, and the submission is dropped in silence.
    """
    monkeypatch.setenv("WORKER_MODE", "1")
    doc = _app_doc("queued")
    job = _FakeDoc(
        {
            "id": "job1",
            "url": "https://x/y",
            "company": "Acme",
            "title": "Staff Engineer",
            "user_decision": "approved",
        }
    )
    monkeypatch.setattr(
        applications, "_client", lambda: _FakeDb(_FakeUser(doc, job, {}))
    )
    monkeypatch.setattr(
        applications, "MasterProfile", SimpleNamespace(model_validate=lambda d: d)
    )
    monkeypatch.setattr(
        applications,
        "Job",
        SimpleNamespace(model_validate=lambda d: SimpleNamespace(**d)),
    )

    async def fake_check_posting(job):
        return "ok"

    async def fake_tailor_application(job, profile, upload=True):
        assert state.lease_is_held(doc.data), "the lease must be held *during* the run"
        return SimpleNamespace(
            model_dump=lambda **kw: {"objective_text": "hi", "status": "nonsense"},
            resume_variant_uri="gs://b/r.docx",
        )

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        pass

    monkeypatch.setattr(applications, "check_posting", fake_check_posting)
    monkeypatch.setattr(applications, "tailor_application", fake_tailor_application)
    monkeypatch.setattr(applications, "persist_run_cost", fake_persist_run_cost)

    client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "job1"})

    assert doc.data["status"] == "ready_for_review"
    assert doc.data is not None and "lease" not in doc.data
    assert doc.data["objective_text"] == "hi"  # the content write still landed
    # ...and the document is claimable again, which is the whole point.
    state.try_transition(doc, doc.get(), "submitting")
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="w") is True


@pytest.fixture
def apply_world(monkeypatch):
    """The real ``/tasks/apply`` claim over a fake document, with
    ``run_submission`` replaced by a recorder — 'did we drive the browser
    twice?' is then directly observable."""
    monkeypatch.setenv("WORKER_MODE", "1")
    submissions: list[tuple] = []

    async def fake_run_submission(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))

    monkeypatch.setattr(worker, "run_submission", fake_run_submission)

    def install(app_doc: _FakeDoc):
        monkeypatch.setattr(worker, "application_ref", lambda u, a: app_doc)
        return app_doc

    return install, submissions


def test_task_apply_claims_the_lease_for_the_length_of_the_run(
    client, apply_world, monkeypatch
):
    """The lease is held *while* the run runs — the claim exists to fence the
    work, not to mark the document afterwards."""
    install, submissions = apply_world
    doc = install(_app_doc("submitting"))
    held: list[dict] = []

    async def observe(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))
        held.append(doc.data["lease"])  # what a concurrent worker would see

    monkeypatch.setattr(worker, "run_submission", observe)

    resp = client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})

    assert resp.status_code == 200 and resp.json() == {"ok": True, "ran": True}
    assert submissions == [("u1", "app-job1", False)]
    # The lease is the worker's claim; the status is the API's, taken already.
    assert held[0]["status"] == "submitting" and held[0]["owner"]
    assert doc.data["status"] == "submitting"


def test_a_duplicate_apply_delivery_does_not_submit_twice(apply_world, monkeypatch):
    """The one that matters: a redelivered task arriving **while the first is
    still running** must not put a second real application into an employer's
    ATS. The status can't refuse it — the document is already ``submitting``
    because that is how the API claimed it — so the live lease is what says no.

    Driven through the handler coroutines directly rather than the TestClient:
    the whole point is that the two deliveries overlap, which sequential HTTP
    calls cannot express.
    """
    install, submissions = apply_world
    doc = install(_app_doc("submitting"))
    monkeypatch.setenv("WORKER_MODE", "1")
    second_arrived = asyncio.Event()
    running = asyncio.Event()

    async def slow_submission(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))
        running.set()
        await second_arrived.wait()  # still driving the browser

    monkeypatch.setattr(worker, "run_submission", slow_submission)
    body = worker.ApplyTask(user_id="u1", app_id="app-job1")

    async def scenario():
        first = asyncio.create_task(worker.task_apply(body))
        await running.wait()
        second = await worker.task_apply(body)  # redelivered mid-run
        second_arrived.set()
        return await first, second

    first_result, second_result = asyncio.run(scenario())

    assert first_result == {"ok": True, "ran": True}
    # 200 and no work, not an error: a retry would only ask the same question.
    assert second_result == {"ok": True, "ran": False}
    assert submissions == [("u1", "app-job1", False)]
    assert len(doc.data["timeline"]) == 1  # the no-op wrote nothing at all


def test_apply_is_a_no_op_once_the_submission_reported_back(
    client, apply_world, monkeypatch
):
    """The terminal write releases the lease, so a task redelivered *after* the
    run finished can't be let through by the lease being gone — the status has
    left ``submitting`` and that is the second half of the claim."""
    install, submissions = apply_world
    doc = install(_app_doc("submitting"))

    async def submit_and_record(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))
        state.try_transition(doc, doc.get(), "submitted", lease=state.CLEAR_LEASE)

    monkeypatch.setattr(worker, "run_submission", submit_and_record)
    assert client.post(
        "/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"}
    ).json()["ran"]
    assert doc.data is not None and "lease" not in doc.data

    late = client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})

    assert late.status_code == 200 and late.json()["ran"] is False
    assert submissions == [("u1", "app-job1", False)]


def test_a_lease_left_by_a_lost_terminal_write_is_handed_back(
    client, apply_world, monkeypatch
):
    """Every terminal transition carries CLEAR_LEASE, so the outcome and the
    release are one write — except when that write *loses*, which is exactly
    when it leaves our lease behind on someone else's status."""
    install, submissions = apply_world
    doc = install(_app_doc("submitting"))

    async def outcome_lost(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))
        # cli/unwedge_submitting got there first; the run's own terminal write
        # is refused, and the lease it would have cleared survives it.
        state.try_transition(doc, doc.get(), "failed", note="unwedged")
        assert doc.data["lease"]  # still ours at this point

    monkeypatch.setattr(worker, "run_submission", outcome_lost)
    client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})

    assert doc.data["status"] == "failed"
    assert doc.data is not None and "lease" not in doc.data


def test_a_run_that_records_no_outcome_keeps_its_lease(
    client, apply_world, monkeypatch
):
    """The opposite case, and the dangerous one: the run ended with the document
    still ``submitting``, so whether the form went in is unknown. Releasing here
    would invite a redelivery straight back into it — the lease is left to
    expire, which is what unwedge_submitting exists to adjudicate."""
    install, submissions = apply_world
    doc = install(_app_doc("submitting"))

    async def no_outcome(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))

    monkeypatch.setattr(worker, "run_submission", no_outcome)
    client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})

    assert doc.data["status"] == "submitting"
    assert doc.data["lease"]["status"] == "submitting"


def test_apply_is_a_no_op_for_a_missing_application(client, apply_world):
    install, submissions = apply_world
    install(_FakeDoc(None))
    resp = client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "ran": False}
    assert submissions == []


def test_apply_dry_run_only_runs_from_a_submittable_status(client, apply_world):
    """A rehearsal has no business driving a browser at an application that is
    mid-submit or already submitted."""
    install, submissions = apply_world
    doc = install(_app_doc("ready_for_review"))
    assert client.post(
        "/tasks/apply",
        json={"user_id": "u1", "app_id": "app-job1", "dry_run": True},
    ).json() == {"ok": True, "ran": True, "dry_run": True}
    assert submissions == [("u1", "app-job1", True)]
    # No claim of any kind: a dry run writes no status, so there is nothing for
    # a repeat to corrupt and nothing to take a lease on.
    assert doc.updates == []

    for blocked in ("submitting", "submitted", "queued"):
        install(_app_doc(blocked))
        resp = client.post(
            "/tasks/apply",
            json={"user_id": "u1", "app_id": "app-job1", "dry_run": True},
        )
        assert resp.json() == {"ok": True, "ran": False, "dry_run": True}, blocked
    assert len(submissions) == 1


@pytest.fixture
def submission_world(monkeypatch):
    """``run_submission`` for real, with everything past the document faked:
    profile, job, the ATS liveness check, GCS and the submitter itself. Returns
    the application document plus hooks for the two things that talk to the
    outside world, so a test can decide what the network does and when."""
    doc = _app_doc("ready_for_review", resume_variant_uri="gs://b/r.docx")
    job = _FakeDoc(
        {"id": "job1", "url": "https://x/y", "user_decision": "approved"}, "job1"
    )
    monkeypatch.setattr(
        applications, "_client", lambda: _FakeDb(_FakeUser(doc, job, {}))
    )
    monkeypatch.setattr(
        applications, "MasterProfile", SimpleNamespace(model_validate=lambda d: d)
    )
    monkeypatch.setattr(
        applications,
        "Job",
        SimpleNamespace(model_validate=lambda d: SimpleNamespace(**d)),
    )
    monkeypatch.setattr(
        applications, "download_resume", lambda uri: Path("/tmp/r.docx")
    )

    hooks = SimpleNamespace(posting="ok", result={"success": True, "dry_run": True})
    submits: list[dict] = []

    async def check_posting(job):
        return hooks.posting() if callable(hooks.posting) else hooks.posting

    async def submit_application(job, prof, resume, **kw):
        submits.append(kw)
        if kw.get("on_progress"):
            kw["on_progress"]("Attaching resume", "submitting")
        return hooks.result

    monkeypatch.setattr(applications, "check_posting", check_posting)
    monkeypatch.setattr(applications, "submit_application", submit_application)
    return SimpleNamespace(doc=doc, job=job, hooks=hooks, submits=submits)


def test_a_dry_run_never_records_a_submission(submission_world):
    """``submit_greenhouse`` reports a dry run as ``success=True`` (with
    ``dry_run=True``). Treating that as a real success would mark a job
    applied-to when nothing was submitted — so the dry-run path writes no
    status of its own, in either direction."""
    doc = submission_world.doc

    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    assert submission_world.submits[0]["dry_run"] is True  # never a live click
    assert doc.data["status"] == "ready_for_review"
    assert "submitted" not in [e["status"] for e in doc.data["timeline"]]
    assert "confirmation" not in (doc.data or {})
    assert not (doc.data or {}).get("screenshots")
    assert "nothing was submitted" in doc.data["timeline"][-1]["note"]


def test_rehearsal_progress_notes_cannot_be_read_as_a_real_submission(
    submission_world,
):
    """The submitter emits the same step labels either way ("Attaching
    resume", status ``submitting``). Written through unchanged they render on
    the tracking page as a submission in progress, against a document nobody
    submitted — so a rehearsal's notes are labelled and keep the entry's status
    where the document actually is."""
    doc = submission_world.doc

    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    added = doc.data["timeline"][1:]
    assert added, "the submitter's progress never reached the timeline"
    for event in added:
        assert event["note"].startswith(applications.DRY_RUN_NOTE)
        assert event["status"] == "ready_for_review"
    assert "submitting" not in [e["status"] for e in doc.data["timeline"]]


def test_a_real_submission_keeps_the_submitters_own_progress_labels(
    submission_world,
):
    """Positive control for the test above: the marking is specific to
    rehearsals, and the live SSE stream still sees the real step labels."""
    submission_world.doc._data["status"] = "submitting"
    submission_world.hooks.result = {"success": True}

    asyncio.run(applications.run_submission("u1", "app-job1"))

    timeline = submission_world.doc.data["timeline"]
    assert ("submitting", "Attaching resume") in [
        (e["status"], e.get("note")) for e in timeline
    ]
    assert submission_world.doc.data["status"] == "submitted"


def test_a_rehearsal_cannot_park_a_document_a_real_submission_took_over(
    submission_world,
):
    """**Filter outside, swap inside — the third time this phase.**

    ``check_posting`` is a network round trip. A rehearsal that starts on a
    ``ready_for_review`` document, and returns from that call to find the user
    has clicked Submit, must not park the document at ``posting_removed``:
    ``submitting → posting_removed`` is a legal edge, so only the precondition
    *inside* the swap can refuse it. Getting this wrong clears the lease under a
    live browser and destroys the confirmation evidence for an application that
    really was sent.
    """
    doc = submission_world.doc

    def user_clicks_submit_mid_check():
        state.try_transition(doc, doc.get(), "submitting")
        state.try_claim_lease(doc, doc.get(), "submitting", owner="the-real-run")
        return "removed"

    submission_world.hooks.posting = user_clicks_submit_mid_check

    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    assert doc.data["status"] == "submitting"
    assert doc.data["lease"]["owner"] == "the-real-run"
    assert "posting_removed" not in [e["status"] for e in doc.data["timeline"]]
    assert submission_world.submits == []  # the rehearsal still stopped


def test_an_uncontested_rehearsal_does_record_a_dead_posting(submission_world):
    """Positive control: the guard narrows the precondition, it does not turn
    the pre-flight check off. The posting really being gone is a fact about the
    world, and a rehearsal that owns the document still records it."""
    submission_world.hooks.posting = "removed"

    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    assert submission_world.doc.data["status"] == "posting_removed"
    assert submission_world.job.data["user_decision"] == "dismissed"


def test_a_failing_dry_run_does_not_mark_a_real_application_failed(monkeypatch):
    """``ready_for_review → failed`` is a legal edge, so the guard has to be
    explicit: a rehearsal that blows up must not consume the user's document."""
    doc = _app_doc("ready_for_review")  # no resume_variant_uri: run_submission raises
    user = _FakeUser(doc, _FakeDoc({"id": "job1", "url": "https://x/y"}, "job1"), {})
    monkeypatch.setattr(applications, "_client", lambda: _FakeDb(user))

    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    assert doc.data["status"] == "ready_for_review"
    assert doc.data["timeline"][-1]["note"].startswith(applications.DRY_RUN_NOTE)
    assert "errored" in doc.data["timeline"][-1]["note"]


def test_dry_run_is_worker_only():
    """Mirror of test_score_task_cannot_turn_off_the_budget: a switch that must
    never be reachable from the public HTTP surface, pinned by shape rather
    than by exercising a path.

    Every route module is scanned, not a hand-picked subset — the gap this
    closes is a *new* public model somewhere nobody thought to list.
    """
    assert worker.ApplyTask.model_fields["dry_run"].default is False

    route_modules = [
        importlib.import_module(f"api.routes.{path.stem}")
        for path in sorted(Path(applications.__file__).parent.glob("*.py"))
        if not path.stem.startswith("_")
    ]
    assert {m.__name__ for m in route_modules} == {
        f"api.routes.{name}"
        for name in (
            "applications",
            "companies",
            "discovery",
            "jobs",
            "profile",
            "worker",
        )
    }
    for module in route_modules:
        for name, obj in vars(module).items():
            if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
                continue
            if module is worker and obj is worker.ApplyTask:
                continue  # the one place it is allowed to exist
            assert "dry_run" not in obj.model_fields, f"{module.__name__}.{name}"

    # Keyword-only, so submit()'s positional call cannot reach it by accident.
    param = inspect.signature(applications.run_submission).parameters["dry_run"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False


#: The one module allowed to build an enqueue argument at runtime:
#: ``dispatch_cycle`` interpolates ``kind`` into the path while hard-coding the
#: ``discovery`` queue. Anywhere else, a computed queue or path is something
#: this test cannot vouch for and says so.
_DYNAMIC_OK = {Path(discovery.__file__)}


def _suspect_enqueue_lines(source: str, *, dynamic_ok: bool) -> list[int]:
    """Line numbers of ``enqueue`` calls that reach — or might reach — the
    tailor/apply queues. Reads the AST, so it is not fooled by f-strings."""
    suspect = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "enqueue" or len(node.args) < 2:
            continue
        queue, route = node.args[0], node.args[1]
        for arg, hits in ((queue, {"tailor", "apply"}), (route, None)):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if hits is None:
                    if "/tasks/tailor" in arg.value or "/tasks/apply" in arg.value:
                        suspect.append(node.lineno)
                elif arg.value in hits:
                    suspect.append(node.lineno)
            elif not dynamic_ok:
                suspect.append(node.lineno)
    return sorted(set(suspect))


def test_nothing_enqueues_to_the_funnel_routes_yet():
    """The deploy-ordering seam, pinned.

    CI deploys hermes-api and hermes-worker from the same merge. Shipping the
    handlers and their callers together would leave a window where the API
    enqueues to a worker route that 404s, so the handlers land one merge ahead,
    dead on arrival. **This test is expected to be deleted by the PR that adds
    the callers** — it is a guard on this merge, not a permanent property.
    """
    paths = {route.path for route in worker.router.routes}
    assert {"/tasks/tailor", "/tasks/apply"} <= paths
    # The queues themselves are provisioned and already known — this PR
    # verifies that rather than editing it.
    assert {"tailor", "apply"} <= KNOWN_QUEUES

    markers = (
        '"/tasks/tailor"',
        '"/tasks/apply"',
        'enqueue("tailor"',
        'enqueue("apply"',
        'dispatch_cycle("tailor"',
        'dispatch_cycle("apply"',
    )
    # The request models too — but not in the module that defines them.
    elsewhere = (
        "TailorTask(",
        "ApplyTask(",
        "import TailorTask",
        "import ApplyTask",
    )
    handlers = Path(worker.__file__)
    callers = []
    for path in REPO_ROOT.rglob("*.py"):
        if set(path.parts) & {".venv", "tests", "node_modules", "locust_env"}:
            continue
        source = path.read_text()
        names = () if path == handlers else elsewhere
        if any(marker in source for marker in markers + names):
            callers.append(str(path.relative_to(REPO_ROOT)))
        # Literals alone would miss ``enqueue(_KIND, f"/tasks/{_KIND}", ...)``,
        # which is exactly how the existing dispatch_cycle is written. So every
        # enqueue call is read from the AST: a constant naming one of these
        # queues or paths is a caller, and an argument that isn't a constant at
        # all is one nobody can rule out by reading.
        callers.extend(
            f"{path.relative_to(REPO_ROOT)}:{line}"
            for line in _suspect_enqueue_lines(source, dynamic_ok=path in _DYNAMIC_OK)
        )
    assert callers == [], f"PR B ships dead routes; these reach them: {callers}"
