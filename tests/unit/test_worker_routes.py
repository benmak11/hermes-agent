# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Worker task handlers + the queue/in-process dispatch seam.

Pins the security contract (task routes 404 without WORKER_MODE, so the
public API service never exposes them) and the dispatch behavior on both
sides of QUEUE_MODE.

The Phase 2 funnel routes (``/tasks/tailor``, ``/tasks/apply``) shipped one
merge ahead of their callers — the deploy-ordering seam, so the API could never
enqueue to a route the worker didn't have yet. This is the merge that turns the
callers on, so what is pinned here now is the other half: the dispatch helpers
enqueue to routes the worker actually serves, they commit before they enqueue,
and a redelivered task is a no-op that spends nothing rather than a second LLM
run or a duplicate real job application.
"""

import ast
import asyncio
import copy
import importlib
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.transforms import ArrayUnion
from pydantic import BaseModel

import api.routes.applications as applications
import api.routes.discovery as discovery
import api.routes.jobs as jobs
import api.routes.worker as worker
from api.deps import verify_user
from obs.logging import current_run_id
from tools import queues
from tools.applications import reaper, state
from tools.matching import budget
from tools.queues import KNOWN_QUEUES
from tools.submitters import SUBMIT_CLICKED

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


class _FakeUsers:
    """A Firestore client whose ``users`` collection streams these documents."""

    def __init__(self, docs: dict):
        self._docs = docs
        self.reads = 0

    def _snap(self, uid, data):
        def to_dict():
            self.reads += 1
            return dict(data)

        return SimpleNamespace(id=uid, to_dict=to_dict)

    def collection(self, name):
        assert name == "users"
        return SimpleNamespace(
            stream=lambda: [self._snap(uid, d) for uid, d in self._docs.items()]
        )


@pytest.fixture
def cron_world(monkeypatch):
    """``cron_tick`` over two users, with the per-user tick and reap recorded."""
    monkeypatch.setenv("WORKER_MODE", "1")
    ticked: list[tuple] = []
    reaped: list[str] = []

    async def fake_tick(user_id, *, force_check=False, doc=None):
        ticked.append((user_id, force_check, doc))

    async def fake_reap(user_id, *, background_tasks):
        reaped.append(user_id)
        return {"recovered": 0, "truncated": 0}

    users = _FakeUsers({"u1": {"discovery_settings": {}}, "u2": {}})
    monkeypatch.setattr(discovery, "tick_user", fake_tick)
    monkeypatch.setattr(discovery, "reap_user", fake_reap)
    monkeypatch.setattr(discovery, "maybe_enqueue_batch_resume", lambda: False)
    monkeypatch.setattr(discovery, "_client", lambda: users)
    return SimpleNamespace(
        ticked=ticked, reaped=reaped, tick=fake_tick, reap=fake_reap, users=users
    )


def test_cron_tick_fans_out_in_request_under_queue_mode(cron_world, monkeypatch):
    """The hourly tick's real home is hermes-worker, which does **not** run
    with ``cpu-throttling: false``. A per-user tick deferred past the response
    there runs on an instance that may already be frozen — the unattended loops
    would then fire for nobody, which is the same bug as "discovery never runs".
    Under QUEUE_MODE a tick is a settings read plus an enqueue, so it belongs in
    the request."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    background = BackgroundTasks()

    result = asyncio.run(discovery.cron_tick(background))

    assert result == {
        "ok": True,
        "users": 2,
        "failed": 0,
        "reaped": 0,
        "reap_failed": 0,
        "reap_truncated": 0,
    }
    assert [(uid, forced) for uid, forced, _doc in cron_world.ticked] == [
        ("u1", True),
        ("u2", True),
    ]
    # The reaper rides the same in-request fan-out, for the same reason.
    assert cron_world.reaped == ["u1", "u2"]
    assert background.tasks == []  # nothing left for a frozen instance to run


def test_cron_tick_hands_over_the_documents_it_already_read(cron_world, monkeypatch):
    """The fan-out streams the whole ``users`` collection, so each tick is handed
    the document rather than being left to re-fetch it."""
    monkeypatch.setenv("QUEUE_MODE", "1")

    asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert [doc for _uid, _forced, doc in cron_world.ticked] == [
        {"discovery_settings": {}},
        {},
    ]
    assert cron_world.users.reads == 2  # one materialization per streamed doc


def test_tick_user_uses_the_document_it_was_handed(monkeypatch):
    """The other half, and the one that makes the hand-over worth anything: a
    tick given a document must not go and fetch it again. Doubling the reads
    also doubles the latency of the single request Cloud Scheduler is waiting
    on, and that request now does the whole fan-out."""

    def no_reads(user_id):
        pytest.fail("tick_user re-read a document it was already given")

    monkeypatch.setattr(discovery, "_user_ref", no_reads)
    monkeypatch.setattr(discovery, "_last_tick_check", {})

    asyncio.run(
        discovery.tick_user(
            "u1",
            force_check=True,
            doc={
                "discovery_settings": {
                    "auto_discovery": False,
                    "liveness_sweep": False,
                }
            },
        )
    )


def test_tick_user_still_reads_for_itself_when_given_nothing(monkeypatch):
    """Positive control: the opportunistic callers pass no document, and the
    fixture above would pass just as well against a tick that does nothing."""
    reads: list[str] = []

    def counted(user_id):
        reads.append(user_id)
        return SimpleNamespace(
            get=lambda: SimpleNamespace(
                to_dict=lambda: {"discovery_settings": {"auto_discovery": False}}
            )
        )

    monkeypatch.setattr(discovery, "_user_ref", counted)
    monkeypatch.setattr(discovery, "_last_tick_check", {})

    asyncio.run(discovery.tick_user("u1", force_check=True))

    assert reads == ["u1"]


def test_cron_tick_still_defers_the_fan_out_without_a_queue(cron_world, monkeypatch):
    """The other side of it: with no queue a due tick runs the whole
    discovery-and-scoring cycle, which is minutes of work per user and cannot
    happen inside an HTTP request."""
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    background = BackgroundTasks()

    assert asyncio.run(discovery.cron_tick(background)) == {
        "ok": True,
        "users": 2,
        "failed": 0,
        # Deferred with the ticks, so nothing was reaped inside the request.
        "reaped": 0,
        "reap_failed": 0,
        "reap_truncated": 0,
    }

    assert cron_world.ticked == []
    assert [(t.func, t.args, t.kwargs) for t in background.tasks] == [
        (
            cron_world.tick,
            ("u1",),
            {"force_check": True, "doc": {"discovery_settings": {}}},
        ),
        (cron_world.reap, ("u1",), {"background_tasks": background}),
        (cron_world.tick, ("u2",), {"force_check": True, "doc": {}}),
        (cron_world.reap, ("u2",), {"background_tasks": background}),
    ]


def test_one_users_tick_failing_does_not_cost_the_rest_theirs(cron_world, monkeypatch):
    """Running in-request means an exception is now the *request's* problem.
    The fan-out has to survive one bad user, or a single broken profile silently
    stops every other user's unattended loops — and it has to say so."""
    monkeypatch.setenv("QUEUE_MODE", "1")

    async def explode_for_u1(user_id, *, force_check=False, doc=None):
        if user_id == "u1":
            raise RuntimeError("that user's settings doc is unreadable")
        cron_world.ticked.append((user_id, force_check, doc))

    monkeypatch.setattr(discovery, "tick_user", explode_for_u1)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert result == {
        "ok": True,
        "users": 2,
        "failed": 1,
        "reaped": 0,
        "reap_failed": 0,
        "reap_truncated": 0,
    }
    assert [uid for uid, _forced, _doc in cron_world.ticked] == ["u2"]
    # u1's tick blew up, but its stuck applications are still worth collecting.
    assert cron_world.reaped == ["u1", "u2"]


def test_a_fan_out_where_nothing_ticked_is_not_reported_as_success(
    cron_world, monkeypatch
):
    """Swallowing every failure behind a 200 is worse than failing loudly: with
    a missing queue credential *every* tick raises, Cloud Scheduler records an
    hourly success, and the unattended loops are dead with nothing to alert on.
    Answering 5xx makes the scheduler's own retry and alerting worth something.
    """
    monkeypatch.setenv("QUEUE_MODE", "1")

    async def explode(user_id, *, force_check=False, doc=None):
        raise RuntimeError("TASKS_SA_EMAIL is not set")

    monkeypatch.setattr(discovery, "tick_user", explode)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert raised.value.status_code == 500
    assert "2 users" in raised.value.detail


def test_a_broken_reaper_never_costs_the_tick_its_fan_out(cron_world, monkeypatch):
    """The reaper is wired *inside* the per-user handling but behind its own
    try/except, so it cannot promote itself into the 500 above.

    Discovery is the loop the scheduler exists for; the reaper is a repair pass
    bolted alongside it. A reaper that throws for every user — a bad index, a
    Firestore permission — must not take the hourly discovery fan-out down with
    it. It gets its own counter instead, which is the thing to alert on."""
    monkeypatch.setenv("QUEUE_MODE", "1")

    async def explode(user_id, *, background_tasks):
        raise RuntimeError("the applications query is broken")

    monkeypatch.setattr(discovery, "reap_user", explode)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert result == {
        "ok": True,
        "users": 2,
        "failed": 0,
        "reaped": 0,
        "reap_failed": 2,
        "reap_truncated": 0,
    }
    # Every tick still ran.
    assert [uid for uid, _forced, _doc in cron_world.ticked] == ["u1", "u2"]


