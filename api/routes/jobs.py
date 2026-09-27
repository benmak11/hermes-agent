# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Job vetting endpoints: list scored pending jobs, record a decision."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from api.deps import SpendConfirm, required, verify_user
from api.routes.applications import application_id, dispatch_tailor
from api.routes.discovery import refuse_live_runs, tick_user
from models.job import Job
from obs.logging import get_logger, run_context
from tools import queues, spend
from tools.applications import state as app_state
from tools.ats.validate import check_posting
from tools.matching.score import score_pending_jobs
from tools.run_costs import persist_run_cost
from tools.spend.estimate import Estimate

router = APIRouter(tags=["jobs"])
log = get_logger("api.jobs")

#: The spend seam for the one paid route in this module, built once at import
#: rather than inline in the signature — ``Depends(required(...))`` in an
#: argument default constructs the dependency on every call (B008), and it is
#: a singleton by nature.
SCORE_CONSENT = Depends(required(spend.SCORE_BACKLOG))

#: Where a confirmed backlog score goes on the queue. **Not ``/tasks/score``**
#: — that handler is online-only, and this intent has to keep reaching
#: ``batch_runs.score_or_start_run``, which sends a backlog over
#: ``BATCH_MIN_PENDING`` to a half-price batch. Sending it to the online
#: scorer would double the cost of the one workflow this route replaced, and
#: make the batch-rate floor of every quote a price the product cannot reach.
SCORE_BACKLOG_TASK_PATH = "/tasks/score/backlog"

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


@router.get("/jobs/pending")
def list_pending_jobs(
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
    min_score: int = 60,
) -> dict:
    """Return scored, still-pending jobs above min_score, ranked high to low.

    Also reports how many pending jobs exist *before* each filter, so the client
    can tell three states apart that otherwise all render as an empty list:
    nothing discovered yet, discovered but not scored yet, and scored but all
    below the threshold. Only the last one is fixed by lowering the threshold,
    and telling the other two to lower it sends them nowhere.

    Both counts are free: this already streams every pending document and
    filters in Python, so they are tallies of a pass that was happening anyway,
    not extra queries.
    """
    # Opportunistic scheduler tick (throttled in-process): opening the review
    # queue runs any due auto-discovery/sweep loop without external cron infra.
    background_tasks.add_task(tick_user, user_id)
    snaps = (
        _client()
        .collection("users")
        .document(user_id)
        .collection("jobs")
        .where(filter=FieldFilter("user_decision", "==", "pending"))
        .stream()
    )
    jobs = []
    pending_total = 0
    scored_total = 0
    for snap in snaps:
        pending_total += 1
        d = snap.to_dict()
        match = d.get("match")
        if not match:  # not scored yet
            continue
        scored_total += 1
        if match.get("overall_score", 0) < min_score:
            continue
        jobs.append({"id": snap.id, **d})
    jobs.sort(key=lambda j: j["match"]["overall_score"], reverse=True)
    return {
        "jobs": jobs,
        "pending_total": pending_total,
        "scored_total": scored_total,
    }


async def run_score_backlog(user_id: str) -> None:
    """Score this user's pending backlog, in-process, under its own run id.

    The no-queue counterpart of ``/tasks/score``, and deliberately the same
    shape as it: a ``run_context`` so the spend has a ``run_id`` to accumulate
    under and the job docs land with a real ``scored_run_id``, and a ledger
    flush in a ``finally`` so a run that dies after paying still records what
    it paid.

    ``cycle_id=None``, exactly as the worker task passes: this draws down the
    window the last discovery cycle opened rather than opening a fresh one, so
    "200 per cycle" cannot be reset by clicking the button again. The budget
    is untouched by this PR and this is the path that keeps it that way.
    """
    with run_context("score_backlog", user_id=user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        counts: dict = {}
        try:
            counts = await score_pending_jobs(user_id, cycle_id=None)
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                user_id,
                run_id,
                runner="score_backlog",
                trigger="manual",
                started_at=started_at,
                jobs={
                    "pending": counts.get("pending", 0),
                    "scored": counts.get("scored", 0),
                    "discarded": counts.get("discarded", 0),
                    "failed": counts.get("failed", 0),
                },
            )


@router.post("/jobs/score", dependencies=[Depends(refuse_live_runs)])
async def score_backlog(
    background_tasks: BackgroundTasks,
    body: SpendConfirm | None = None,
    user_id: str = Depends(verify_user),
    estimate: Estimate = SCORE_CONSENT,
) -> dict:
    """The second, priced click: score the jobs discovery already found.

    ``POST /settings/discovery/run`` now finds without scoring. This is the
    other half — and unlike that route it is *only* ever the paid verb, so it
    carries the seam as a hard dependency: no valid ``confirm`` token, no work,
    402 with a fresh quote and a fresh token. There is no unpriced path
    through here to fall back to.

    **The seam does not grant anything.** ``score_pending_jobs`` takes its own
    reservation from ``tools.matching.budget`` exactly as it always has, so the
    per-cycle and per-day caps still bound what a yes can cost — the estimate
    is a *reading* of that grant, never a substitute for it.

    Refused from a local process for the reason the discovery route is: an
    enqueue from a laptop hands the same paid work to the real worker, one
    process further away. That refusal is a **route-level dependency**, not a
    check in this body, so it runs *before* the seam consumes the token —
    otherwise a developer's 403 would silently burn a single-use
    confirmation and they would have to confirm again to be refused again.
    """
    log.info(
        "jobs.score_confirmed",
        user_id=user_id,
        units=estimate.units,
        usd_low=estimate.usd_low,
        usd_high=estimate.usd_high,
        rate_source=estimate.rate_source,
    )
    quoted = estimate.as_dict()
    if queues.enabled():
        # Minute-granular id, like the manual discovery run: a double-click
        # dedupes, a deliberate re-run a minute later does not.
        task_id = f"manual-score-{user_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M')}"
        queued = await asyncio.to_thread(
            queues.enqueue,
            "score",
            SCORE_BACKLOG_TASK_PATH,
            {"user_id": user_id},
            task_id=task_id,
        )
        return {
            "ok": True,
            "mode": "queued",
            "deduped": not queued,
            "estimate": quoted,
        }
    background_tasks.add_task(run_score_backlog, user_id)
    return {"ok": True, "mode": "in_process", "estimate": quoted}


