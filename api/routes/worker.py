# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Task handlers for the hermes-worker service (Phase B).

Cloud Tasks pushes queued work here. The routes exist in every deployment of
the shared image but are enabled only where ``WORKER_MODE`` is on — on the
public hermes-api service they 404, so the only way in is through the private
worker service, where Cloud Run's platform-level OIDC has already
authenticated the caller (the ``hermes-tasks`` invoker SA).

Handlers run the work inline (not as background tasks) so the HTTP status
reflects the outcome and Cloud Tasks' retry policy applies to infrastructure
failures (e.g. instance death mid-run). Cycle functions swallow their own
work-level exceptions by design — a failed *cycle* waits for the next
scheduler tick rather than hot-retrying paid LLM calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from google.cloud import firestore
from pydantic import BaseModel

from api.routes.discovery import run_discovery_cycle, run_sweep_cycle
from obs.logging import get_logger, run_context
from tools.matching import batch_runs
from tools.matching.score import score_pending_jobs
from tools.queues import worker_mode
from tools.run_costs import persist_run_cost

log = get_logger("api.worker")

router = APIRouter(prefix="/tasks", tags=["worker"])


class CycleTask(BaseModel):
    user_id: str
    trigger: str = "queued"


class ScoreTask(BaseModel):
    user_id: str
    limit: int | None = None
    # Deliberately no ignore_budget: the scoring cap must not be switchable
    # from the HTTP surface. The escape hatch is cli/run_matching only.


def _require_worker() -> None:
    if not worker_mode():
        # 404 (not 403): on the public API service these routes don't exist.
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/discovery")
async def task_discovery(body: CycleTask) -> dict:
    """Full discovery cycle: fetch -> filter -> persist -> score."""
    _require_worker()
    await run_discovery_cycle(body.user_id, trigger=body.trigger)
    return {"ok": True}


@router.post("/sweep")
async def task_sweep(body: CycleTask) -> dict:
    """Liveness sweep: dismiss postings the ATS took down."""
    _require_worker()
    await run_sweep_cycle(body.user_id, trigger=body.trigger)
    return {"ok": True}


@router.post("/score")
async def task_score(body: ScoreTask) -> dict:
    """Standalone scoring of pending jobs (online mode).

    Wrapped in a ``run_context`` like the discovery/sweep cycles are: without
    one this handler's Pro calls have no ``run_id`` to accumulate under, so
    its spend never reaches the ledger and the job docs it writes land with a
    null ``scored_run_id``.

    That ``run_id`` is for *measurement* only — ``cycle_id=None`` keeps this
    task drawing down the cycle window discovery opened rather than opening a
    fresh one, so "300 per cycle" can't be reset on demand by anything that
    can reach this route. (Consequence, since the cycle counter has no
    time-based rollover: once a window is spent, an ad-hoc score task gets
    nothing until the next discovery cycle — not merely until midnight.)
    """
    _require_worker()
    with run_context("score_task", user_id=body.user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        counts: dict = {}
        try:
            counts = await score_pending_jobs(
                body.user_id, limit=body.limit, cycle_id=None
            )
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                body.user_id,
                run_id,
                runner="score_task",
                started_at=started_at,
                jobs={
                    "pending": counts.get("pending", 0),
                    "scored": counts.get("scored", 0),
                    "discarded": counts.get("discarded", 0),
                    "failed": counts.get("failed", 0),
                },
            )
    return {"ok": True, **counts}


@router.post("/batch/start")
async def task_batch_start(body: ScoreTask) -> dict:
    """Submit a resumable batch run (Phase C); resume ticks ingest results.

    Also under a ``run_context`` — and here the ``run_id`` is what the run doc
    records as ``origin_run_id``, so the batch's tokens, priced hours later by
    a resume pass, land on this task's ledger doc instead of being scattered
    across whichever worker ticks ingested them. ``cycle_id=None`` for the
    same reason as ``/tasks/score`` above: measure under this run, spend out
    of the open window.
    """
    _require_worker()
    with run_context("batch_start", user_id=body.user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        result: dict = {}
        try:
            result = await batch_runs.start(
                body.user_id, limit=body.limit, cycle_id=None
            )
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                body.user_id,
                run_id,
                runner="batch_start",
                started_at=started_at,
                batch_run=result.get("run"),
            )
    return {"ok": True, **result}


@router.post("/batch/resume")
async def task_batch_resume() -> dict:
    """One resume pass: poll in-flight batch runs, ingest whatever finished.

    Enqueued hourly by the cron tick while runs are in flight. Runs inline so
    a mid-ingest instance death surfaces as a task failure and Cloud Tasks
    retries it — the claim TTL plus idempotent ingestion make that safe.
    """
    _require_worker()
    summary = await batch_runs.resume()
    return {"ok": True, **summary}