def test_the_tick_reports_what_the_reaper_recovered(cron_world, monkeypatch):
    """A stuck application looks exactly like an idle one, so the reaper's
    *inaction* is invisible. The count comes back in the response rather than
    living only in logs."""
    monkeypatch.setenv("QUEUE_MODE", "1")

    async def recovered(user_id, *, background_tasks):
        return {"recovered": 3 if user_id == "u1" else 1, "truncated": 0}

    monkeypatch.setattr(discovery, "reap_user", recovered)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert result["reaped"] == 4
    assert result["reap_failed"] == 0


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

    def set(self, data, merge=False):
        if merge and self._data is not None:
            _resolve(self._data, data)
        else:
            self._data = {}
            _resolve(self._data, data)
        self._version += 1

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


# --------------------------------------------------------------------------
# Phase 2 PR C: the funnel's own dispatch seam.
# --------------------------------------------------------------------------


@pytest.fixture
def enqueued(monkeypatch):
    """Record what ``applications`` pushes onto a queue, and refuse the real
    client. Returns the list of (queue, path, payload, task_id)."""
    calls: list[tuple] = []

    def fake_enqueue(queue, path, payload, *, task_id=None):
        calls.append((queue, path, payload, task_id))
        return True

    monkeypatch.setattr(applications.queues, "enqueue", fake_enqueue)
    return calls


def test_the_dispatch_helpers_are_synchronous():
    """Unlike ``dispatch_cycle``, and deliberately.

    Every caller is a synchronous route (``decide``, ``regenerate``,
    ``submit``), which FastAPI already runs in a threadpool — where the
    blocking Firestore reads and compare-and-swaps those routes are built out of
    belong. An ``async`` helper would have to be reached with ``asyncio.run`` or
    by making the routes coroutines, which is exactly the arrangement
    ``tools.applications.state`` documents as the one to avoid. Neither helper
    awaits anything, so there is nothing to trade for it.
    """
    assert not inspect.iscoroutinefunction(applications.dispatch_tailor)
    assert not inspect.iscoroutinefunction(applications.dispatch_apply)


def test_dispatch_tailor_enqueues_when_queue_mode_on(monkeypatch, enqueued):
    monkeypatch.setenv("QUEUE_MODE", "1")
    background = BackgroundTasks()

    assert (
        applications.dispatch_tailor("u1", "job1", background_tasks=background) is True
    )

    queue, path, payload, task_id = enqueued[0]
    assert (queue, path) == ("tailor", "/tasks/tailor")
    assert payload == {"user_id": "u1", "job_id": "job1"}
    # Minute grain, like dispatch_cycle's `manual`: a double-click on Approve or
    # Regenerate dedupes, a deliberate regenerate a minute later doesn't.
    assert task_id.startswith("tailor-u1-job1-")
    assert len(task_id.split("-")[-1]) == 12  # YYYYMMDDHHMM
    assert background.tasks == []


def test_dispatch_tailor_falls_back_to_a_background_task(monkeypatch):
    """No queue infra: the work runs in-process, exactly as before. This is what
    keeps local dev, the tests above, and the pre-worker deployment working."""
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    monkeypatch.setattr(
        applications.queues,
        "enqueue",
        lambda *a, **k: pytest.fail("nothing may be enqueued without QUEUE_MODE"),
    )
    background = BackgroundTasks()

    assert (
        applications.dispatch_tailor("u1", "job1", background_tasks=background) is True
    )

    assert [(t.func, t.args) for t in background.tasks] == [
        (applications.run_tailoring, ("u1", "job1"))
    ]


def test_dispatch_apply_names_the_task_after_the_claim(monkeypatch, enqueued):
    """The apply task id carries the ``submit_attempts`` value the claim wrote,
    not a timestamp: same claim, same name (so the queue refuses a duplicate
    dispatch), new claim, new name however soon the retry comes."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    background = BackgroundTasks()

    assert (
        applications.dispatch_apply(
            "u1", "app-job1", attempt=3, background_tasks=background
        )
        is True
    )

    queue, path, payload, task_id = enqueued[0]
    assert (queue, path) == ("apply", "/tasks/apply")
    assert payload == {"user_id": "u1", "app_id": "app-job1"}
    assert task_id == "apply-u1-app-job1-3"
    # No dry_run in the payload: the switch is worker-only and stays that way.
    assert "dry_run" not in payload
    assert background.tasks == []


def test_dispatch_apply_falls_back_to_a_background_task(monkeypatch):
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    monkeypatch.setattr(
        applications.queues,
        "enqueue",
        lambda *a, **k: pytest.fail("nothing may be enqueued without QUEUE_MODE"),
    )
    background = BackgroundTasks()

    assert (
        applications.dispatch_apply(
            "u1", "app-job1", attempt=1, background_tasks=background
        )
        is True
    )

    assert [(t.func, t.args) for t in background.tasks] == [
        (applications.run_submission, ("u1", "app-job1"))
    ]


def test_a_deduped_dispatch_is_reported_to_the_caller(monkeypatch):
    """Returning True on a task the queue refused would tell the caller work is
    scheduled when none is."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    monkeypatch.setattr(applications.queues, "enqueue", lambda *a, **k: False)
    background = BackgroundTasks()

    assert (
        applications.dispatch_tailor("u1", "job1", background_tasks=background) is False
    )
    assert (
        applications.dispatch_apply(
            "u1", "app-job1", attempt=1, background_tasks=background
        )
        is False
    )


def test_approving_a_job_dispatches_only_after_the_application_exists(
    monkeypatch, enqueued
):
    """Commit, then enqueue — at the front of the funnel too.

    A worker can be reading the document before ``decide`` executes its next
    line. Enqueue first and ``run_tailoring`` finds nothing to claim and returns
    without spending anything, so the approval quietly never tailors.
    """
    monkeypatch.setenv("QUEUE_MODE", "1")
    app_doc = _FakeDoc(None)  # no Application yet
    job_doc = _FakeDoc({"id": "job1", "url": "https://x/y"}, "job1")
    monkeypatch.setattr(
        jobs, "_client", lambda: _FakeDb(_FakeUser(app_doc, job_doc, None))
    )
    api = FastAPI()
    api.include_router(jobs.router)
    api.dependency_overrides[verify_user] = lambda: "u1"

    resp = TestClient(api).post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    queue, path, payload, task_id = enqueued[0]
    assert (queue, path) == ("tailor", "/tasks/tailor")
    assert payload == {"user_id": "u1", "job_id": "job1"}
    assert task_id.startswith("tailor-u1-job1-")
    # The document the task names is already there, already claimable.
    assert app_doc.data[state.STATUS_FIELD] == state.INITIAL


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
    """The ``/tasks/apply`` handler over a fake document, with
    ``run_submission`` replaced by a recorder.

    Since the delivery claim moved *into* ``run_submission`` (so that the API's
    own background path is fenced by it too), what is left to pin at this level
    is the pass-through: the handler reports what the run reported, and the
    dry-run gate still refuses to open a browser on a document that isn't
    submittable. The claim itself is exercised against the real function below.
    """
    monkeypatch.setenv("WORKER_MODE", "1")
    submissions: list[tuple] = []
    outcome = SimpleNamespace(ran=True)

    async def fake_run_submission(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))
        return outcome.ran

    monkeypatch.setattr(worker, "run_submission", fake_run_submission)

    def install(app_doc: _FakeDoc):
        monkeypatch.setattr(worker, "application_ref", lambda u, a: app_doc)
        monkeypatch.setattr(
            applications, "_apps", lambda user_id: _FakeCollection(app_doc)
        )
        return app_doc

    return SimpleNamespace(install=install, submissions=submissions, outcome=outcome)


def test_task_apply_reports_whether_the_run_claimed_the_work(client, apply_world):
    """``ran`` is the callee's answer now, and the handler must not guess at it:
    "a browser was driven" and "a redelivery found the work already claimed and
    did nothing" are the two outcomes this route exists to tell apart."""
    apply_world.install(_app_doc("submitting"))

    resp = client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "ran": True}

    apply_world.outcome.ran = False  # claim lost, or the document is gone
    resp = client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})
    # 200 and no work, not an error: a retry would only ask the same question.
    assert resp.status_code == 200 and resp.json() == {"ok": True, "ran": False}
    assert apply_world.submissions == [("u1", "app-job1", False)] * 2


def test_apply_is_a_no_op_for_a_missing_application(client, monkeypatch):
    """Driven through the real ``run_submission``: the missing-document check
    moved in there with the claim, so faking the callee would test nothing."""
    monkeypatch.setenv("WORKER_MODE", "1")
    doc = _FakeDoc(None)
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))

    resp = client.post("/tasks/apply", json={"user_id": "u1", "app_id": "app-job1"})

    assert resp.status_code == 200 and resp.json() == {"ok": True, "ran": False}
    assert doc.updates == []  # nothing written to a document that isn't there


def test_apply_dry_run_only_runs_from_a_submittable_status(client, apply_world):
    """A rehearsal has no business driving a browser at an application that is
    mid-submit or already submitted."""
    doc = apply_world.install(_app_doc("ready_for_review"))
    assert client.post(
        "/tasks/apply",
        json={"user_id": "u1", "app_id": "app-job1", "dry_run": True},
    ).json() == {"ok": True, "ran": True, "dry_run": True}
    assert apply_world.submissions == [("u1", "app-job1", True)]
    # No claim of any kind: a dry run writes no status, so there is nothing for
    # a repeat to corrupt and nothing to take a lease on.
    assert doc.updates == []

    for blocked in ("submitting", "submitted", "queued"):
        apply_world.install(_app_doc(blocked))
        resp = client.post(
            "/tasks/apply",
            json={"user_id": "u1", "app_id": "app-job1", "dry_run": True},
        )
        assert resp.json() == {"ok": True, "ran": False, "dry_run": True}, blocked
    assert len(apply_world.submissions) == 1


