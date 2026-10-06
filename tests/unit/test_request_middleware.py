# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Request-context middleware: Cloud Tasks correlation fields on the worker.

The worker binds what enqueued a task (``origin_request_id`` /
``origin_run_id``) and Cloud Tasks' own attempt fields, while still minting
its own ``request_id``. The public API ignores those headers entirely: there
they are browser-settable.
"""

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.app_utils.middleware import RequestContextMiddleware

_TASK_FIELDS = (
    "origin_request_id",
    "origin_run_id",
    "task_name",
    "retry_count",
    "execution_count",
)

# What Cloud Tasks sends on a third attempt of a task enqueued from a click.
_CLOUD_TASKS_HEADERS = {
    "X-Origin-Request-Id": "rid-click-1",
    "X-Origin-Run-Id": "run.42",
    "X-CloudTasks-QueueName": "hermes-score",
    "X-CloudTasks-TaskName": "manual-score-u1-202610061200",
    "X-CloudTasks-TaskRetryCount": "2",
    "X-CloudTasks-TaskExecutionCount": "1",
    "X-CloudTasks-TaskETA": "1791288000.0",
}


@pytest.fixture
def client():
    """An app whose one route returns the structlog context it ran under."""
    structlog.contextvars.clear_contextvars()
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.post("/tasks/echo")
    def echo() -> dict:
        return dict(structlog.contextvars.get_contextvars())

    yield TestClient(app)
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)


def test_the_worker_binds_every_field_from_a_cloud_tasks_request(client, worker):
    bound = client.post("/tasks/echo", headers=_CLOUD_TASKS_HEADERS).json()

    assert bound["origin_request_id"] == "rid-click-1"
    assert bound["origin_run_id"] == "run.42"
    assert bound["task_name"] == "manual-score-u1-202610061200"
    assert bound["retry_count"] == 2
    assert bound["execution_count"] == 1
    # Ints, so ``retry_count>0`` is a numeric comparison in Cloud Logging.
    assert type(bound["retry_count"]) is int
    assert type(bound["execution_count"]) is int


def test_a_first_attempt_binds_zero_rather_than_nothing(client, worker):
    headers = _CLOUD_TASKS_HEADERS | {
        "X-CloudTasks-TaskRetryCount": "0",
        "X-CloudTasks-TaskExecutionCount": "0",
    }
    bound = client.post("/tasks/echo", headers=headers).json()
    assert bound["retry_count"] == 0
    assert bound["execution_count"] == 0


def test_the_worker_binds_none_of_them_without_the_headers(client, worker):
    """Cloud Scheduler and a bare POST carry none of these."""
    bound = client.post("/tasks/echo").json()

    for key in _TASK_FIELDS:
        assert key not in bound, key
    assert bound["request_id"]


def test_the_api_ignores_the_headers_even_when_present(client, api):
    bound = client.post("/tasks/echo", headers=_CLOUD_TASKS_HEADERS).json()

    for key in _TASK_FIELDS:
        assert key not in bound, key


def test_the_worker_still_mints_its_own_request_id(client, worker):
    """The origin is a separate field: one ``request_id`` never spans both
    services' log lines."""
    resp = client.post("/tasks/echo", headers=_CLOUD_TASKS_HEADERS)
    bound = resp.json()

    assert bound["origin_request_id"] == "rid-click-1"
    assert bound["request_id"] != "rid-click-1"
    assert resp.headers["X-Request-Id"] == bound["request_id"]


@pytest.mark.parametrize(
    "bad",
    # Latin-1 bytes: what a non-ASCII header value arrives as.
    ["x" * 65, "rid with spaces", 'rid"}', "rid;drop", b"r\xefd"],
)
def test_the_worker_drops_garbage_origin_ids(client, worker, bad):
    headers = _CLOUD_TASKS_HEADERS | {
        "X-Origin-Request-Id": bad,
        "X-Origin-Run-Id": bad,
    }
    bound = client.post("/tasks/echo", headers=headers).json()

    assert "origin_request_id" not in bound
    assert "origin_run_id" not in bound
    # The well-formed fields beside them are unaffected.
    assert bound["task_name"] == "manual-score-u1-202610061200"


@pytest.mark.parametrize("bad", ["-1", "2.0", "two", "1234567", " 2"])
def test_the_worker_drops_malformed_counts(client, worker, bad):
    headers = _CLOUD_TASKS_HEADERS | {
        "X-CloudTasks-TaskRetryCount": bad,
        "X-CloudTasks-TaskExecutionCount": bad,
    }
    bound = client.post("/tasks/echo", headers=headers).json()

    assert "retry_count" not in bound
    assert "execution_count" not in bound


def test_the_worker_drops_a_malformed_task_name(client, worker):
    headers = _CLOUD_TASKS_HEADERS | {"X-CloudTasks-TaskName": "t" * 501}
    bound = client.post("/tasks/echo", headers=headers).json()
    assert "task_name" not in bound
