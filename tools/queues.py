# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cloud Tasks dispatch for long-running work (Phase B ops architecture).

Discovery/scoring cycles used to run as in-process background tasks on the
API server — sequential, unthrottled, and lost on instance restart. With
``QUEUE_MODE`` on, work is enqueued to per-type Cloud Tasks queues that push
to the dedicated ``hermes-worker`` Cloud Run service instead: queue rate
limits stop 429 storms and retry-amplified token burn, and named tasks give
idempotency (a task id can't be re-created while its tombstone lives, so a
double-click or overlapping tick dedupes at the queue).

``QUEUE_MODE`` unset/off means every caller falls back to running the work
in-process, which keeps local dev, tests, and the pre-worker deployment
working with zero infrastructure.

Env contract (all required only when QUEUE_MODE is on):
- ``QUEUE_MODE``: "1"/"true"/"on" enables dispatch.
- ``WORKER_URL``: base URL of the hermes-worker service (terraform output).
- ``TASKS_SA_EMAIL``: invoker SA for the OIDC token (terraform output).
- ``TASKS_LOCATION``: queue region, defaults to us-central1.
- ``GOOGLE_CLOUD_PROJECT``: the project hosting the queues.
"""

from __future__ import annotations

import json
import os

from obs.logging import get_logger

log = get_logger("tools.queues")

# Queue names match deployment/terraform/single-project/worker.tf.
QUEUE_PREFIX = "hermes"
KNOWN_QUEUES = {"extract", "discovery", "score", "tailor", "apply"}

# Cloud Tasks caps HTTP task dispatch at 30 minutes; work that can outlive
# this (full-backlog batch scoring) belongs to the Phase C resumable
# pipelines, not a queue task.
_DISPATCH_DEADLINE_SECONDS = 1800


def enabled() -> bool:
    """True when work should be enqueued instead of run in-process."""
    return os.getenv("QUEUE_MODE", "").strip().lower() in {"1", "true", "on"}


def worker_mode() -> bool:
    """True on the hermes-worker service (enables /tasks/* handlers).

    The worker runs the same image as hermes-api; this env var is what makes
    a deployment *be* the worker. The worker service is deployed private
    (``--no-allow-unauthenticated``), so platform-level OIDC — not app code —
    authenticates Cloud Tasks and Cloud Scheduler calls.
    """
    return os.getenv("WORKER_MODE", "").strip().lower() in {"1", "true", "on"}


def worker_url() -> str:
    url = os.getenv("WORKER_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("QUEUE_MODE is on but WORKER_URL is not set.")
    return url


def _client():
    # Lazy import: google-cloud-tasks only has to exist/authenticate on paths
    # that actually enqueue.
    from google.cloud import tasks_v2

    return tasks_v2, tasks_v2.CloudTasksClient()


def enqueue(
    queue: str,
    path: str,
    payload: dict,
    *,
    task_id: str | None = None,
) -> bool:
    """Enqueue a POST to the worker; returns False when deduped.

    ``task_id`` makes the task named: Cloud Tasks refuses a name that exists
    or recently completed (~1h tombstone), which is the overlap guard — e.g.
    ``tick-{user}-{YYYYMMDDHH}`` dedupes to one tick per user per hour no
    matter how many triggers fire.
    """
    if queue not in KNOWN_QUEUES:
        raise ValueError(f"Unknown queue {queue!r}; expected one of {KNOWN_QUEUES}")
    # Validate config before constructing the client: CloudTasksClient() needs
    # credentials, so a misconfigured env should fail on the config error, not
    # a DefaultCredentialsError from the transport.
    url = worker_url()
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.getenv("TASKS_LOCATION", "us-central1")
    queue_name = f"{QUEUE_PREFIX}-{queue}"

    tasks_v2, client = _client()
    from google.api_core.exceptions import AlreadyExists

    parent = client.queue_path(project, location, queue_name)

    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{url}{path}",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            "oidc_token": {
                "service_account_email": os.environ["TASKS_SA_EMAIL"],
                "audience": url,
            },
        },
        "dispatch_deadline": {"seconds": _DISPATCH_DEADLINE_SECONDS},
    }
    if task_id:
        task["name"] = client.task_path(project, location, queue_name, task_id)

    try:
        client.create_task(parent=parent, task=task)
    except AlreadyExists:
        log.info("queue.deduped", queue=queue_name, task_id=task_id, path=path)
        return False
    log.info("queue.enqueued", queue=queue_name, task_id=task_id, path=path)
    return True