def test_apply_dry_run_is_a_no_op_for_a_missing_application(client, apply_world):
    apply_world.install(_FakeDoc(None))
    resp = client.post(
        "/tasks/apply", json={"user_id": "u1", "app_id": "app-job1", "dry_run": True}
    )
    # ``dry_run`` is reported on every return out of the rehearsal branch, or a
    # log search for rehearsals silently misses the ones that found nothing.
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ran": False, "dry_run": True}
    assert apply_world.submissions == []


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

    # ``during`` runs where the browser would be — after the claim, before any
    # outcome is written — so a test can decide what the rest of the world does
    # while this run holds the document.
    hooks = SimpleNamespace(
        posting="ok", result={"success": True, "dry_run": True}, during=None
    )
    submits: list[dict] = []

    async def check_posting(job):
        return hooks.posting() if callable(hooks.posting) else hooks.posting

    async def submit_application(job, prof, resume, **kw):
        submits.append(kw)
        if hooks.during is not None:
            interruption = hooks.during()
            if inspect.isawaitable(interruption):
                await interruption
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


# --------------------------------------------------------------------------
# Phase 2 PR D: the point-of-no-return marker.
#
# Every _emit in the Greenhouse submitter used the same "submitting" token, so
# the Submit click — the one step that cannot be undone — was indistinguishable
# from "Attaching resume". SUBMIT_CLICKED separates it, and the caller turns it
# into the single fact the reaper reads before deciding whether a dead
# submission may be retried.
# --------------------------------------------------------------------------


def _clicks(submission_world, message="Submitting application"):
    """Make the fake submitter report the click, the way Greenhouse now does."""

    async def submit_application(job, prof, resume, **kw):
        submission_world.submits.append(kw)
        if kw.get("on_progress"):
            kw["on_progress"](message, SUBMIT_CLICKED)
        if submission_world.hooks.during is not None:
            submission_world.hooks.during()
        return submission_world.hooks.result

    return submit_application


def test_the_click_marker_lands_in_the_same_write_as_its_timeline_entry(
    submission_world, monkeypatch
):
    """The marker is what stands between a dead submission and a duplicate real
    application, so it must not be possible for the timeline to say the form was
    submitted while the marker is missing. One write, both fields.

    And the entry itself is recorded as ``submitting``, never as the token:
    ``web/`` renders a closed union of statuses and ``review/page.tsx`` filters
    the submission timeline on ``["submitting", "submitted", "failed"]``, so a
    new token reaching Firestore would simply stop rendering."""
    doc = submission_world.doc
    doc._data["status"] = "submitting"
    submission_world.hooks.result = {"success": True}
    monkeypatch.setattr(applications, "submit_application", _clicks(submission_world))
    before = len(doc.updates)

    asyncio.run(applications.run_submission("u1", "app-job1"))

    marker_writes = [
        fields
        for fields, _option in doc.updates[before:]
        if "submit_attempted_at" in fields
    ]
    assert len(marker_writes) == 1, "the marker must be written exactly once"
    assert state.TIMELINE_FIELD in marker_writes[0]  # ...in that same write
    assert doc.data["submit_attempted_at"]

    clicked = [
        e for e in doc.data["timeline"] if e.get("note") == "Submitting application"
    ]
    assert [e["status"] for e in clicked] == ["submitting"]
    assert SUBMIT_CLICKED not in [e["status"] for e in doc.data["timeline"]]


def test_a_rehearsal_never_writes_the_click_marker(submission_world, monkeypatch):
    """A dry run stops before the button, so ``submit_greenhouse`` cannot reach
    the emit at all — but the guard is on the *caller* too, so no future
    submitter can talk a $0 rehearsal into claiming a browser clicked Submit.
    A rehearsal that left the marker behind would make the reaper permanently
    refuse to retry an application nobody ever sent."""
    doc = submission_world.doc
    monkeypatch.setattr(applications, "submit_application", _clicks(submission_world))

    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    assert "submit_attempted_at" not in doc.data
    assert doc.data["status"] == "ready_for_review"
    # Still labelled and still parked where the document actually is.
    assert doc.data["timeline"][-1]["note"].startswith(applications.DRY_RUN_NOTE)


def test_a_worker_killed_after_the_click_is_reaped_as_uncertain(
    submission_world, monkeypatch
):
    """**The two halves of this PR, joined.**

    ``CancelledError`` is what a Cloud Run eviction looks like from inside the
    coroutine: it is not an ``Exception``, so ``run_submission``'s ``except``
    never runs and the document is left in ``submitting`` exactly as a killed
    worker leaves it — with the marker already on it, because the emit happens
    before the click.

    The reaper then finds an expired lease and a marker, and the only correct
    answer is: fail it, flag it, tell the user to check their email, and never
    re-enqueue it.
    """
    doc = submission_world.doc
    doc._data["status"] = "submitting"

    def killed():
        raise asyncio.CancelledError()

    submission_world.hooks.during = killed
    monkeypatch.setattr(applications, "submit_application", _clicks(submission_world))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(applications.run_submission("u1", "app-job1"))

    # A killed worker leaves exactly this: still submitting, still leased, and
    # the marker recording that a browser got as far as the button.
    assert doc.data["status"] == "submitting"
    assert doc.data["submit_attempted_at"]
    assert doc.data["lease"]["status"] == "submitting"

    dispatched: list[tuple] = []
    later = datetime.now(UTC) + timedelta(seconds=state.IN_PROGRESS["submitting"] + 1)
    outcome = reaper.reap_one(
        doc,
        doc.get(),
        doc.data,
        reaper.classify(doc.data, now=later),
        user_id="u1",
        dispatch=lambda u, j: dispatched.append((u, j)) or True,
        now=later,
    )

    assert outcome == "release_uncertain"
    assert dispatched == []  # never, at any attempt count
    assert doc.data["status"] == "failed"
    assert doc.data[reaper.UNCERTAIN_FIELD] is True
    assert "UNKNOWN" in doc.data["timeline"][-1]["note"]


SUBMITTERS_DIR = REPO_ROOT / "tools" / "submitters"


def _end_line(node: ast.AST) -> int:
    """``node.end_lineno``, which is Optional in the AST but never absent for
    anything parsed from a file."""
    return getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)


def _submitter_sources() -> dict[str, str]:
    """Every module under ``tools/submitters`` that drives a form, from disk.

    **Enumerated, never listed.** Lever and Ashby submitters are planned work
    (the auto-apply-failures plan), and the scan below has to cover a new
    submitter the day it lands rather than the day someone remembers this test
    exists — a submitter that emits a plain ``"submitting"`` for its click is
    invisible to ``run_submission``'s progress callback, so the reaper never
    sees ``submit_attempted_at`` and writes "nothing was submitted, so it is
    safe to submit again" about an application that really was filed.

    A module qualifies by containing a ``.click(``, which is exactly the
    capability that makes the token load-bearing, and is what separates a
    submitter from ``router.py``, ``storage.py`` and the package ``__init__``.
    """
    sources = {}
    for path in sorted(SUBMITTERS_DIR.glob("*.py")):
        source = path.read_text()
        if ".click(" in source:
            sources[path.name] = source
    return sources


def test_the_submitter_scan_reaches_everything_the_router_dispatches_to():
    """The scan's blind spot would be a submitter that files an application
    without a Playwright click — an HTTP POST straight at an ATS API, say. It
    would still need the token, and ``.click(`` would not find it. So cross-
    check the enumeration against what ``router.py`` actually dispatches to:
    a module the router imports but the scan cannot see fails here, loudly,
    instead of passing the test below vacuously."""
    found = set(_submitter_sources())
    assert "greenhouse.py" in found  # the scan is not vacuous
    router = ast.parse((SUBMITTERS_DIR / "router.py").read_text())
    dispatched = {
        f"{node.module}.py"
        for node in ast.walk(router)
        if isinstance(node, ast.ImportFrom) and node.level and node.module
    }
    assert dispatched <= found, (
        f"{sorted(dispatched - found)} is dispatched to but not scanned — if it "
        "can file an application it must emit SUBMIT_CLICKED before doing so"
    )


@pytest.mark.parametrize("module", sorted(_submitter_sources()))
def test_only_the_click_emits_its_own_token(module):
    """The token has to name *one* step. Every other label a submitter emits is
    display chatter, and if a second one carried SUBMIT_CLICKED the marker would
    start meaning "we got somewhere near the form"."""
    source = _submitter_sources()[module]
    tree = ast.parse(source)
    emits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_emit"
    ]
    # ast.unparse because the last emit's token is a conditional expression
    # ("submitted" if confirmed else "submitting"), not a literal.
    tokens = [ast.unparse(node.args[2]) for node in emits]
    assert tokens.count("SUBMIT_CLICKED") == 1, (
        f"{module} must name its point of no return exactly once — a click that "
        'emits a plain "submitting" is indistinguishable from "Attaching '
        'resume", and the reaper reads that difference'
    )
    assert "SUBMIT_CLICKED" not in " ".join(t for t in tokens if t != "SUBMIT_CLICKED")

    marker = next(n for n in emits if ast.unparse(n.args[2]) == "SUBMIT_CLICKED")
    marker_end = _end_line(marker)
    # The first thing awaited after that emit is the click itself. The marker
    # means "a browser clicked Submit", so anything awaited in between would be
    # a way to write it and then never click.
    after = "\n".join(source.splitlines()[marker_end:])
    first_await = after.split("await ", 1)[1].splitlines()[0].strip()
    assert first_await.endswith(".click()"), first_await
    # ...and it is the only click left in that function. This replaces the old
    # "exactly six steps" tripwire, which only greenhouse could have: after the
    # point of no return a submitter presses one button, and a second click
    # down there is a second application nobody counted.
    enclosing = min(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.lineno <= marker.lineno <= _end_line(node)
        ),
        key=lambda node: _end_line(node) - node.lineno,
    )
    clicks = [
        node
        for node in ast.walk(enclosing)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "click"
        and node.lineno > marker_end
    ]
    assert len(clicks) == 1, [ast.unparse(c) for c in clicks]


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


