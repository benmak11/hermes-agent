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
from obs.logging import current_request_id, get_logger, run_context
from tools import decisions, exposures, queues, spend
from tools.applications import state as app_state
from tools.ats.validate import check_posting
from tools.matching.score import score_pending_jobs
from tools.run_costs import DONE, FAILED, RUNNING, open_run, persist_run_cost
from tools.spend.estimate import Estimate

router = APIRouter(tags=["jobs"])
log = get_logger("api.jobs")

#: The spend seam for the one paid route in this module, built once at import
#: rather than inline in the signature (B008).
SCORE_CONSENT = Depends(required(spend.SCORE_BACKLOG))

#: Where a confirmed backlog score goes on the queue. Not ``/tasks/score``:
#: that handler is online-only, and this intent must reach
#: ``batch_runs.score_or_start_run``, which sends a backlog over
#: ``BATCH_MIN_PENDING`` to a half-price batch. The online scorer would double
#: the cost and put every quote's batch-rate floor out of reach.
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
    """Return scored, still-pending jobs above ``min_score`` (default 60),
    ranked high to low.

    Also reports how many pending jobs exist before each filter, so the client
    can tell apart three states that otherwise all render as an empty list:
    nothing discovered, discovered but unscored, and scored but all below the
    threshold. Only the last is fixed by lowering the threshold. Both counts
    are tallies of a stream this route was already making, not extra queries.

    Jobs carrying ``exploration`` (``tools.matching.score.should_explore``) are
    returned alongside the threshold survivors even though they score under it,
    and the flag is stripped unconditionally from the response: a sampled job
    must be indistinguishable from a normally-surfaced one, or the badge biases
    the very decision the sample exists to collect. The record below keeps the
    flag, since it is server-side.

    Under ``LOG_EXPOSURES`` (default off) the returned list is also written to
    ``users/{uid}/exposures`` — **one document per response, including every 3s
    poll**, so the collection grows with tab-open time. ``rank`` is 0-based and
    is the position in the list actually returned, stamped after the sort;
    ``min_score`` is recorded because it defined the choice set and cannot be
    recovered from the items. The write is a background task, so the request id
    is read here while the middleware's contextvar is still bound.

    The response is identical with the flag on or off.
    """
    user_ref = _client().collection("users").document(user_id)
    snaps = (
        user_ref.collection("jobs")
        .where(filter=FieldFilter("user_decision", "==", "pending"))
        .stream()
    )
    log_exposures = exposures.enabled()
    explored_ids: set[str] = set()
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
        explored = bool(d.pop("exploration", False))
        if match.get("overall_score", 0) < min_score and not explored:
            continue
        if log_exposures and explored:
            explored_ids.add(snap.id)
        jobs.append({"id": snap.id, **d})
    jobs.sort(key=lambda j: j["match"]["overall_score"], reverse=True)
    if log_exposures:
        background_tasks.add_task(
            exposures.log_exposure,
            user_ref,
            items=[
                exposures.ExposureItem(
                    job_id=job["id"],
                    rank=rank,
                    overall_score=job["match"].get("overall_score"),
                    exploration=job["id"] in explored_ids,
                )
                for rank, job in enumerate(jobs)
            ],
            min_score=min_score,
            request_id=current_request_id(),
        )
    # Opportunistic scheduler tick (throttled in-process): opening the review
    # queue runs any due auto-discovery/sweep loop without external cron infra.
    #
    # Registered after the exposure write: Starlette runs background tasks in
    # order and abandons the rest when one raises, and ``tick_user`` can raise,
    # so with the tick first every tick failure dropped the exposure — a loss
    # correlated with discovery trouble rather than random.
    background_tasks.add_task(tick_user, user_id)
    return {
        "jobs": jobs,
        "pending_total": pending_total,
        "scored_total": scored_total,
    }


