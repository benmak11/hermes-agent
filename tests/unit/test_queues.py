# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cloud Tasks dispatch (Phase B): env gates, task shape, named-task dedupe."""

import json

import pytest
from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2

from tools import queues


class _FakeClient:
    def __init__(self, raise_exists: bool = False):
        self.created: list[dict] = []
        self._raise_exists = raise_exists

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project, location, queue, task_id):
        return f"{self.queue_path(project, location, queue)}/tasks/{task_id}"

    def create_task(self, *, parent, task):
        if self._raise_exists:
            raise AlreadyExists("task exists")
        self.created.append({"parent": parent, "task": task})


@pytest.fixture
def queue_env(monkeypatch):
    monkeypatch.setenv("QUEUE_MODE", "1")
    monkeypatch.setenv("WORKER_URL", "https://worker.example.run.app")
    monkeypatch.setenv("TASKS_SA_EMAIL", "tasks@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    monkeypatch.setenv("TASKS_LOCATION", "us-central1")


def test_enabled_and_worker_mode_env_gates(monkeypatch):
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    monkeypatch.delenv("WORKER_MODE", raising=False)
    assert not queues.enabled()
    assert not queues.worker_mode()
    monkeypatch.setenv("QUEUE_MODE", "true")
    monkeypatch.setenv("WORKER_MODE", "1")
    assert queues.enabled()
    assert queues.worker_mode()
    monkeypatch.setenv("QUEUE_MODE", "0")
    assert not queues.enabled()


def test_enqueue_builds_oidc_post_task(queue_env, monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(queues, "_client", lambda: (tasks_v2, client))

    ok = queues.enqueue(
        "discovery",
        "/tasks/discovery",
        {"user_id": "u1", "trigger": "cron"},
        task_id="cron-discovery-u1-2026071013",
    )

    assert ok is True
    assert len(client.created) == 1
    created = client.created[0]
    assert created["parent"].endswith("/queues/hermes-discovery")
    task = created["task"]
    assert task["name"].endswith("/tasks/cron-discovery-u1-2026071013")
    req = task["http_request"]
    assert req["url"] == "https://worker.example.run.app/tasks/discovery"
    assert json.loads(req["body"]) == {"user_id": "u1", "trigger": "cron"}
    assert req["oidc_token"]["audience"] == "https://worker.example.run.app"
    assert (
        req["oidc_token"]["service_account_email"]
        == "tasks@proj.iam.gserviceaccount.com"
    )


def test_enqueue_dedupes_on_existing_task_name(queue_env, monkeypatch):
    monkeypatch.setattr(
        queues, "_client", lambda: (tasks_v2, _FakeClient(raise_exists=True))
    )
    ok = queues.enqueue("discovery", "/tasks/discovery", {}, task_id="dup")
    assert ok is False


def test_enqueue_rejects_unknown_queue(queue_env):
    with pytest.raises(ValueError, match="Unknown queue"):
        queues.enqueue("nope", "/tasks/nope", {})


def test_enqueue_requires_worker_url(queue_env, monkeypatch):
    monkeypatch.delenv("WORKER_URL")
    with pytest.raises(RuntimeError, match="WORKER_URL"):
        queues.enqueue("discovery", "/tasks/discovery", {})