# --------------------------------------------------------------------------
# The delivery claim, where it lives now.
#
# It used to be taken by ``/tasks/apply``. That fenced worker against worker
# and nothing else: ``dispatch_apply`` still runs the same function as a
# background task wherever QUEUE_MODE is off, and a Cloud Run traffic migration
# puts two hermes-api revisions on the same documents for the length of a
# rollout. These drive the claim through ``run_submission`` itself, which is
# what every one of those paths now goes through.
# --------------------------------------------------------------------------


def _submitting(world) -> _FakeDoc:
    """The document as ``POST /submit`` leaves it: claimed, unleased."""
    world.doc._data[state.STATUS_FIELD] = "submitting"
    return world.doc


def test_the_lease_is_held_for_the_length_of_the_run(submission_world):
    """The claim fences the work, so it has to be taken before the browser
    opens and held until an outcome is written — not stamped on afterwards."""
    doc = _submitting(submission_world)
    submission_world.hooks.result = {"success": True}
    held: list[dict] = []
    submission_world.hooks.during = lambda: held.append(doc.data["lease"])

    assert asyncio.run(applications.run_submission("u1", "app-job1")) is True

    assert held[0]["status"] == "submitting" and held[0]["owner"]
    assert doc.data["status"] == "submitted"
    # Handed back in the same write that recorded the outcome.
    assert doc.data is not None and "lease" not in doc.data


def test_two_runners_cannot_submit_the_same_application(submission_world, monkeypatch):
    """**The one that matters, and the reason the claim moved.**

    Two runners for one ``submitting`` document — a redelivered task, or the
    API's own in-process path racing a worker during a revision rollout — must
    not both put a real application into an employer's ATS. The status cannot
    refuse the second: the document is *already* ``submitting``, because that is
    how the API claimed it. The live lease is what says no, and it only says no
    to both if both paths take it.

    The first run here is the background-task path (``run_submission`` called
    directly, as ``dispatch_apply`` does without a queue); the second arrives
    through the worker handler while the first still holds the browser open.
    """
    monkeypatch.setenv("WORKER_MODE", "1")
    doc = _submitting(submission_world)
    submission_world.hooks.result = {"success": True}
    running, release = asyncio.Event(), asyncio.Event()

    async def still_driving_the_browser():
        running.set()
        await release.wait()

    submission_world.hooks.during = still_driving_the_browser

    async def scenario():
        first = asyncio.create_task(applications.run_submission("u1", "app-job1"))
        await running.wait()
        # wait_for, so an unfenced second runner *fails* here rather than
        # hanging: it would walk into the same browser hook the first run is
        # still parked in, and wait for a release only it can give.
        redelivered = await asyncio.wait_for(
            worker.task_apply(worker.ApplyTask(user_id="u1", app_id="app-job1")),
            timeout=5,
        )
        release.set()
        return await first, redelivered

    ran, redelivered = asyncio.run(scenario())

    assert ran is True
    assert redelivered == {"ok": True, "ran": False}
    assert len(submission_world.submits) == 1  # one browser, one application
    assert doc.data["status"] == "submitted"


def test_a_delivery_after_the_run_finished_is_refused_by_the_status(
    submission_world, monkeypatch
):
    """The terminal write releases the lease, so a task arriving *after* the run
    finished can't be let through by the lease being gone — the status has left
    ``submitting``, and that is the other half of the claim."""
    monkeypatch.setenv("WORKER_MODE", "1")
    doc = _submitting(submission_world)
    submission_world.hooks.result = {"success": True}

    assert asyncio.run(applications.run_submission("u1", "app-job1")) is True
    assert doc.data is not None and "lease" not in doc.data

    late = asyncio.run(
        worker.task_apply(worker.ApplyTask(user_id="u1", app_id="app-job1"))
    )

    assert late == {"ok": True, "ran": False}
    assert len(submission_world.submits) == 1


def test_a_lease_left_by_a_lost_terminal_write_is_handed_back(submission_world):
    """Every terminal transition carries CLEAR_LEASE, so the outcome and the
    release are one write — except when that write *loses*, which is exactly
    when it leaves our lease behind on someone else's status."""
    doc = _submitting(submission_world)
    submission_world.hooks.result = {"success": False, "error": "the ATS said no"}

    def unwedged_while_we_ran():
        # cli/unwedge_submitting got there first; the run's own terminal write
        # is then refused, and the lease it would have cleared survives it.
        state.try_transition(doc, doc.get(), "failed", note="unwedged")
        assert doc.data["lease"]  # still ours at this point

    submission_world.hooks.during = unwedged_while_we_ran

    asyncio.run(applications.run_submission("u1", "app-job1"))

    assert doc.data["status"] == "failed"
    # Released, so the next claim isn't blocked by a run that has ended.
    assert doc.data is not None and "lease" not in doc.data


def test_a_failure_right_after_the_claim_still_hands_the_lease_back(submission_world):
    """Everything between taking the claim and the ``finally`` that returns it is
    a region where an exception strands the lease on the document for its full
    TTL — with the user's application wedged in ``submitting`` behind it. So the
    ``try`` opens on the statement after the claim, which means the post-claim
    re-read is inside it, not in front of it."""
    doc = _submitting(submission_world)
    real_get, tripped = doc.get, []

    def blink():
        # The first read taken while a lease is held is the post-claim re-read.
        if not tripped and state.lease_is_held(doc.data or {}):
            tripped.append(True)
            raise RuntimeError("Firestore blinked right after the claim landed")
        return real_get()

    doc.get = blink

    asyncio.run(applications.run_submission("u1", "app-job1"))

    assert tripped, "the re-read never happened — this test pins nothing"
    assert doc.data["status"] == "failed"
    assert doc.data is not None and "lease" not in doc.data
    assert "Firestore blinked" in doc.data["timeline"][-1]["note"]
    assert submission_world.submits == []  # and no browser was opened


def test_a_run_that_records_no_outcome_keeps_its_lease(submission_world):
    """The opposite case, and the dangerous one: the run ended with the document
    still ``submitting``, so whether the form went in is unknown. Releasing here
    would invite a redelivery straight back into it — the lease is left to
    expire, which is what unwedge_submitting exists to adjudicate."""
    doc = _submitting(submission_world)

    async def the_worker_is_evicted_mid_submit():
        raise asyncio.CancelledError

    submission_world.hooks.during = the_worker_is_evicted_mid_submit

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(applications.run_submission("u1", "app-job1"))

    assert doc.data["status"] == "submitting"
    assert doc.data["lease"]["status"] == "submitting"
    assert state.lease_is_held(doc.data)


def test_a_rehearsal_takes_no_lease(submission_world):
    """A dry run makes no claim — it writes no status of its own, so there is
    nothing a repeat could corrupt — and it must not take one either: a lease on
    a ``ready_for_review`` document is still live when the user clicks Submit,
    and the real run would then find the document held by nobody."""
    asyncio.run(applications.run_submission("u1", "app-job1", dry_run=True))

    assert "lease" not in submission_world.doc.data
    assert submission_world.doc.data["status"] == "ready_for_review"


def test_a_submission_no_one_claimed_never_opens_a_browser(submission_world):
    """The claim is a precondition on the *status*, not just on the lease: a
    document that never reached ``submitting`` has no claim to inherit."""
    submission_world.doc._data["lease"] = state.lease_for(
        "submitting", owner="someone-else"
    )
    _submitting(submission_world)

    assert asyncio.run(applications.run_submission("u1", "app-job1")) is False
    assert submission_world.submits == []
    assert submission_world.doc.data["lease"]["owner"] == "someone-else"


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


def _literal_enqueue_calls(source: str) -> set[tuple[str, str]]:
    """Every ``enqueue(queue, path, ...)`` whose queue and path are both string
    literals. Reads the AST, so it is not fooled by f-strings."""
    calls = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name != "enqueue":
            continue
        queue, route = node.args[0], node.args[1]
        if isinstance(queue, ast.Constant) and isinstance(route, ast.Constant):
            calls.add((queue.value, route.value))
    return calls


def test_the_funnel_routes_have_callers_and_they_agree():
    """The replacement for PR B's deploy-ordering guard.

    That guard asserted **nothing in the repo enqueued** to ``/tasks/tailor`` or
    ``/tasks/apply``: CI deploys hermes-api and hermes-worker from the same
    merge, so the handlers had to land one merge ahead of their callers or the
    API could enqueue to a route that 404s. This is the merge that adds the
    callers, so the guard is replaced by its opposite — the two names each
    dispatch helper hard-codes have to be a queue that is provisioned and a
    route the worker actually serves. A typo in either is a task that vanishes.

    Scanned across the whole repo rather than just ``applications.py``, and
    keyed by file: the funnel has exactly two entry points and they live in one
    module, so a third appearing anywhere else is something to look at, not
    something to add to a list.
    """
    served = {route.path for route in worker.router.routes}
    funnel_queues, funnel_paths = {"tailor", "apply"}, {"/tasks/tailor", "/tasks/apply"}
    callers: dict[str, set[tuple[str, str]]] = {}
    for path in REPO_ROOT.rglob("*.py"):
        if set(path.parts) & {".venv", "tests", "node_modules", "locust_env"}:
            continue
        touches = {
            (queue, route)
            for queue, route in _literal_enqueue_calls(path.read_text())
            if queue in funnel_queues or route in funnel_paths
        }
        if touches:
            callers[str(path.relative_to(REPO_ROOT))] = touches

    assert callers == {
        "api/routes/applications.py": {
            ("tailor", "/tasks/tailor"),
            ("apply", "/tasks/apply"),
        }
    }
    for queue, route in callers["api/routes/applications.py"]:
        assert queue in KNOWN_QUEUES, queue
        assert route in served, route