async def run_score_backlog(user_id: str) -> None:
    """Score this user's pending backlog, in-process, under its own run id.
    Spends real money on Gemini.

    The no-queue counterpart of ``/tasks/score``, same shape: a ``run_context``
    so the spend accumulates under a ``run_id`` and job docs land with a real
    ``scored_run_id``, and a ledger flush in a ``finally`` so a run that dies
    after paying still records what it paid.

    ``cycle_id=None``, as the worker task passes: this draws down the window
    the last discovery cycle opened rather than opening a fresh one, so "200
    per cycle" cannot be reset by clicking the button again.
    """
    with run_context("score_backlog", user_id=user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        counts: dict = {}
        # ``running`` until a leg below decides: a task killed mid-scoring banks
        # ``running`` and the activity contract ages it into ``stalled``.
        ledger_state = RUNNING
        await open_run(
            firestore.AsyncClient,
            user_id,
            run_id,
            runner="score_backlog",
            trigger="manual",
            started_at=started_at,
        )
        try:
            counts = await score_pending_jobs(user_id, cycle_id=None)
            ledger_state = DONE
        except Exception:
            # Re-raised untouched; this clause exists only to record the
            # outcome on the ledger before the ``finally`` closes it.
            ledger_state = FAILED
            raise
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                user_id,
                run_id,
                runner="score_backlog",
                trigger="manual",
                state=ledger_state,
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
    """The second, priced click: score the jobs discovery already found. Spends
    real money on Gemini.

    Always the paid verb, so it carries the consent seam as a hard dependency:
    no valid ``confirm`` token, no work, 402 with a fresh quote and token.

    The seam grants nothing. ``score_pending_jobs`` takes its own reservation
    from ``tools.matching.budget``, so the per-cycle and per-day caps still
    bound what a yes can cost; the estimate is a reading of that grant.

    Refused from a local process, as a route-level dependency rather than a
    check in this body so it runs before the seam consumes the single-use
    token — a 403 must not burn a confirmation.
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

    ``exploration`` is stripped here too: these shelves offer restore and
    approve, so a decision on them is not final and a marker would bias the
    re-decision exactly as it would in the queue.
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
        d.pop("exploration", None)
        jobs.append({"id": snap.id, **d})
    jobs.sort(key=lambda j: j["match"]["overall_score"], reverse=True)
    return {"jobs": jobs}


async def dismiss_skipped_if_posting_removed(user_id: str, job_id: str) -> None:
    """Background task: validate a freshly skipped job's posting.

    A skipped job sits on the Skipped shelf offering restore/approve; if the
    posting has died there is nothing left to act on, so dismiss it. Fail-open:
    only a definitive removal dismisses.
    """
    task_log = log.bind(user_id=user_id, job_id=job_id, task="skip_validation")
    user_ref = _client().collection("users").document(user_id)
    job_ref = user_ref.collection("jobs").document(job_id)
    snap = job_ref.get()
    if not snap.exists:
        return
    job = Job.model_validate(snap.to_dict())
    if await check_posting(job) != "removed":
        return
    # The user may have restored or approved the job while we probed, so only
    # dismiss a still-skipped one. This re-read, not the one above, is the
    # decision's "before" state.
    current = job_ref.get().to_dict() or {}
    if current.get("user_decision") != "rejected":
        task_log.info("job.posting_removed_but_redecided", url=job.url)
        return
    job_ref.update(
        {
            "user_decision": "dismissed",
            "posting_removed_at": datetime.now(UTC).isoformat(),
        }
    )
    task_log.info("job.posting_removed", url=job.url)
    # Only the write that actually changed `user_decision` is an event; the
    # early returns above decided nothing.
    decisions.log_decision(
        user_ref,
        job_id=job_id,
        decision="dismissed",
        previous_decision=current.get("user_decision"),
        job_doc=current,
        actor="system",
    )


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
    moving it to ``tailoring``. Approval is the entry event into the
    application state machine, which is why it is not in the transition table.
    Idempotent: an existing Application is left untouched.

    Reverting (``pending``) puts the job back in the review queue and discards
    an application the agent hasn't submitted yet; a submitted/responded one is
    history and stays. Skipping (``rejected``) schedules a background
    posting-liveness check, so a dead posting is dismissed rather than shelved.

    Every decision appends one event to ``users/{uid}/decisions``, because the
    job document only holds the latest answer and an undo would erase the
    original choice. The pre-read is wrapped, so a transient Firestore error
    degrades the event (``previous_decision: None``) rather than failing the
    decision. Under ``LOG_EXPOSURES`` the event carries ``shown_at`` only when
    the latest exposure actually contained this job — the shelves POST here too
    and never fetch ``/jobs/pending`` — and is ``None`` otherwise, never ``now``.
    """
    user_ref = _client().collection("users").document(user_id)
    job_ref = user_ref.collection("jobs").document(job_id)
    # Read before the update: this is the only place the decision being
    # replaced, and the score the user was looking at, still exist. A missing
    # document is left to the update below to report, so there is one source of
    # truth for "does this job exist?".
    #
    # Wrapped, because this read exists only to write a label: a failed read
    # must not fail the decision and leave the job un-approved with nothing
    # dispatched. An event with ``previous_decision: None`` is the accepted
    # degradation.
    try:
        before = job_ref.get()
        before_doc = before.to_dict() if before.exists else None
    except Exception:
        log.exception("job.decision_preread_failed", job_id=job_id)
        before_doc = None
    try:
        job_ref.update({"user_decision": body.decision})
    except NotFound:
        raise HTTPException(status_code=404, detail="job not found") from None
    log.info("job.decided", job_id=job_id, decision=body.decision)
    # After the update, never before: a 404 raised above changed nothing, so
    # there is no decision to record.
    decisions.log_decision(
        user_ref,
        job_id=job_id,
        decision=body.decision,
        previous_decision=(before_doc or {}).get("user_decision"),
        job_doc=before_doc,
        shown_at=(
            exposures.latest_shown_at(user_ref, job_id) if exposures.enabled() else None
        ),
    )

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
