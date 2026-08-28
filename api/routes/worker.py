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

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from google.cloud import firestore
from pydantic import BaseModel

from api.routes.applications import (
    SUBMITTABLE,
    application_ref,
    run_submission,
    run_tailoring,
)
from api.routes.discovery import run_discovery_cycle, run_sweep_cycle
from obs.logging import get_logger, run_context
from tools.applications import state
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


class TailorTask(BaseModel):
    user_id: str
    job_id: str


class ApplyTask(BaseModel):
    user_id: str
    app_id: str
    # Worker-only, and it stays that way: no model behind a public route has
    # this field, and the only caller that could set it is a task created by
    # hand. It exists so the submission path can be driven end to end against a
    # live posting for $0 — see run_submission's dry_run.
    dry_run: bool = False


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


# --------------------------------------------------------------------------
# The application funnel (Phase 2). These handlers landed one merge ahead of
# their callers — CI deploys hermes-api and hermes-worker from the same commit,
# so shipping both together would have left a window where the API enqueued to
# a worker route that didn't exist yet. This is the merge that turns the
# callers on (``applications.dispatch_tailor`` / ``dispatch_apply``).
# --------------------------------------------------------------------------


@router.post("/tailor")
async def task_tailor(body: TailorTask) -> dict:
    """Tailor an approved job — the work ``jobs.decide`` schedules on approval.

    **This handler takes no claim, on purpose.** ``run_tailoring`` claims its
    own work by CAS-ing the Application ``queued → tailoring`` before it spends
    an LLM run, so the claim already happens exactly once and a second one here
    would either double-claim or (worse) claim a state the callee then refuses
    to re-claim, dropping the work on the floor. That single claim is also what
    makes a redelivered task safe: delivery #2 finds the document in
    ``tailoring`` or past it, the edge is illegal, and the task returns without
    spending anything.

    No ``run_context`` either, unlike ``/tasks/score``: the callee opens its own
    and flushes its own ledger doc.
    """
    _require_worker()
    await run_tailoring(body.user_id, body.job_id)
    return {"ok": True}


@router.post("/apply")
async def task_apply(body: ApplyTask) -> dict:
    """Submit an application the API already claimed — or rehearse one for $0.

    **The claim is split in two, and neither half is taken twice.**
    ``POST /applications/{id}/submit`` compare-and-swaps
    ``ready_for_review → submitting`` and only then dispatches the work; that
    swap is the double-click guard and it has to stay on the request, because
    it is what turns the losing click into a 409. By the time the task gets
    here the status *is* that claim — and ``submitting → submitting`` is
    illegal, exactly as it must be — so the other claim, the lease, answers the
    question a status can't: *is a process running this right now?*

    **That lease is taken by ``run_submission``, not by this handler.** It used
    to be taken here, which fenced worker against worker and nothing else:
    ``dispatch_apply`` still runs the same function as a background task
    wherever QUEUE_MODE is off, and a Cloud Run traffic migration puts two
    hermes-api revisions on the same documents for the length of the rollout. A
    claim only one of those paths takes is not a lock. Moving it into the callee
    puts every path that can drive a submission behind the same primitive, and
    is why this handler now reads as thinly as ``/tasks/tailor`` does — ``ran``
    is simply whether the callee got the claim.

    **What is load-bearing in production today is still not that.** The
    ``hermes-apply`` queue is provisioned with ``max_attempts = 1``, so an apply
    task is never redelivered in the first place; the lease is what makes this
    correct if that is ever raised, and what a reaper (and
    ``cli/unwedge_submitting``) can read to tell a live run from a dead one.

    ``dry_run`` takes no lease because it makes no claim: it writes no status of
    its own, so nothing a repeat could corrupt, and repeating it costs nothing.
    The status check below is a courtesy, not a safety property — a rehearsal
    has no business opening a browser on an application that is mid-submit or
    already submitted, but what makes it *safe* is that the one write it can
    still reach (``posting_removed``, from ``run_submission``'s pre-flight
    check) carries its own ``allowed_from`` inside the swap. Reading the status
    here and acting on it later would otherwise be the same filter-outside-the-
    swap bug the state machine exists to prevent.

    Failures are the callee's to record (it writes ``failed`` and returns), so
    this route answers 200 for everything short of an infrastructure fault —
    the same contract as the cycle handlers above, and what stops Cloud Tasks
    from re-driving a browser at a posting that already rejected us.
    """
    _require_worker()
    task_log = log.bind(user_id=body.user_id, app_id=body.app_id)

    if body.dry_run:
        ref = application_ref(body.user_id, body.app_id)
        snap = await asyncio.to_thread(ref.get)
        if not snap.exists:
            task_log.info("task.apply.missing")
            return {"ok": True, "ran": False, "dry_run": True}
        current = (snap.to_dict() or {}).get(state.STATUS_FIELD)
        if current not in SUBMITTABLE:
            task_log.info("task.apply.dry_run_skipped", current=current)
            return {"ok": True, "ran": False, "dry_run": True}
        await run_submission(body.user_id, body.app_id, dry_run=True)
        return {"ok": True, "ran": True, "dry_run": True}

    # False means the document is gone or the claim was lost — a duplicate
    # delivery, or a document that moved on. 200, not 4xx or 5xx: this task has
    # nothing left to do, and a retry would only ask the same question again.
    ran = await run_submission(body.user_id, body.app_id)
    return {"ok": True, "ran": ran}