# --------------------------------------------------------------------------
# reap_user: the seam between the cron tick and the reaper.
#
# ``cron_world`` monkeypatches this function wholesale, so nothing above
# executes its body. What it binds — which dispatcher, and whether the pass is
# allowed to act — is the load-bearing fact behind the apply fork.
# --------------------------------------------------------------------------


@pytest.fixture
def reap_seam(monkeypatch):
    """``reap_user`` with ``reap_applications`` replaced by a recorder, so the
    call it actually makes is directly observable."""
    calls: list[dict] = []

    def record(user_id, **kwargs):
        calls.append({"user_id": user_id, **kwargs})
        return {"recovered": 0, "truncated": 0}

    monkeypatch.setattr(discovery.reaper, "reap_applications", record)
    return calls


def test_reap_user_lets_the_pass_act(reap_seam):
    """``execute`` defaults to True and nothing here may quietly flip it.

    A pass pinned to ``execute=False`` still returns a tally and still reports
    ``recovered: 0`` — indistinguishable from "nothing to do" — so the reaper
    would silently never recover anything, for anyone, forever.
    """
    asyncio.run(discovery.reap_user("u1", background_tasks=BackgroundTasks()))

    assert len(reap_seam) == 1
    assert reap_seam[0]["user_id"] == "u1"
    # Not passed at all, or passed as True — either is fine; False is not.
    assert reap_seam[0].get("execute", True) is True


def test_the_pass_acts_by_default():
    """The other half of the test above, which leans on the callee's default and
    cannot check it there — that fixture has replaced the callee."""
    assert (
        inspect.signature(reaper.reap_applications).parameters["execute"].default
        is True
    )


def test_reap_user_binds_the_tailor_dispatcher_and_only_that_one(
    reap_seam, monkeypatch
):
    """**Which dispatcher is bound is the whole apply fork.**

    The reaper re-dispatches *tailoring* — cheap, claimed before it spends, safe
    to repeat. It must never be able to reach ``dispatch_apply``: that drives a
    live browser at a real employer, and re-running one is the duplicate
    application this PR exists to prevent.
    """
    tailored: list[tuple] = []
    monkeypatch.setattr(
        discovery,
        "dispatch_tailor",
        lambda uid, job_id, *, background_tasks: (
            tailored.append((uid, job_id, background_tasks)) or True
        ),
    )

    def never(*args, **kwargs):
        pytest.fail("the reaper reached dispatch_apply")

    monkeypatch.setattr(applications, "dispatch_apply", never)

    background = BackgroundTasks()
    asyncio.run(discovery.reap_user("u1", background_tasks=background))

    dispatch = reap_seam[0]["dispatch"]
    assert dispatch("u1", "job1") is True

    # The request's own BackgroundTasks is forwarded, which is what keeps the
    # in-process path working with QUEUE_MODE off.
    assert tailored == [("u1", "job1", background)]


def test_reap_user_reports_the_tally_the_tick_reads(reap_seam, monkeypatch):
    """The tick sums ``recovered`` and ``truncated`` off this return value."""
    monkeypatch.setattr(
        discovery.reaper,
        "reap_applications",
        lambda user_id, **kw: {"recovered": 2, "truncated": 1},
    )

    result = asyncio.run(discovery.reap_user("u1", background_tasks=BackgroundTasks()))

    assert result["recovered"] == 2 and result["truncated"] == 1


def test_a_successful_publish_clears_the_reapers_recovery_budget(client, monkeypatch):
    """Driven through the real ``run_tailoring``, because the epoch is only
    worth anything if the *publish* carries it.

    ``reaper.MAX_ATTEMPTS`` bounds consecutive failed recoveries. Without a
    reset the count is a lifetime total, so an application recovered three times
    during a queue outage and then tailored perfectly stays permanently one
    stale tick from ``give_up`` — a give_up that dispatches nothing while its
    note tells the user to press Regenerate.
    """
    monkeypatch.setenv("WORKER_MODE", "1")
    doc = _app_doc("queued", **{reaper.ATTEMPTS_FIELD: reaper.MAX_ATTEMPTS})
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
        return SimpleNamespace(
            model_dump=lambda **kw: {"objective_text": "hi"},
            resume_variant_uri="gs://b/r.docx",
        )

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        pass

    monkeypatch.setattr(applications, "check_posting", fake_check_posting)
    monkeypatch.setattr(applications, "tailor_application", fake_tailor_application)
    monkeypatch.setattr(applications, "persist_run_cost", fake_persist_run_cost)

    client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "job1"})

    assert doc.data["status"] == "ready_for_review"
    assert doc.data is not None and reaper.ATTEMPTS_FIELD not in doc.data
    assert reaper.attempts(doc.data) == 0
    # In the same write as the status, not beside it: the content write next to
    # it carries no precondition, so a reset there could land on a document a
    # regenerate had already moved on.
    publish = next(f for f, _o in doc.updates if f.get("status") == "ready_for_review")
    assert publish[reaper.ATTEMPTS_FIELD] is firestore.DELETE_FIELD


# --------------------------------------------------------------------------
# ``run_tailoring``'s two terminal writes name the status they own.
#
# Both used to go out bare. That is survivable while a tailoring run is the
# only thing that ever touches its own document — but the reaper *deliberately*
# manufactures overlapping runs: it requeues a document whose lease lapsed, and
# the next dispatch produces a run B on the same document while the evicted
# run A may still be alive and about to raise.
# --------------------------------------------------------------------------


def _zombie_world(monkeypatch, doc, on_tailor):
    """``run_tailoring`` for real, with ``on_tailor`` standing in for the paid
    call — the window in which another process can take the document over."""
    monkeypatch.setenv("WORKER_MODE", "1")
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

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        pass

    monkeypatch.setattr(applications, "check_posting", fake_check_posting)
    monkeypatch.setattr(applications, "tailor_application", on_tailor)
    monkeypatch.setattr(applications, "persist_run_cost", fake_persist_run_cost)


@pytest.mark.parametrize("moved_to", ["queued", "submitting"])
def test_a_zombie_tailoring_run_cannot_fail_a_document_it_no_longer_owns(
    client, monkeypatch, moved_to
):
    """``queued → failed`` and ``submitting → failed`` are both legal edges, so
    only ``allowed_from={"tailoring"}`` refuses them.

    Without it the run that died gets the last word: on ``queued`` it fails the
    document the reaper just recovered, and on ``submitting`` it fails — and
    unleases — an application a browser is at that moment submitting for real,
    destroying the confirmation evidence for it.
    """
    doc = _app_doc("queued")
    handovers: list[str] = []

    async def evicted_mid_run(job, profile, upload=True):
        # The reaper found run A's lease expired and put the work back.
        assert state.try_transition(
            doc,
            doc.get(),
            "queued",
            allowed_from={"tailoring"},
            lease=state.lease_for("queued", owner="reaper"),
        )
        if moved_to == "submitting":
            # ...run B claimed it, tailored it, and the user clicked Submit.
            for to, lease in (
                ("tailoring", state.lease_for("tailoring", owner="B")),
                ("ready_for_review", state.CLEAR_LEASE),
                ("submitting", state.lease_for("submitting", owner="submitter")),
            ):
                assert state.try_transition(doc, doc.get(), to, lease=lease)
        handovers.append(doc.data["status"])
        raise RuntimeError("worker A was evicted and came back to die")

    _zombie_world(monkeypatch, doc, evicted_mid_run)

    client.post("/tasks/tailor", json={"user_id": "u1", "job_id": "job1"})

    assert handovers == [moved_to]  # the handover really happened
    assert doc.data["status"] == moved_to
    assert [e["status"] for e in doc.data["timeline"]][-1] != "failed"
    # And the live owner's claim is still on the document: the failure write
    # carries CLEAR_LEASE, so a write that lands is a lease that is gone.
    owner = "reaper" if moved_to == "queued" else "submitter"
    assert state.lease_owner(doc.data) == owner


def test_run_tailorings_terminal_writes_name_the_status_they_own():
    """The publish is the one this cannot be shown by outcome.

    ``tailoring`` is today the only row in ``state.TRANSITIONS`` with an edge to
    ``ready_for_review``, so ``allowed_from={"tailoring"}`` on the publish is
    currently the table's own precondition restated — it changes no behaviour.
    It is there because ``tools.applications.reaper``'s docstring used to cite
    that bare write as the reason ``submitting → ready_for_review`` could never
    be added to the table. Stating the precondition at the call site is what
    makes that decision the table's to make.

    The claim (``→ tailoring``) is deliberately not in the list: it is the write
    that *establishes* ownership rather than one that acts on it, and there is
    no earlier owner for it to trample.
    """
    source = (REPO_ROOT / "api" / "routes" / "applications.py").read_text()
    run_tailoring = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_tailoring"
    )
    writes = {
        ast.literal_eval(node.args[1]): node
        for node in ast.walk(run_tailoring)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_transition"
    }
    assert set(writes) == {"tailoring", "ready_for_review", "failed"}, (
        "a new status write in run_tailoring — which status does it own?"
    )
    for status in ("ready_for_review", "failed"):
        call = writes[status]
        assert "allowed_from" in {kw.arg for kw in call.keywords}, ast.unparse(call)


# --------------------------------------------------------------------------
# Phase 2 PR E: the schedule-slot lease.
#
# ``tick_user`` used to write ``last_*_at`` = now and *then* dispatch, so a run
# that died looked exactly like one that succeeded and the user waited out a
# whole ``discovery_interval_hours`` before anything retried. The slot claim
# moved to the cycle's own success write; a lease covers the gap in between.
# --------------------------------------------------------------------------


def _merge_into(target: dict, data: dict) -> None:
    """``set(merge=True)`` semantics: nested maps merge, DELETE_FIELD removes."""
    for key, value in data.items():
        if value is firestore.DELETE_FIELD:
            target.pop(key, None)
        elif isinstance(value, dict):
            nested = target.get(key)
            if not isinstance(nested, dict):
                nested = target[key] = {}
            _merge_into(nested, value)
        else:
            target[key] = value


