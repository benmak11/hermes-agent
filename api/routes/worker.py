# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Task handlers for the hermes-worker service.

Cloud Tasks pushes queued work here, and several of these handlers drive real
Gemini spend. The routes exist in every deployment of the shared image but are
enabled only where ``WORKER_MODE`` is on — on the public hermes-api service
they 404, so the only way in is the private worker service, where Cloud Run's
OIDC has already authenticated the caller.

Handlers run the work inline, not as background tasks, so the HTTP status
reflects the outcome and Cloud Tasks' retry policy applies to infrastructure
failures. Cycle functions swallow their own work-level exceptions by design: a
failed cycle waits for the next scheduler tick rather than hot-retrying paid
LLM calls.
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
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools.applications import state
from tools.matching import batch_runs
from tools.matching.score import score_pending_jobs
from tools.queues import worker_mode
from tools.run_costs import DONE, FAILED, RUNNING, open_run, persist_run_cost

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
    # Worker-only: no model behind a public route has this field. It exists so
    # the submission path can be driven end to end against a live posting for
    # $0 — see run_submission's dry_run.
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


@router.post("/discovery/scan")
async def task_discovery_scan(body: CycleTask) -> dict:
    """Find-only discovery cycle: fetch -> filter -> persist. No scoring.

    Its own route rather than a field on ``/tasks/discovery`` because Cloud
    Tasks delivers to whichever worker revision is live, which during a
    rollout is the old one: it would drop an unknown ``{"score": false}`` and
    score the backlog anyway, failing the guard open on every deploy. An old
    worker 404s this path instead and the retry lands once the new revision is
    serving. See ``api.routes.discovery.SCAN_TASK_PATH``.
    """
    _require_worker()
    await run_discovery_cycle(body.user_id, trigger=body.trigger, score=False)
    return {"ok": True}


@router.post("/sweep")
async def task_sweep(body: CycleTask) -> dict:
    """Liveness sweep: dismiss postings the ATS took down."""
    _require_worker()
    await run_sweep_cycle(body.user_id, trigger=body.trigger)
    return {"ok": True}


@router.post("/score")
async def task_score(body: ScoreTask) -> dict:
    """Standalone scoring of pending jobs (online mode). Spends real money.

    Wrapped in a ``run_context`` because without a ``run_id`` this handler's
    Pro calls never reach the ledger and its job docs land with a null
    ``scored_run_id``. That id is for measurement only: ``cycle_id=None`` keeps
    this task drawing down the window discovery opened rather than opening a
    fresh one, so the per-cycle cap cannot be reset on demand. Once a window
    is spent an ad-hoc score task gets nothing until the next discovery cycle
    or UTC midnight, whichever comes first.
    """
    _require_worker()
    with run_context("score_task", user_id=body.user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        counts: dict = {}
        # ``running`` until a leg decides: a task Cloud Run kills mid-scoring
        # banks ``running`` and the activity contract ages it into ``stalled``.
        ledger_state = RUNNING
        await open_run(
            firestore.AsyncClient,
            body.user_id,
            run_id,
            runner="score_task",
            trigger="task",
            started_at=started_at,
        )
        agent_started = log_agent_start(log, "scoring", user_id=body.user_id)
        try:
            counts = await score_pending_jobs(
                body.user_id, limit=body.limit, cycle_id=None
            )
            ledger_state = DONE
            log_agent_end(
                log,
                "scoring",
                agent_started,
                outcome="completed",
                scored=counts.get("scored"),
                discarded=counts.get("discarded"),
                failed=counts.get("failed"),
            )
        except Exception:
            ledger_state = FAILED
            log_agent_end(log, "scoring", agent_started, outcome="failed")
            raise
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                body.user_id,
                run_id,
                runner="score_task",
                state=ledger_state,
                jobs={
                    "pending": counts.get("pending", 0),
                    "scored": counts.get("scored", 0),
                    "discarded": counts.get("discarded", 0),
                    "failed": counts.get("failed", 0),
                },
            )
    return {"ok": True, **counts}


