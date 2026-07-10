# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Worker task handlers + the queue/in-process dispatch seam.

Pins the security contract (task routes 404 without WORKER_MODE, so the
public API service never exposes them) and the dispatch behavior on both
sides of QUEUE_MODE.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.discovery as discovery
import api.routes.worker as worker


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(worker.router)
    return TestClient(app)


def test_task_routes_404_without_worker_mode(client, monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)
    for path in ("/tasks/discovery", "/tasks/sweep"):
        assert client.post(path, json={"user_id": "u1"}).status_code == 404
    for path in ("/tasks/score", "/tasks/batch/start"):
        assert client.post(path, json={"user_id": "u1"}).status_code == 404
    assert client.post("/tasks/batch/resume").status_code == 404


def test_task_discovery_runs_cycle_inline(client, monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    calls = []

    async def fake_cycle(user_id, *, trigger):
        calls.append((user_id, trigger))

    monkeypatch.setattr(worker, "run_discovery_cycle", fake_cycle)
    resp = client.post("/tasks/discovery", json={"user_id": "u1", "trigger": "manual"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert calls == [("u1", "manual")]


def test_task_score_returns_counts(client, monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")

    async def fake_score(user_id, *, limit=None):
        assert (user_id, limit) == ("u1", 50)
        return {"scored": 3, "discarded": 1, "failed": 0, "pending": 4}

    monkeypatch.setattr(worker, "score_pending_jobs", fake_score)
    resp = client.post("/tasks/score", json={"user_id": "u1", "limit": 50})
    assert resp.status_code == 200
    assert resp.json()["scored"] == 3


def test_task_batch_start_and_resume_call_through(client, monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    calls = []

    async def fake_start(user_id, *, limit=None):
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