class _SlotSnap:
    """A snapshot that is a real copy, so a read taken before a concurrent write
    stays stale — the fake would otherwise hand the reader a live view of the
    nested map and the precondition would never be the thing that refuses."""

    def __init__(self, doc_id, data, update_time):
        self.id = doc_id
        self.update_time = update_time
        self.exists = data is not None
        self._data = None if data is None else copy.deepcopy(data)

    def to_dict(self):
        return None if self._data is None else copy.deepcopy(self._data)


class _SlotDoc:
    """A ``users/{uid}`` document honouring the three Firestore behaviours the
    slot lease is built on: ``update`` takes a ``last_update_time``
    precondition; ``update`` addresses nested fields by dotted path rather than
    replacing the whole map (replacing it is what would silently wipe
    ``last_discovery`` and the *other* loop's slot); and ``set(merge=True)``
    resolves ``DELETE_FIELD`` inside a nested map."""

    def __init__(self, data: dict | None = None):
        self.id = "u1"
        self._data = None if data is None else copy.deepcopy(data)
        self._version = 1
        self.updates: list[tuple[dict, object]] = []

    @property
    def data(self) -> dict | None:
        return None if self._data is None else copy.deepcopy(self._data)

    @property
    def state(self) -> dict:
        return (self.data or {}).get("discovery_state") or {}

    def get(self):
        return _SlotSnap(self.id, self._data, self._version)

    def set(self, data, merge=False):
        if not merge or self._data is None:
            self._data = {}
        _merge_into(self._data, copy.deepcopy(data))
        self._version += 1

    def update(self, fields, option=None):
        self.updates.append((fields, option))
        if self._data is None:
            raise NotFound("no such document")
        if option is not None and option._last_update_time != self._version:
            raise FailedPrecondition("stale last_update_time")
        for path, value in fields.items():
            parts = path.split(".")
            target = self._data
            for part in parts[:-1]:
                nested = target.get(part)
                if not isinstance(nested, dict):
                    nested = target[part] = {}
                target = nested
            if value is firestore.DELETE_FIELD:
                target.pop(parts[-1], None)
            else:
                target[parts[-1]] = copy.deepcopy(value)
        self._version += 1


T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
LEASE = timedelta(seconds=discovery._LEASE_SECONDS)


@pytest.fixture
def slot_world(monkeypatch):
    """One opted-in user plus a recorded ``dispatch_cycle``, so "did this tick
    put a cycle on the queue?" is observable and no cycle actually runs."""
    doc = _SlotDoc({"discovery_settings": {"auto_discovery": True}})
    dispatched: list[tuple] = []

    async def fake_dispatch(kind, user_id, *, trigger):
        dispatched.append((kind, user_id, trigger))
        return True

    monkeypatch.setattr(discovery, "_user_ref", lambda uid: doc)
    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch)
    monkeypatch.setattr(discovery, "_last_tick_check", {})

    def freeze(at: datetime):
        monkeypatch.setattr(discovery, "_now", lambda: at)

    def tick(at: datetime, *, doc=None):
        freeze(at)
        asyncio.run(discovery.tick_user("u1", force_check=True, doc=doc))

    def seed(**state):
        doc.set({"discovery_state": state}, merge=True)

    return SimpleNamespace(
        doc=doc, dispatched=dispatched, tick=tick, freeze=freeze, seed=seed
    )


@pytest.fixture
def cycle_world(monkeypatch, slot_world):
    """``run_discovery_cycle`` for real over ``slot_world``'s document, with the
    pipeline and the ledger stubbed out. ``hooks.during`` decides how the run
    ends — returning normally, raising, or being killed."""
    hooks = SimpleNamespace(during=None)

    async def fake_run_discovery(user_id):
        if hooks.during is not None:
            hooks.during()
        return {"jobs": [], "jobs_by_platform": {}, "failures": [], "empty_boards": []}

    async def fake_load_prefs(user_id):
        return None

    async def fake_persist_new_jobs(jobs):
        return 0

    async def fake_score(user_id):
        return {"scored": 0, "discarded": 0, "failed": 0, "pending": 0}

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        pass

    monkeypatch.setattr(discovery, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(discovery, "load_job_preferences", fake_load_prefs)
    monkeypatch.setattr(discovery, "prefilter_jobs", lambda jobs, prefs: (jobs, {}))
    monkeypatch.setattr(discovery, "persist_new_jobs", fake_persist_new_jobs)
    monkeypatch.setattr(discovery, "score_pending_jobs", fake_score)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    monkeypatch.delenv("QUEUE_MODE", raising=False)

    def run(at: datetime, *, trigger="cron"):
        slot_world.freeze(at)
        asyncio.run(discovery.run_discovery_cycle("u1", trigger=trigger))

    return SimpleNamespace(hooks=hooks, doc=slot_world.doc, run=run)


def _explode(message="the boards all died"):
    def boom():
        raise RuntimeError(message)

    return boom


def test_the_slot_lease_outlives_the_run_it_guards():
    """**The inequality is the lock**, and this project has already shipped it
    backwards once (PR B: a 1200s lease over 1800s of work).

    A lease shorter than the work it covers is not a weaker lock, it is *no*
    lock — it is guaranteed to have lapsed before the run could possibly have
    finished, so every tick in between reads it as permission. Derived from the
    dispatch deadline rather than restated, so the two cannot drift apart.
    """
    assert discovery._LEASE_GRACE_SECONDS > 0
    assert (
        discovery._LEASE_SECONDS
        == queues._DISPATCH_DEADLINE_SECONDS + discovery._LEASE_GRACE_SECONDS
    )
    # The behavioural form of the same statement: a cycle that burns its entire
    # dispatch deadline and is then killed by Cloud Run is still holding the
    # slot at the moment it dies. A subtracted grace fails right here.
    killed_at = T0 + timedelta(seconds=queues._DISPATCH_DEADLINE_SECONDS)
    assert discovery._lease_held(discovery._lease(T0), killed_at)


def test_a_tick_leases_the_slot_instead_of_claiming_it(slot_world):
    """The bug, stated as the fix: the tick writes a lease and dispatches, and
    ``last_discovery_at`` — the field ``_due`` actually compares against — stays
    exactly where the last *successful* run left it."""
    slot_world.seed(last_discovery_at="2026-08-01T00:00:00+00:00")

    slot_world.tick(T0)

    assert slot_world.dispatched == [("discovery", "u1", "cron")]
    state = slot_world.doc.state
    assert state["last_discovery_at"] == "2026-08-01T00:00:00+00:00"
    assert state["discovery_lease"]["acquired_at"] == T0.isoformat()
    assert discovery._lease_held(state["discovery_lease"], T0)


def test_a_second_tick_will_not_dispatch_on_top_of_a_live_run(slot_world):
    """The other half of what the pre-claim used to buy, and the reason a lease
    has to exist at all: two triggers must not both dispatch a paid cycle."""
    slot_world.tick(T0)
    slot_world.tick(T0 + timedelta(minutes=20))

    assert slot_world.dispatched == [("discovery", "u1", "cron")]


def test_a_run_that_dies_silently_costs_a_lease_not_an_interval(slot_world):
    """**The bug this PR closes.**

    Nothing writes ``last_discovery_at`` for a cycle whose worker was evicted,
    so before this change the tick's own pre-claim was the only record and the
    user waited a full ``discovery_interval_hours`` — a day, by default — for a
    retry. Now the lease lapses and the next hourly tick picks it straight up.
    """
    slot_world.tick(T0)
    assert slot_world.dispatched == [("discovery", "u1", "cron")]

    # The cycle never returns: no success write, no failure write, nothing.
    slot_world.tick(T0 + LEASE + timedelta(seconds=1))

    assert slot_world.dispatched == [("discovery", "u1", "cron")] * 2
    # ...and well inside the interval that used to be the retry latency.
    assert LEASE < timedelta(hours=6)  # the shortest cadence the UI offers


def test_a_successful_cycle_claims_the_slot_and_hands_the_lease_back(cycle_world):
    """The success write *is* the slot claim. The lease rides out with it,
    because the timestamp landing beside it holds the slot for a whole
    interval — there is nothing left for a lease to protect."""
    cycle_world.doc.set(
        {"discovery_state": {"discovery_lease": discovery._lease(T0)}}, merge=True
    )

    cycle_world.run(T0 + timedelta(minutes=4))

    state = cycle_world.doc.state
    assert state["last_discovery_at"] == (T0 + timedelta(minutes=4)).isoformat()
    assert "discovery_lease" not in state
    assert state["last_discovery"]["trigger"] == "cron"


def test_a_cycle_that_fails_loudly_hands_its_slot_straight_back(
    cycle_world, slot_world
):
    """A run that raised is *over*, and it wrote no ``last_discovery_at`` — so
    its lease is the only thing keeping the next tick off the slot, and holding
    it buys nothing but silence on top of the failure. Dropping it turns a
    lease-TTL wait into a next-tick retry."""
    slot_world.tick(T0)
    assert slot_world.doc.state["discovery_lease"]

    cycle_world.hooks.during = _explode()
    cycle_world.run(T0 + timedelta(minutes=1))

    state = cycle_world.doc.state
    assert "discovery_lease" not in state, "a loud failure left its lease behind"
    assert "last_discovery_at" not in state  # the slot was never claimed

    # And the point of all of it: the next tick retries rather than waiting.
    slot_world.dispatched.clear()
    slot_world.tick(T0 + timedelta(minutes=2))
    assert slot_world.dispatched == [("discovery", "u1", "cron")]


def test_a_sweep_that_fails_loudly_hands_its_slot_back_too(slot_world, monkeypatch):
    """Same contract on the other loop, which has its own slot and its own
    lease field — a fix applied to one of a matched pair is half a fix."""
    slot_world.doc.set(
        {"discovery_settings": {"auto_discovery": False, "liveness_sweep": True}},
        merge=True,
    )

    async def dead_sweep(user_id):
        raise RuntimeError("every ATS timed out")

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        pass

    monkeypatch.setattr(discovery, "sweep_postings", dead_sweep)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)

    slot_world.tick(T0)
    assert slot_world.dispatched == [("sweep", "u1", "cron")]
    assert slot_world.doc.state["sweep_lease"]

    slot_world.freeze(T0 + timedelta(minutes=1))
    asyncio.run(discovery.run_sweep_cycle("u1", trigger="cron"))

    assert "sweep_lease" not in slot_world.doc.state
    assert "last_sweep_at" not in slot_world.doc.state