@router.get("/jobs/decided")
def list_decided_jobs(
    decision: Literal["approved", "rejected", "starred"],
    user_id: str = Depends(verify_user),
) -> dict:
    """Jobs the user already decided on (the starred / skipped shelves).

    Scored jobs only, ranked high to low — same shape as /jobs/pending so the
    web app can reuse its card rendering.
    """
    snaps = (
        _client()
        .collection("users")
        .document(user_id)
        .collection("jobs")
        .where(filter=FieldFilter("user_decision", "==", decision))
        .stream()
    )
    jobs = []
    for snap in snaps:
        d = snap.to_dict()
        if not d.get("match"):
            continue
        jobs.append({"id": snap.id, **d})
    jobs.sort(key=lambda j: j["match"]["overall_score"], reverse=True)
    return {"jobs": jobs}


async def dismiss_skipped_if_posting_removed(user_id: str, job_id: str) -> None:
    """Background task: validate a freshly skipped job's posting.

    A skipped job sits on the Skipped shelf offering restore/approve — if the
    posting has died there is nothing left to act on, so dismiss it outright.
    Same fail-open contract as the application path: only a definitive removal
    dismisses. (Approvals are covered by the check at the top of tailoring.)
    """
    task_log = log.bind(user_id=user_id, job_id=job_id, task="skip_validation")
    job_ref = (
        _client()
        .collection("users")
        .document(user_id)
        .collection("jobs")
        .document(job_id)
    )
    snap = job_ref.get()
    if not snap.exists:
        return
    job = Job.model_validate(snap.to_dict())
    if await check_posting(job) != "removed":
        return
    # The user may have restored or approved the job while we probed; the
    # approval path runs its own check, so only dismiss a still-skipped job.
    if (job_ref.get().to_dict() or {}).get("user_decision") != "rejected":
        task_log.info("job.posting_removed_but_redecided", url=job.url)
        return
    job_ref.update(
        {
            "user_decision": "dismissed",
            "posting_removed_at": datetime.now(UTC).isoformat(),
        }
    )
    task_log.info("job.posting_removed", url=job.url)


class Decision(BaseModel):
    # "pending" reverts a prior decision — the undo path and the
    # starred/skipped "restore to queue" action.
    decision: Literal["approved", "rejected", "starred", "pending"]


@router.post("/jobs/{job_id}/decide")
def decide(
    job_id: str,
    body: Decision,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
) -> dict:
    """Record the user's decision on a job.

    Approving a job kicks off tailoring: create the Application in ``queued``
    state and dispatch the (LLM + render + upload) pipeline — to the worker
    queue, or to a background task where there is no queue — which claims it by
    moving it to ``tailoring``. Approval is the *entry event* into the
    application state machine — the job's own ``pending``/``approved`` decision
    is a separate fact on a separate document, which is why it isn't in the
    transition table.
    Idempotent — an existing Application is left untouched.

    Reverting (``pending``) puts the job back in the review queue; an
    application the agent hasn't submitted yet is discarded so the pipeline
    view matches. A submitted/responded application is history and stays.

    Skipping (``rejected``) schedules a posting-liveness check in the
    background — a posting that has already died is dismissed rather than
    shelved. (Approvals get the same check at the top of tailoring.)
    """
    user_ref = _client().collection("users").document(user_id)
    try:
        user_ref.collection("jobs").document(job_id).update(
            {"user_decision": body.decision}
        )
    except NotFound:
        raise HTTPException(status_code=404, detail="job not found") from None
    log.info("job.decided", job_id=job_id, decision=body.decision)

    if body.decision == "pending":
        app_ref = user_ref.collection("applications").document(application_id(job_id))
        snap = app_ref.get()
        if snap.exists and snap.to_dict().get("status") not in (
            "submitting",
            "submitted",
            "responded",
        ):
            app_ref.delete()
            log.info("application.discarded", job_id=job_id, reason="decision_reverted")

    if body.decision == "rejected":
        background_tasks.add_task(dismiss_skipped_if_posting_removed, user_id, job_id)

    if body.decision == "approved":
        app_ref = user_ref.collection("applications").document(application_id(job_id))
        if not app_ref.get().exists:
            app_ref.set(
                {
                    "id": application_id(job_id),
                    "user_id": user_id,
                    "job_id": job_id,
                    **app_state.creation_fields(),
                }
            )
            # Dispatch after the Application document exists, never before: a
            # worker can pick the task up before this request's next line runs,
            # and run_tailoring's claim needs something to claim.
            queued = dispatch_tailor(user_id, job_id, background_tasks=background_tasks)
            log.info("job.tailoring_scheduled", job_id=job_id, deduped=not queued)

    return {"ok": True}