@router.post("/score/backlog")
async def task_score_backlog(body: ScoreTask) -> dict:
    """The user's "score what you found" click, executed. Spends real money.

    Goes through ``score_or_start_run`` rather than the online scorer, which is
    why this route exists instead of reusing ``/tasks/score``: that seam routes
    a backlog over ``BATCH_MIN_PENDING`` to a half-price Vertex batch, and the
    online scorer would double the price and put every quote's batch-rate floor
    out of reach.

    ``cycle_id=None`` for the reason ``/tasks/score`` gives: measure under this
    run, spend out of the window discovery opened. Its own path for the same
    rollout-skew reason as ``/tasks/discovery/scan``.
    """
    _require_worker()
    with run_context("score_backlog", user_id=body.user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        counts: dict = {}
        ledger_state = RUNNING
        await open_run(
            firestore.AsyncClient,
            body.user_id,
            run_id,
            runner="score_backlog",
            trigger="manual",
            started_at=started_at,
        )
        agent_started = log_agent_start(log, "score_backlog", user_id=body.user_id)
        try:
            counts = await batch_runs.score_or_start_run(body.user_id, cycle_id=None)
            # A backlog that went to a Vertex batch is not done: the worker's
            # resume tick closes this doc when it ingests the results, under
            # this same run_id.
            ledger_state = RUNNING if counts.get("batch_run") else DONE
            log_agent_end(
                log,
                "score_backlog",
                agent_started,
                outcome="completed",
                scored=counts.get("scored"),
                discarded=counts.get("discarded"),
                failed=counts.get("failed"),
                batch_run=counts.get("batch_run"),
            )
        except Exception:
            ledger_state = FAILED
            log_agent_end(log, "score_backlog", agent_started, outcome="failed")
            raise
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                body.user_id,
                run_id,
                runner="score_backlog",
                trigger="manual",
                state=ledger_state,
                batch_run=counts.get("batch_run"),
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
    """Submit a resumable batch run; resume ticks ingest results. Commits real
    Gemini spend that is priced later, when a resume pass ingests it.

    The ``run_id`` is what the run doc records as ``origin_run_id``, so those
    tokens land on this task's ledger doc rather than scattering across
    whichever worker ticks ingested them. ``cycle_id=None`` as in
    ``/tasks/score``: measure under this run, spend out of the open window.
    """
    _require_worker()
    with run_context("batch_start", user_id=body.user_id) as run_id:
        started_at = datetime.now(UTC).isoformat()
        result: dict = {}
        ledger_state = RUNNING
        await open_run(
            firestore.AsyncClient,
            body.user_id,
            run_id,
            runner="batch_start",
            trigger="task",
            started_at=started_at,
        )
        agent_started = log_agent_start(log, "batch_start", user_id=body.user_id)
        try:
            result = await batch_runs.start(
                body.user_id, limit=body.limit, cycle_id=None
            )
            # Submitting a batch is the beginning of the work, not the end:
            # the resume tick that ingests it closes this doc.
            ledger_state = RUNNING if result.get("run") else DONE
            log_agent_end(
                log,
                "batch_start",
                agent_started,
                outcome="completed",
                batch_run=result.get("run"),
                stage=result.get("stage"),
                pending=result.get("pending"),
            )
        except Exception:
            ledger_state = FAILED
            log_agent_end(log, "batch_start", agent_started, outcome="failed")
            raise
        finally:
            await persist_run_cost(
                firestore.AsyncClient,
                body.user_id,
                run_id,
                runner="batch_start",
                state=ledger_state,
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
# The application funnel. Callers are ``applications.dispatch_tailor`` and
# ``dispatch_apply``.
# --------------------------------------------------------------------------


@router.post("/tailor")
async def task_tailor(body: TailorTask) -> dict:
    """Tailor an approved job — the work ``jobs.decide`` schedules on approval.
    Spends real money on Gemini.

    Takes no claim on purpose: ``run_tailoring`` CAS-es the Application
    ``queued → tailoring`` before it spends an LLM run, so the claim happens
    exactly once and a second one here could claim a state the callee then
    refuses to re-claim, dropping the work. That single claim is also what
    makes a redelivered task safe. No ``run_context`` either — the callee opens
    its own and flushes its own ledger doc.
    """
    _require_worker()
    await run_tailoring(body.user_id, body.job_id)
    return {"ok": True}


@router.post("/apply")
async def task_apply(body: ApplyTask) -> dict:
    """Submit an application the API already claimed — or rehearse one for $0.
    A non-dry run sends a real application to a real employer.

    Takes no claim of its own. ``POST /applications/{id}/submit`` already
    compare-and-swapped ``ready_for_review → submitting`` (the double-click
    guard, which stays on the request because it is what turns the losing click
    into a 409), and the lease that answers "is a process running this right
    now" is taken inside ``run_submission`` so every path that can drive a
    submission takes the same one. ``ran`` is whether the callee got it.

    The ``hermes-apply`` queue is provisioned with ``max_attempts = 1``, so an
    apply task is never redelivered today; the lease is what keeps this correct
    if that is raised.

    The ``dry_run`` status check below is a courtesy, not a safety property:
    what makes a rehearsal safe is that the one write it can reach
    (``posting_removed``) carries its own ``allowed_from`` inside the swap.

    Failures are the callee's to record, so this answers 200 for everything
    short of an infrastructure fault — which is what stops Cloud Tasks from
    re-driving a browser at a posting that already rejected us.
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
    # delivery, or a document that moved on. 200, not 4xx or 5xx: a retry would
    # only ask the same question again.
    ran = await run_submission(body.user_id, body.app_id)
    return {"ok": True, "ran": ran}