def test_a_cycle_killed_mid_run_leaves_its_lease_to_expire(cycle_world, slot_world):
    """The opposite case, and why the release is in the ``except`` and not the
    ``finally``.

    ``CancelledError`` is what a Cloud Run eviction looks like from inside the
    coroutine; it is not an ``Exception``, so it never reaches the handler. That
    run may still be going, and freeing its slot on the way out would invite a
    second cycle straight into it — so the lease is left to expire on the clock.
    A release moved into the ``finally`` fails right here.
    """
    slot_world.tick(T0)

    def evicted():
        raise asyncio.CancelledError

    cycle_world.hooks.during = evicted
    with pytest.raises(asyncio.CancelledError):
        cycle_world.run(T0 + timedelta(minutes=1))

    assert discovery._lease_held(cycle_world.doc.state["discovery_lease"], T0)
    slot_world.dispatched.clear()
    slot_world.tick(T0 + timedelta(minutes=2))
    assert slot_world.dispatched == []


def test_a_tick_cannot_take_a_slot_another_tick_claimed_between_read_and_write(
    slot_world,
):
    """**The phase's signature bug, in this file's shape.**

    ``tick_user`` reads ``discovery_state`` and writes it two statements later,
    and the two triggers that reach it — the hourly cron and the opportunistic
    tick from ``jobs.list_pending_jobs`` — really can interleave there. A read
    outside the write is not a compare-and-swap: both ticks see a free slot and
    both dispatch a paid cycle. The claim is conditioned on ``update_time``, so
    the loser re-reads, finds a live lease, and dispatches nothing.

    ``dispatch_cycle``'s named task ids do not cover this. They dedupe one
    ``(trigger, kind, user, hour)`` tuple, and these two ticks carry *different*
    triggers — as do two ticks either side of an hour boundary — and with
    ``QUEUE_MODE`` off there is no queue to hold a name at all.
    """
    doc, raced, intruder = slot_world.doc, [], []
    real_get = doc.get

    def racing_get():
        snap = real_get()  # our read lands first...
        if not raced:
            raced.append(None)
            # ...then the other tick's whole claim does, bumping update_time.
            intruder.append(discovery._claim_slot("u1", "discovery", 24, T0))
        return snap

    doc.get = racing_get

    assert discovery._claim_slot("u1", "discovery", 24, T0) is False
    assert intruder == [True], "the intruder never claimed — this pins nothing"
    assert doc.state["discovery_lease"]["acquired_at"] == T0.isoformat()


def test_a_tick_holding_a_stale_document_cannot_re_run_a_finished_cycle(slot_world):
    """The interval is re-checked inside the swap too, not just the lease.

    The cron fan-out hands ``tick_user`` a document it streamed moments ago. If
    the cycle that document was due for has since succeeded — writing a fresh
    ``last_discovery_at`` and releasing its lease — a claim that re-checked only
    the lease would sail through and buy a second cycle.
    """
    slot_world.seed(last_discovery_at=T0.isoformat())
    stale = {"discovery_settings": {"auto_discovery": True}, "discovery_state": {}}

    slot_world.tick(T0 + timedelta(minutes=1), doc=stale)

    assert slot_world.dispatched == []
    assert "discovery_lease" not in slot_world.doc.state


def test_a_failing_manual_run_never_frees_a_scheduled_runs_slot(
    cycle_world, slot_world
):
    """``POST /settings/discovery/run`` and the onboarding kickoff dispatch
    straight past ``tick_user`` and hold no lease, so they have nothing to hand
    back — and the lease they *would* find belongs to a scheduled cycle that
    may still be running."""
    slot_world.tick(T0)
    held = slot_world.doc.state["discovery_lease"]

    cycle_world.hooks.during = _explode()
    cycle_world.run(T0 + timedelta(minutes=1), trigger="manual")

    assert cycle_world.doc.state["discovery_lease"] == held


def test_every_trigger_tick_user_dispatches_under_can_release_a_slot(slot_world):
    """The other half of the test above: that gate is a set of trigger strings,
    so it is only correct while every trigger ``tick_user`` actually dispatches
    under is in it.

    Driven through ``tick_user`` rather than read off its source. Both entry
    points are exercised — the cron fan-out's ``force_check=True`` and the
    opportunistic tick from ``jobs.list_pending_jobs`` — and what is asserted is
    the trigger that reached ``dispatch_cycle``, which is the same string the
    cycle later hands to ``_release_slot``.
    """
    for force_check in (True, False):
        slot_world.seed(discovery_lease=firestore.DELETE_FIELD)
        discovery._last_tick_check.clear()
        slot_world.freeze(T0)
        asyncio.run(discovery.tick_user("u1", force_check=force_check))

    assert {trigger for _kind, _uid, trigger in slot_world.dispatched} == set(
        discovery.SLOT_TRIGGERS
    )
    # The triggers that reach a cycle without ever passing through tick_user.
    assert not discovery.SLOT_TRIGGERS & {"manual", "onboarding", "scheduled", "queued"}


def test_a_release_refuses_a_lease_taken_after_this_run_began(slot_world):
    """A run whose lease lapsed while it was still going finds a *successor's*
    lease on the document. Clearing that would put a second cycle on the same
    user with nothing left to stop a third; there is no owner token to carry
    across the queue boundary, so the acquisition time is what tells them
    apart."""
    successor = T0 + LEASE + timedelta(minutes=5)
    slot_world.seed(discovery_lease=discovery._lease(successor))

    assert discovery._release_slot("u1", "discovery", "cron", T0) is False
    assert (
        slot_world.doc.state["discovery_lease"]["acquired_at"] == successor.isoformat()
    )

    # Positive control: our own lease, acquired before we began, does go back.
    slot_world.seed(discovery_lease=discovery._lease(T0))
    assert discovery._release_slot("u1", "discovery", "cron", T0) is True
    assert "discovery_lease" not in slot_world.doc.state


def test_an_unreadable_lease_never_wedges_a_loop_for_good(slot_world):
    """The bias here is the opposite of ``state.lease_is_held``'s, on purpose.

    Nothing reaps a schedule slot: a lease read as held is never dispatched
    against, so nothing ever runs to clear it and this user's loops stop
    forever — "discovery never runs", the bug this module exists to prevent.
    There, refusing a claim only wedges one document while claiming anyway risks
    a duplicate real job application, so it errs the other way.
    """
    for junk in ({"expires_at": "not a date"}, {}, "a string", None):
        assert discovery._lease_held(junk, T0) is False

    slot_world.seed(discovery_lease={"expires_at": "whenever"})
    slot_world.tick(T0)

    assert slot_world.dispatched == [("discovery", "u1", "cron")]
    # ...and the corrupt value is overwritten on the way past.
    assert discovery._lease_held(slot_world.doc.state["discovery_lease"], T0)


def test_a_claim_only_ever_touches_its_own_slot(slot_world):
    """``update`` replaces a map value wholesale unless the field is addressed
    by dotted path, and ``discovery_state`` also carries the last run's metrics
    and the *other* loop's slot. A claim that took the map would wipe both."""
    slot_world.seed(
        last_sweep_at="2026-08-25T00:00:00+00:00", last_discovery={"scored": 7}
    )

    slot_world.tick(T0)

    state = slot_world.doc.state
    assert state["last_sweep_at"] == "2026-08-25T00:00:00+00:00"
    assert state["last_discovery"] == {"scored": 7}
    assert state["discovery_lease"]["acquired_at"] == T0.isoformat()


def test_the_next_run_display_does_not_go_backwards_during_a_run(slot_world):
    """``last_discovery_at`` moves only on success now, so a run in flight would
    otherwise leave the Profile card advertising a next run in the *past* —
    further into the past every hour it stayed in flight. Counting from the
    moment the slot was leased restores exactly what the old pre-claim showed.
    """
    slot_world.seed(last_discovery_at=(T0 - timedelta(days=3)).isoformat())
    slot_world.tick(T0)  # a cycle is now in flight
    assert slot_world.dispatched == [("discovery", "u1", "cron")]

    payload = discovery.get_discovery_settings(BackgroundTasks(), user_id="u1")

    assert datetime.fromisoformat(payload["next_discovery_at"]) == T0 + timedelta(
        hours=24
    )
    # Positive control: with no run in flight the card still counts from the
    # last success, so this is the lease being folded in and nothing else.
    assert (
        discovery._next_iso(
            (T0 - timedelta(days=3)).isoformat(), 24, lease=None, now=T0
        )
        == (T0 + timedelta(hours=-72 + 24)).isoformat()
    )


def test_the_next_run_display_says_due_again_once_a_failed_run_lets_go():
    """...and it has to degrade honestly at the other end: a failure releases
    the lease, and the card goes back to reporting a next run in the past —
    which is the truth, because the loop *is* due."""
    last = (T0 - timedelta(days=3)).isoformat()

    while_running = discovery._next_iso(last, 24, lease=discovery._lease(T0), now=T0)
    after_release = discovery._next_iso(last, 24, lease=None, now=T0)

    assert datetime.fromisoformat(while_running) > T0
    assert datetime.fromisoformat(after_release) < T0


# --------------------------------------------------------------------------
# PR E, review pass: the guards the first round left undefended.
# --------------------------------------------------------------------------


def test_a_release_cannot_free_a_lease_claimed_between_read_and_write(slot_world):
    """**The release side of the compare-and-swap**, and the one the first round
    of tests left open — the claim had this and the release did not.

    Run A claims at ``T`` and overruns its lease (in-process, where no dispatch
    deadline applies). It fails loudly at ``T+1900`` and reads the document: the
    lease is expired but still carries A's own ``acquired_at``, so the ownership
    check passes. In the microseconds that follow, a tick sees that expired
    lease, claims a fresh one, and dispatches run B. Without the precondition on
    A's write, A then deletes **B's live lease** and the next tick dispatches a
    third cycle — two concurrent paid discovery-and-scoring runs on one user.
    """
    slot_world.seed(discovery_lease=discovery._lease(T0))
    doc, raced, intruder = slot_world.doc, [], []
    real_get = doc.get
    late = T0 + LEASE + timedelta(seconds=40)

    def racing_get():
        snap = real_get()  # A reads: expired, and still stamped as A's
        if not raced:
            raced.append(None)
            # A tick collects the expired lease and dispatches run B.
            intruder.append(discovery._claim_slot("u1", "discovery", 24, late))
        return snap

    doc.get = racing_get

    assert discovery._release_slot("u1", "discovery", "cron", T0) is False
    assert intruder == [True], "the intruder never claimed — this pins nothing"
    # B's lease survived A's release, and is the one still standing.
    assert doc.state["discovery_lease"]["acquired_at"] == late.isoformat()
    assert discovery._lease_held(doc.state["discovery_lease"], late)


def test_a_release_that_cannot_reach_firestore_never_escapes(monkeypatch):
    """The "never raises" line in the docstring, made load-bearing.

    ``_release_slot`` is called from ``run_discovery_cycle``'s ``except``. A
    transient ``ServiceUnavailable`` on its read would propagate out of the
    cycle, out of ``task_discovery``, and answer HTTP 500 — at which point the
    ``hermes-discovery`` queue redelivers and buys a **second full paid cycle
    for a run that had already failed**. The bookkeeping must not be able to do
    that.
    """

    def unreachable(user_id):
        raise RuntimeError("503 Firestore is unavailable")

    monkeypatch.setattr(discovery, "_user_ref", unreachable)

    assert discovery._release_slot("u1", "discovery", "cron", T0) is False


def test_a_cycle_survives_a_release_that_cannot_reach_firestore(
    cycle_world, slot_world
):
    """The consequence chain of the test above, end to end: the cycle still
    reports its own failure the way it always did, and nothing reaches the task
    handler that the queue would read as "retry this"."""
    slot_world.tick(T0)
    cycle_world.hooks.during = _explode()

    def unreachable():
        raise RuntimeError("503 Firestore is unavailable")

    cycle_world.doc.get = unreachable

    cycle_world.run(T0 + timedelta(minutes=1))  # returns, rather than raising


def test_a_successful_sweep_claims_its_slot_and_hands_the_lease_back(
    slot_world, monkeypatch
):
    """The matched pair, made whole: the discovery cycle's success write was
    pinned and the sweep's was not, so ``sweep_lease``'s release could be
    deleted with the whole suite green — which is what got PR D sent back."""

    async def fake_sweep(user_id):
        return {"checked": 4, "dismissed": 1}

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        pass

    monkeypatch.setattr(discovery, "sweep_postings", fake_sweep)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    slot_world.seed(sweep_lease=discovery._lease(T0))

    slot_world.freeze(T0 + timedelta(minutes=2))
    asyncio.run(discovery.run_sweep_cycle("u1", trigger="cron"))

    state = slot_world.doc.state
    assert state["last_sweep_at"] == (T0 + timedelta(minutes=2)).isoformat()
    assert "sweep_lease" not in state
    assert state["last_sweep"]["dismissed"] == 1


def test_a_claim_wins_on_its_retry_when_an_unrelated_write_landed_underneath(
    slot_world,
):
    """The retry's re-read, and the *winning* path — the first round covered
    only the losing one, so the re-read could be deleted and the retry become
    dead code with the suite green.

    ``tools.matching.budget.reserve`` reserves out of this very document in a
    transaction, so a scoring run in flight moves ``update_time`` without going
    anywhere near the slot. Without the re-read the second attempt reuses the
    stale ``update_time`` and is *guaranteed* to lose, so every tick that raced
    a scoring run would silently skip its cycle.
    """
    doc, bumped = slot_world.doc, []
    real_get = doc.get

    def budget_writes_underneath():
        snap = real_get()
        if not bumped:
            bumped.append(None)
            doc.set({"scoring_budget": {"day": {"used": 12}}}, merge=True)
        return snap

    doc.get = budget_writes_underneath

    assert discovery._claim_slot("u1", "discovery", 24, T0) is True
    assert bumped, "nothing wrote underneath the claim — this pins nothing"
    assert doc.state["discovery_lease"]["acquired_at"] == T0.isoformat()
    # The unrelated write is still there: the retry re-read it rather than
    # writing the whole map back over it.
    assert doc.data["scoring_budget"] == {"day": {"used": 12}}


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(T0, id="aware-datetime"),
        pytest.param(T0.replace(tzinfo=None), id="naive-datetime"),
        pytest.param(T0.isoformat(), id="aware-string"),
        pytest.param(T0.replace(tzinfo=None).isoformat(), id="naive-string"),
    ],
)
def test_every_shape_a_stored_timestamp_comes_back_in_is_comparable(stored):
    """``_parse_ts`` normalises four shapes, and all four are reachable.

    Firestore hands timestamps back as ``datetime`` objects, not strings, so a
    lease written by anything but this module's ``isoformat`` — or a
    hand-repaired document — reads back as one. If any of these escaped naive,
    the comparison in ``_lease_held`` would raise ``TypeError: can't compare
    offset-naive and offset-aware datetimes`` straight up through ``_due`` and
    out of ``tick_user``, killing that user's loops entirely.
    """
    parsed = discovery._parse_ts(stored)

    assert parsed == T0
    assert parsed is not None and parsed.tzinfo is not None
    # The behavioural form: neither of the two consumers may raise on it.
    assert discovery._lease_held({"expires_at": stored}, T0 - timedelta(minutes=1))
    assert discovery._due(stored, 24, T0 + timedelta(hours=25)) is True


def test_a_cycle_re_stamps_its_lease_against_the_run_not_the_queue_wait(
    cycle_world, slot_world
):
    """**The claim and the run do not start together.**

    ``tick_user`` leases the slot before ``enqueue_cycle``, but the TTL is
    derived from the dispatch deadline, which bounds run *duration*. A claim
    stamped at dispatch time therefore spends its TTL sitting in a queue: the
    ``discovery`` queue allows three concurrent dispatches and the worker runs
    at ``containerConcurrency = 1`` across five queues, so a fan-out that finds
    several users due can leave the last of them waiting tens of minutes. Its
    lease would lapse mid-run, and the next hourly cron is a *different* task
    name, so nothing would dedupe the duplicate — a regression against the old
    pre-claim, which held for a full interval however long the queue was.
    """
    slot_world.tick(T0)
    dispatched_with = slot_world.doc.state["discovery_lease"]
    started = T0 + timedelta(minutes=20)  # 20 minutes behind the queue
    still_running = started + timedelta(minutes=15)

    # The lease the *tick* took is already dead by the time this run ends.
    assert not discovery._lease_held(dispatched_with, still_running)

    held: list[dict] = []
    cycle_world.hooks.during = lambda: held.append(
        cycle_world.doc.state["discovery_lease"]
    )
    cycle_world.run(started)

    assert held, "the run never reached its work — this pins nothing"
    assert held[0]["acquired_at"] == started.isoformat()
    assert discovery._lease_held(held[0], still_running)


def test_a_sweep_re_stamps_its_lease_too(slot_world, monkeypatch):
    """The matched pair again — the sweep queues behind the same worker."""
    held: list[dict] = []

    async def fake_sweep(user_id):
        held.append(slot_world.doc.state["sweep_lease"])
        return {"checked": 0, "dismissed": 0}

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        pass

    monkeypatch.setattr(discovery, "sweep_postings", fake_sweep)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    slot_world.seed(sweep_lease=discovery._lease(T0))

    started = T0 + timedelta(minutes=20)
    slot_world.freeze(started)
    asyncio.run(discovery.run_sweep_cycle("u1", trigger="cron"))

    assert held[0]["acquired_at"] == started.isoformat()


def test_a_manual_run_never_stamps_a_slot_lease(cycle_world, slot_world):
    """A manual or onboarding run holds no slot — it went straight to
    ``dispatch_cycle``, past ``tick_user`` — so stamping one would lock the
    scheduled ticks out of a cadence this run never joined, for a full TTL."""
    held: list = []
    cycle_world.hooks.during = lambda: held.append(
        cycle_world.doc.state.get("discovery_lease")
    )

    cycle_world.run(T0, trigger="manual")

    assert held == [None]


def test_a_re_stamp_leaves_the_rest_of_the_slot_state_alone(cycle_world, slot_world):
    """It is a dotted-path write like the claim, for the same reason: a nested
    map would take ``discovery_state`` wholesale and wipe the last run's metrics
    and the other loop's slot on the way past."""
    slot_world.seed(
        last_sweep_at="2026-08-25T00:00:00+00:00", last_discovery={"scored": 7}
    )
    slot_world.tick(T0)
    cycle_world.hooks.during = _explode()

    cycle_world.run(T0 + timedelta(minutes=20))

    state = cycle_world.doc.state
    assert state["last_sweep_at"] == "2026-08-25T00:00:00+00:00"
    assert state["last_discovery"] == {"scored": 7}
