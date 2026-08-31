# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Application endpoints: tailoring lifecycle + the diff/review surface.

The approval hook in ``jobs.decide`` creates an Application in ``queued`` state
and dispatches ``run_tailoring``, which claims the work by moving it to
``tailoring``; these endpoints let the web app poll, edit the objective,
regenerate, and hand off to submission.

**Where that work runs is decided by :func:`dispatch_tailor` and
:func:`dispatch_apply`.** With ``QUEUE_MODE`` on they enqueue a named Cloud
Tasks task to hermes-worker; without it they fall back to a FastAPI background
task on this instance, which is what keeps local dev and the pre-worker
deployment working. Both are called **after** the document that describes the
work has been committed, never before: a task can be picked up by a worker
before the caller's next line executes, and one that arrives ahead of its own
write reads the old document and does nothing at all.

Every ``status`` write in here goes through ``tools.applications.state`` — the
compare-and-swap that stops a double-click on Submit from starting two live ATS
submissions. Nothing in this module writes a status field directly.

Firestore round trips made from the ``async def``s in here go through
``asyncio.to_thread``. **The deployment that needs it is hermes-api, not the
worker:** hermes-worker runs at ``containerConcurrency = 1``, so a task has that
event loop to itself, but with QUEUE_MODE off ``run_tailoring`` and
``run_submission`` are background tasks on the loop that is serving every other
request — including the SSE stream polling this very document.

Two things are deliberately left blocking, and the docstring says so rather than
implying the coroutines are clean. ``progress()`` below is invoked by the
submitter through a *synchronous* callback, so it has no await to hand the work
back on; and the GCS transfers (``download_resume``, ``upload_screenshot``) are
blocking too. Both are known, neither is a Firestore round trip, and converting
them is a separate change.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from google.cloud import firestore
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api.deps import verify_user, verify_user_query
from models.job import Job
from models.profile import MasterProfile
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import queues
from tools.applications import reaper, state
from tools.ats.validate import check_posting
from tools.run_costs import persist_run_cost
from tools.submitters import SUBMIT_CLICKED
from tools.submitters.router import submit_application
from tools.submitters.storage import download_resume, upload_screenshot
from tools.tailoring.pipeline import application_id, tailor_application

log = get_logger("api.applications")

# Statuses from which a fresh submission is allowed (failed permits a retry).
# Derived from the state machine so the two can't drift — the enforcement is the
# compare-and-swap in submit(), not this set.
SUBMITTABLE = {s for s, nxt in state.TRANSITIONS.items() if "submitting" in nxt}
#: Prefix on every timeline note a rehearsal writes. ``web/`` renders notes
#: verbatim, so this is the only thing separating "we walked the form for free"
#: from "we applied to this job" in the user's own record of what happened.
DRY_RUN_NOTE = "[dry run] "
# Where the SSE stream stops polling. Wider than state.TERMINAL_STATUSES on
# purpose: submitted/failed end *this* submission even though the lifecycle can
# still move on from them.
TERMINAL = {"submitted", "responded", "posting_removed"}

router = APIRouter(tags=["applications"])

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _apps(user_id: str):
    return _client().collection("users").document(user_id).collection("applications")


def application_ref(user_id: str, app_id: str):
    """The Application document reference.

    Public because ``api/routes/worker.py`` needs the *same* reference this
    module writes through: its ``/tasks/apply`` handler claims the delivery on
    the document that ``submit()`` already moved to ``submitting``.
    """
    return _apps(user_id).document(app_id)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _transition(ref, to: str, **kwargs) -> bool:
    """``state.try_transition`` on a fresh read, off the event loop.

    The read and the swap it is conditioned on go into the *same* thread hop on
    purpose: they are one compare-and-swap, and an await between them would only
    widen the window the update-time precondition exists to close.
    """
    return await asyncio.to_thread(
        lambda: state.try_transition(ref, ref.get(), to, **kwargs)
    )


async def _dismiss_if_posting_removed(
    user_ref, app_ref, job: Job, task_log, *, allowed_from: Collection[str]
) -> bool:
    """Verify the posting is still live before spending work on it.

    Only a definitive "gone" from the ATS dismisses (check_posting fails open on
    transient errors). On removal: the job drops out of the queue/shelves via
    ``user_decision: dismissed`` and the application flips to the terminal
    ``posting_removed`` status, which is the user-facing notification on the
    tracking page. Returns True when the caller should stop.

    ``allowed_from`` is **required, and each caller passes the status it owns.**
    ``check_posting`` is a network round trip, so every caller here holds a read
    that is already stale by the time the write goes out, and
    ``submitting → posting_removed`` is a legal edge. Without the precondition
    *inside* the swap, a rehearsal (or a tailoring run) that started on a
    ``ready_for_review`` document could return from that round trip, find the
    user had meanwhile clicked Submit, and park the document terminally —
    clearing the lease out from under a live browser and destroying the
    confirmation evidence for an application that really was sent. Same failure
    the liveness sweep documents in ``state.try_transition``; filtering before
    the swap is not a compare-and-swap.
    """
    if await check_posting(job) != "removed":
        return False
    task_log.warning("application.posting_removed", url=job.url)
    await asyncio.to_thread(
        user_ref.collection("jobs").document(job.id).update,
        {"user_decision": "dismissed", "posting_removed_at": _now()},
    )
    # The caller stops either way — the posting really is gone. A refused
    # transition means the document moved out from under us: either someone
    # already parked it somewhere terminal, or it is now owned by a run this
    # caller must not interrupt.
    if not await _transition(
        app_ref,
        "posting_removed",
        note=f"posting no longer available at {job.url} — application dismissed",
        lease=state.CLEAR_LEASE,
        allowed_from=allowed_from,
    ):
        task_log.info("application.posting_removed_not_recorded")
    return True


async def run_tailoring(user_id: str, job_id: str) -> None:
    """Background task: tailor an approved job and persist the Application.

    Claims the ``queued`` Application by moving it to ``tailoring`` — a claim
    that loses (a second task, or a status that has since moved on) returns
    without spending an LLM run. Then reads the profile + job, runs the
    tailoring pipeline, merges the result onto the doc and transitions it to
    ``ready_for_review``. On failure the doc is flipped to ``failed`` with a
    timeline note so the UI can surface it.
    """
    # Background tasks run after the response, outside the request context, so
    # bind the ids onto this logger explicitly to keep the trail intact. The
    # run_context adds a run_id that the tailoring pipeline's own log lines
    # (tools.tailoring) inherit via contextvars.
    task_log = log.bind(user_id=user_id, job_id=job_id, task="tailoring")
    db = _client()
    user_ref = db.collection("users").document(user_id)
    app_ref = user_ref.collection("applications").document(application_id(job_id))
    with run_context("tailoring", user_id=user_id, job_id=job_id) as run_id:
        started_at = _now()
        started = log_agent_start(task_log, "tailoring")
        try:
            # Claim before any paid work. Two schedulings of this task (approve
            # then regenerate, or a retry) can't both spend an LLM run on the
            # same job, and a doc the undo path deleted is never resurrected.
            #
            # Status and lease in **one** write. The status is what stops the
            # double spend; the lease is what stops a worker killed mid-run from
            # stranding the document forever. Without it the tailor queue's
            # retry arrives, correctly refuses the now-illegal queued → tailoring
            # edge, and the work is silently dropped — the user's only way out
            # being the Regenerate button, on a page that gives no sign anything
            # is wrong. Every exit below hands the lease back.
            if not await _transition(
                app_ref,
                "tailoring",
                lease=state.lease_for("tailoring", owner=state.new_owner()),
            ):
                task_log.info("tailoring.not_claimed")
                log_agent_end(task_log, "tailoring", started, outcome="not_claimed")
                return

            profile = MasterProfile.model_validate(
                (await asyncio.to_thread(user_ref.get)).to_dict()
            )
            job_ref = user_ref.collection("jobs").document(job_id)
            job_doc = await asyncio.to_thread(job_ref.get)
            if not job_doc.exists:
                raise ValueError(f"Job {job_id} not found")
            job = Job.model_validate(job_doc.to_dict())
            task_log = task_log.bind(company=job.company, title=job.title)

            # The posting may have died between discovery and approval — don't
            # spend an LLM run tailoring for a page that no longer exists.
            # allowed_from is the status this run owns: if the document has left
            # ``tailoring`` while check_posting was on the wire, it is no longer
            # ours to park.
            if await _dismiss_if_posting_removed(
                user_ref, app_ref, job, task_log, allowed_from={"tailoring"}
            ):
                log_agent_end(task_log, "tailoring", started, outcome="posting_removed")
                return

            app = await tailor_application(job, profile, upload=True)

            # The user may have reverted the approval (undo) while tailoring
            # ran — don't resurrect an application decide() already discarded.
            decision = ((await asyncio.to_thread(job_ref.get)).to_dict() or {}).get(
                "user_decision"
            )
            if decision != "approved":
                # The one exit that leaves the lease behind, deliberately: the
                # undo path has usually deleted this document already, and
                # writing to one that survived would only resurrect state the
                # user asked us to drop. It expires on the IN_PROGRESS clock.
                task_log.info("tailoring.discarded", decision=decision)
                log_agent_end(
                    task_log,
                    "tailoring",
                    started,
                    outcome="discarded",
                    decision=decision,
                )
                return

            # Content only, merged. The old blanket set() of the whole model
            # replaced the document and took the timeline with it; status and
            # timeline belong to the state machine, so they're stripped here and
            # written by the transition below.
            content = app.model_dump(mode="json")
            for field in state.OWNED_FIELDS:
                content.pop(field, None)
            # update(), not set(merge=True): a doc the undo path deleted between
            # the decision check above and here must not come back.
            #
            # It also only writes the fields the model carries, which is what
            # keeps ``submit_attempts`` alive across a regenerate. That counter
            # names the apply task (see dispatch_apply): resetting it here would
            # let a later submission reuse a task name the queue still holds a
            # tombstone for, and the submission would be deduped into silence.
            await asyncio.to_thread(app_ref.update, content)
            # CLEAR_LEASE: the run is over. A tailoring lease left on a
            # ready_for_review document would still be live when the user clicks
            # Submit, and the worker's own claim on ``submitting`` would find it
            # held and refuse — a submission silently dropped by a lease nobody
            # owns any more.
            #
            # ``reap_attempts`` is cleared **here**, in the same write, because
            # this is the event that proves the pipeline works for this
            # document. The reaper's cap counts *consecutive* failed recoveries;
            # without an epoch it counts them for the lifetime of the
            # application, and a doc recovered three times during a queue outage
            # and then tailored perfectly stays permanently one stale tick away
            # from ``give_up`` — a give_up that dispatches nothing while telling
            # the user to press Regenerate, the one thing that cannot help.
            # Riding the swap rather than the content write above matters: the
            # content write has no precondition, so a reset there could land on
            # a document a regenerate had already moved on.
            #
            # ``allowed_from`` is the status this run claimed, for the same
            # reason every other write in this file carries one: the tailoring
            # pipeline above is minutes of network, so this swap is decided on a
            # read that is long stale, and ``tailoring → ready_for_review`` is
            # not the only way into this document. The reaper deliberately
            # manufactures overlapping runs — it requeues a document whose lease
            # lapsed, and run B claims it — so without this, run A finishing
            # late would publish *its* result over run B's live ``tailoring``
            # document and clear B's lease with it.
            if not await _transition(
                app_ref,
                "ready_for_review",
                lease=state.CLEAR_LEASE,
                extra={reaper.ATTEMPTS_FIELD: firestore.DELETE_FIELD},
                allowed_from={"tailoring"},
            ):
                task_log.info("tailoring.result_not_published")
            task_log.info("tailoring.done", resume_uri=app.resume_variant_uri)
            log_agent_end(
                task_log,
                "tailoring",
                started,
                outcome="completed",
                resume_uri=app.resume_variant_uri,
            )
        except Exception as e:  # persist failure for the UI, surface in timeline
            task_log.exception("tailoring.failed")
            if not (await asyncio.to_thread(app_ref.get)).exists:
                log_agent_end(task_log, "tailoring", started, outcome="discarded")
                return  # discarded by a revert while we ran — don't resurrect
            # Same precondition as the publish above, and the case it guards is
            # the sharper of the two: a zombie run erroring *after* the reaper
            # requeued this document and run B claimed it would otherwise mark
            # B's live ``tailoring`` document ``failed`` and clear B's lease —
            # failing a run that is working, on the strength of an exception
            # raised by a run nobody is waiting for.
            #
            # It also narrows the one case this block used to cover loosely: an
            # exception thrown *by the claim itself* now leaves the document in
            # ``queued`` rather than flipping it to ``failed``. That is the
            # better answer — ``queued`` is exactly what the reaper re-dispatches
            # — and this run never owned the document to begin with.
            await _transition(
                app_ref,
                "failed",
                note=str(e)[:300],
                lease=state.CLEAR_LEASE,
                allowed_from={"tailoring"},
            )
            log_agent_end(
                task_log, "tailoring", started, outcome="failed", error=str(e)[:300]
            )
        finally:
            # Tailoring is the second-most expensive per-user action, and it
            # binds a run_id — so without this flush its spend accumulates in
            # the API process and is never banked or released.
            await persist_run_cost(
                _client,
                user_id,
                run_id,
                runner="tailoring",
                job_id=job_id,
                started_at=started_at,
            )


# --------------------------------------------------------------------------
# Where the work runs. dispatch_tailor and dispatch_apply (further down, next
# to run_submission) mirror ``discovery.dispatch_cycle``: a named Cloud Tasks
# task to hermes-worker under QUEUE_MODE, a background task on this instance
# without one, so local dev and the pre-worker deployment keep working.
#
# **Synchronous, unlike dispatch_cycle**, and deliberately so: every caller is
# a synchronous route. Enqueueing is one blocking RPC, which FastAPI already
# runs in a threadpool for those routes; making these ``async`` would drag
# ``decide``/``regenerate``/``submit`` — and the blocking Firestore reads and
# compare-and-swaps they are built out of — onto the event loop instead, which
# is the arrangement ``tools.applications.state`` documents as the thing to
# avoid. Neither helper awaits anything, so there is nothing to gain from it.
# --------------------------------------------------------------------------


def dispatch_tailor(
    user_id: str, job_id: str, *, background_tasks: BackgroundTasks | None = None
) -> bool:
    """Tailor an approved job — on the worker via queue when enabled.

    ``background_tasks`` may be ``None`` for a caller that has no request to
    defer work onto — ``cli.reap_applications``. That makes the helper
    queue-only, and it returns ``False`` rather than pretending: with no queue
    and nowhere to run the work in-process, **nothing was scheduled**, and the
    reaper counts that as a re-dispatch that did not happen. The alternative
    (handing it a throwaway ``BackgroundTasks`` that is never awaited) would
    drop the work silently, which is the failure this whole PR exists to end.

    The task id is minute-granular, matching ``dispatch_cycle``'s ``manual``
    convention: a double-click on Approve or Regenerate dedupes at the queue,
    while a deliberate regenerate a minute later isn't blocked for the full
    hour a Cloud Tasks tombstone lives. Returns False when the queue deduped.

    The residual case that costs something is a regenerate issued in the *same*
    minute as the enqueue it collides with: the application has already been put
    back in ``queued`` by then, so it sits there with nothing running. That is
    precisely what ``regenerate``'s "already queued, so re-schedule rather than
    refuse" branch exists for — the next click builds a new id and picks the
    work back up — which is why this stays minute-granular rather than becoming
    a per-document counter like ``dispatch_apply``'s: a counter would advance
    only on the compare-and-swap, and re-scheduling from ``queued`` doesn't take
    one, so the escape hatch would dedupe for a solid hour instead of a minute.
    """
    if queues.enabled():
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
        return queues.enqueue(
            "tailor",
            "/tasks/tailor",
            {"user_id": user_id, "job_id": job_id},
            task_id=f"tailor-{user_id}-{job_id}-{stamp}",
        )
    if background_tasks is None:
        log.warning("tailor.not_dispatched", user_id=user_id, job_id=job_id)
        return False
    background_tasks.add_task(run_tailoring, user_id, job_id)
    return True


def _backfill_job_url(user_id: str, app: dict) -> dict:
    """Populate job_url from the job doc for applications created before it was
    denormalized. Persists once so the link works everywhere (list + review)."""
    if app.get("job_url") or not app.get("job_id"):
        return app
    job_snap = (
        _client()
        .collection("users")
        .document(user_id)
        .collection("jobs")
        .document(app["job_id"])
        .get()
    )
    url = job_snap.to_dict().get("url") if job_snap.exists else None
    if url:
        app["job_url"] = url
        _apps(user_id).document(app["id"]).update({"job_url": url})
    return app


@router.get("/applications")
def list_applications(user_id: str = Depends(verify_user)) -> dict:
    """All applications for the user, newest activity first."""
    apps = [_backfill_job_url(user_id, s.to_dict()) for s in _apps(user_id).stream()]
    apps.sort(key=lambda a: (a.get("timeline") or [{}])[-1].get("at", ""), reverse=True)
    return {"applications": apps}


@router.get("/applications/{app_id}")
def get_application(app_id: str, user_id: str = Depends(verify_user)) -> dict:
    snap = _apps(user_id).document(app_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    return _backfill_job_url(user_id, snap.to_dict())


@router.get("/applications/{app_id}/resume")
def download_resume_file(
    app_id: str, user_id: str = Depends(verify_user)
) -> FileResponse:
    """Download the tailored resume .docx (for applying manually)."""
    snap = _apps(user_id).document(app_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    uri = snap.to_dict().get("resume_variant_uri")
    if not uri:
        raise HTTPException(status_code=404, detail="no resume for this application")
    path = download_resume(uri)
    company = (snap.to_dict().get("job_company") or "company").replace(" ", "_")
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        filename=f"resume_{company}.docx",
    )


class ObjectiveUpdate(BaseModel):
    objective_text: str


@router.put("/applications/{app_id}/objective")
def update_objective(
    app_id: str, body: ObjectiveUpdate, user_id: str = Depends(verify_user)
) -> dict:
    """Inline-edit the generated objective from the review UI."""
    ref = _apps(user_id).document(app_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="application not found")
    ref.update({"objective_text": body.objective_text})
    log.info(
        "application.objective_updated",
        app_id=app_id,
        user_id=user_id,
        chars=len(body.objective_text),
    )
    return {"ok": True}


@router.post("/applications/{app_id}/regenerate")
def regenerate(
    app_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
) -> dict:
    """Re-run tailoring for this application's job (explicit user action).

    Puts the application back in ``queued``; ``run_tailoring`` claims it from
    there. Rejected with 409 where a regenerate makes no sense (mid-submission,
    submitted, posting removed) — the work must not be dispatched when the state
    change didn't happen, which is also why the dispatch is the *last* thing
    here: the worker can be reading this document before the next line of this
    function runs, so it has to find it already back in ``queued``.

    An application that is *already* queued is re-scheduled rather than
    refused: a dispatch that never arrived leaves the doc stuck there, and this
    is the user's only manual way out of it. Safe because ``run_tailoring``
    claims — a duplicate scheduling can't spend a second LLM run.
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    doc = snap.to_dict()
    job_id = doc["job_id"]
    # The second epoch for the reaper's cap: a user asking for this again is a
    # fresh start, so the automatic-recovery budget starts fresh too. Otherwise
    # a document that exhausted the cap gets exactly one manual retry before the
    # reaper starts failing it on sight — while the note it wrote says to press
    # this button. Inside the swap, so a regenerate that loses resets nothing.
    #
    # **The already-``queued`` branch below takes no swap**, so it gets no reset;
    # there is no precondition to attach one to and a bare write is how this
    # phase's bugs start. That document is already where the reaper would put it
    # and the click has already dispatched, so the residual is a cap that stays
    # spent until the next successful tailoring clears it.
    if doc.get("status") != state.INITIAL and not state.try_transition(
        ref,
        snap,
        state.INITIAL,
        note="regenerate",
        extra={reaper.ATTEMPTS_FIELD: firestore.DELETE_FIELD},
    ):
        raise HTTPException(
            status_code=409,
            detail=f"cannot regenerate from status '{doc.get('status')}'",
        )
    log.info("application.regenerate", app_id=app_id, job_id=job_id, user_id=user_id)
    if not dispatch_tailor(user_id, job_id, background_tasks=background_tasks):
        # Deduped: an enqueue with this id happened inside the last minute. The
        # doc is in ``queued``, so clicking Regenerate again is the way out.
        log.info("application.regenerate_deduped", app_id=app_id, job_id=job_id)
    return {"ok": True}


async def run_submission(user_id: str, app_id: str, *, dry_run: bool = False) -> bool:
    """Submit the application to the live ATS and record evidence.

    Downloads the tailored resume, runs the per-source submitter, uploads pre/post
    screenshots to GCS, and writes the terminal status (submitted/failed). Progress
    is appended to the timeline as it goes so the SSE stream can relay it live.

    **This function takes the delivery claim itself.** The lease used to be
    claimed by the ``/tasks/apply`` handler, which fenced worker against worker
    but nothing else: ``dispatch_apply`` still runs this as a background task
    wherever QUEUE_MODE is off, and during a rollout two hermes-api revisions
    serve the same URL and the same documents at once. A claim taken by only one
    of the paths that can drive a submission is not a lock on submissions. It
    lives here so **every** such path takes the same one, before any browser
    opens, and hands it back the same way.

    Returns True when a run actually happened. False means the document is gone
    or the claim was lost — a redelivered task, or a document that moved on —
    and the caller has nothing left to do; it is the answer ``/tasks/apply``
    reports as ``ran``.

    ``dry_run`` drives the whole path against the live posting but stops the
    submitter before it clicks Submit, so the browser automation can be
    exercised for $0. It is **keyword-only and worker-only** — nothing on the
    public API surface can set it (see ``api/routes/worker.ApplyTask``).

    A dry run writes **no status of its own**: ``submit_greenhouse`` reports one
    as ``success=True, dry_run=True``, and treating that as a real success would
    write ``submitted`` on a job nobody applied to. It writes timeline notes,
    marked as a rehearsal so the tracking page cannot read them as a submission
    in progress. The **one** status it can still reach is ``posting_removed``,
    via the pre-flight check below: the posting being gone is a fact about the
    world rather than about this run, and it is recorded under the same
    ``allowed_from`` guard as everything else here — only while the document is
    still where the rehearsal found it.

    A rehearsal takes **no lease**, because it makes no claim: it writes no
    status of its own, so there is nothing a repeat could corrupt, and repeating
    it costs nothing.
    """
    ref = _apps(user_id).document(app_id)
    snap = await asyncio.to_thread(ref.get)
    if not snap.exists:
        log.info("submission.missing", user_id=user_id, app_id=app_id)
        return False
    app = snap.to_dict()
    job_id = app["job_id"]
    # The status this run may act on, read once and used as the precondition for
    # every write below — a real submission owns ``submitting`` (the API claimed
    # it before scheduling), a rehearsal owns nothing and may only touch a
    # document still sitting where it found it.
    owned: Collection[str] = SUBMITTABLE if dry_run else {"submitting"}
    found_status = app.get("status")
    task_log = log.bind(
        user_id=user_id,
        app_id=app_id,
        job_id=job_id,
        task="submission",
        company=app.get("job_company"),
        title=app.get("job_title"),
    )
    user_ref = _client().collection("users").document(user_id)

    def progress(message: str, status: str) -> None:
        # Timeline only, with exactly one exception. The submitter's second
        # argument is a display label for the step ("Opening ...", "Attaching
        # resume"), not a lifecycle edge — it emits "submitted" the moment it
        # sees a confirmation page, and honouring that as a transition would
        # lock out the real terminal write below, which is the one carrying the
        # screenshots and confirmation.
        if dry_run:
            # A rehearsal's steps are the *same* steps, so unmarked they render
            # on the tracking page as a submission in progress — "Opening…",
            # "Attaching resume" — against a document nobody submitted. Keep the
            # entry's status where the document actually is and label the note.
            #
            # A rehearsal cannot reach SUBMIT_CLICKED — the submitter returns
            # before that line — but this branch comes first regardless, so no
            # future submitter can talk a $0 rehearsal into writing the marker
            # that says a browser clicked Submit.
            state.append_note(
                ref, found_status or "ready_for_review", DRY_RUN_NOTE + message
            )
            return
        if status == SUBMIT_CLICKED:
            # **The point of no return.** The submitter emits this immediately
            # before ``submit_btn.click()``, so from here on the application may
            # already be in the employer's ATS, and no automatic path may
            # resubmit it — ``tools.applications.reaper`` reads exactly this
            # field to decide that. The marker and the timeline entry go in one
            # write so the timeline can never claim the form was submitted while
            # the marker that prevents a retry is missing.
            #
            # The entry itself is recorded as "submitting", not as the token:
            # web/ renders a closed union of statuses and filters the submission
            # timeline on ["submitting", "submitted", "failed"], so the token
            # stays on the wire between submitter and caller.
            state.append_note(
                ref, "submitting", message, extra={"submit_attempted_at": _now()}
            )
            return
        state.append_note(ref, status, message)

    # run_id context so the submitter's own log lines (tools.submitters, the
    # Playwright steps) stitch to this submission in Cloud Logging.
    with run_context("submission", user_id=user_id, app_id=app_id, job_id=job_id):
        started = log_agent_start(task_log, "submission", source=app.get("job_source"))

        # The delivery claim, before any browser opens. The status can't take
        # it: ``POST /applications/{id}/submit`` already claimed the work by
        # swapping ``→ submitting`` (that is what a double-click loses on), so
        # by the time we get here the status *is* the claim and
        # ``submitting → submitting`` is illegal, exactly as it must be. The
        # lease answers the other question — **is a process running this right
        # now** — so a redelivered task, or a second runner during a revision
        # rollout, finds it live and does nothing.
        #
        # Taken here rather than a few lines earlier so that the ``try`` below
        # opens on the very next statement: everything between a claim and the
        # ``finally`` that hands it back is a region where an exception strands
        # the lease on the document for its full TTL.
        owner: str | None = None
        if not dry_run:
            owner = state.new_owner()
            if not await asyncio.to_thread(
                state.try_claim_lease, ref, snap, "submitting", owner=owner
            ):
                # A duplicate delivery, or a document that moved on. Nothing to
                # do, and nothing to record: asking again gets the same answer.
                task_log.info("submission.not_claimed", found_status=found_status)
                log_agent_end(task_log, "submission", started, outcome="not_claimed")
                return False
            task_log = task_log.bind(lease_owner=owner)
        try:
            if owner is not None:
                # Re-read now the claim has landed. ``try_claim_lease`` retries
                # against a *fresh* snapshot, so it can succeed on a document one
                # write newer than the one read at the top of this function —
                # and everything below acts on the content of that read.
                app = (await asyncio.to_thread(ref.get)).to_dict() or app
            resume_uri = app.get("resume_variant_uri")
            if not resume_uri:
                raise ValueError("No tailored resume to submit — run tailoring first.")
            profile = MasterProfile.model_validate(
                (await asyncio.to_thread(user_ref.get)).to_dict()
            )
            job_snap = await asyncio.to_thread(
                user_ref.collection("jobs").document(job_id).get
            )
            job = Job.model_validate(job_snap.to_dict())

            # Last-line check: never drive a browser at a posting the ATS says
            # is gone. Fail-open — a flaky board proceeds and fails visibly.
            if await _dismiss_if_posting_removed(
                user_ref, ref, job, task_log, allowed_from=owned
            ):
                log_agent_end(
                    task_log, "submission", started, outcome="posting_removed"
                )
                return True

            resume_path = download_resume(resume_uri)

            result = await submit_application(
                job,
                profile,
                resume_path,
                dry_run=dry_run,
                headless=True,
                on_progress=progress,
            )

            shots: list[dict] = []
            for key, name in (
                ("pre_submit_screenshot", "pre_submit.png"),
                ("confirmation_screenshot", "confirmation.png"),
            ):
                local = result.get(key)
                if local and os.path.exists(local):
                    shots.append(
                        {
                            "name": name,
                            "uri": upload_screenshot(
                                Path(local), user_id, job_id, name
                            ),
                        }
                    )

            task_log.info(
                "submission.result",
                success=bool(result.get("success")),
                error=result.get("error"),
                screenshots=len(shots),
            )
            log_agent_end(
                task_log,
                "submission",
                started,
                outcome=(
                    ("dry_run" if dry_run else "submitted")
                    if result.get("success")
                    else "failed"
                ),
                error=result.get("error"),
            )
            if dry_run:
                # The pre-submit screenshot is uploaded but not attached to the
                # document (``screenshots`` is submission evidence, and no
                # submission happened), so log where it landed — that upload is
                # the only artifact a rehearsal leaves behind.
                task_log.info(
                    "submission.dry_run", screenshot_uris=[s["uri"] for s in shots]
                )
                # No transition, in either direction. The document is still
                # wherever the rehearsal found it (a submittable status — the
                # handler checks), and there is no edge that would put it back
                # if a dry run moved it, so it moves it nowhere.
                await asyncio.to_thread(
                    state.append_note,
                    ref,
                    found_status or "ready_for_review",
                    DRY_RUN_NOTE
                    + (
                        "completed — stopped before Submit; nothing was submitted"
                        if result.get("success")
                        else f"failed: {result.get('error') or 'unknown'}"[:300]
                    ),
                )
            elif result.get("success"):
                confirm_uri = next(
                    (s["uri"] for s in shots if s["name"] == "confirmation.png"), None
                )
                submitted_at = _now()
                recorded = await _transition(
                    ref,
                    "submitted",
                    lease=state.CLEAR_LEASE,
                    extra={
                        "screenshots": shots,
                        "confirmation": {
                            "submitted_at": submitted_at,
                            "screenshot_uri": confirm_uri,
                        },
                    },
                )
                if not recorded:
                    # The application really went out but the document had
                    # already moved somewhere terminal, so the evidence in
                    # extra= was dropped. This is the loudest thing in the file
                    # on purpose: the URIs below are the only remaining record
                    # that this submission happened.
                    now_snap = await asyncio.to_thread(ref.get)
                    task_log.error(
                        "submission.result_not_recorded",
                        submitted_at=submitted_at,
                        confirmation_uri=confirm_uri,
                        screenshot_uris=[s["uri"] for s in shots],
                        status_now=(now_snap.to_dict() or {}).get(state.STATUS_FIELD),
                    )
            elif not await _transition(
                ref,
                "failed",
                note=(result.get("error") or "submission failed")[:300],
                lease=state.CLEAR_LEASE,
                extra={"screenshots": shots},
            ):
                task_log.warning(
                    "submission.failure_not_recorded",
                    screenshot_uris=[s["uri"] for s in shots],
                )
        except Exception as e:  # record failure for the UI
            task_log.exception("submission.failed")
            if dry_run:
                # Same rule as the success path: a dry run writes no status.
                # ``failed`` is a legal edge from a submittable status, so
                # without this guard a $0 rehearsal could mark a real
                # application failed.
                await asyncio.to_thread(
                    state.append_note,
                    ref,
                    found_status or "ready_for_review",
                    DRY_RUN_NOTE + f"errored: {e!s}"[:300],
                )
                log_agent_end(
                    task_log,
                    "submission",
                    started,
                    outcome="failed",
                    error=str(e)[:300],
                )
                return True
            if not await _transition(
                ref, "failed", note=str(e)[:300], lease=state.CLEAR_LEASE
            ):
                # Leaves the document wedged in ``submitting``; cli/unwedge_submitting
                # is the manual way out until the reaper lands.
                task_log.warning("submission.failure_not_recorded", error=str(e)[:300])
            log_agent_end(
                task_log, "submission", started, outcome="failed", error=str(e)[:300]
            )
        finally:
            # Hand the lease back — but only once the document has an outcome.
            #
            # Normally there is nothing to do: every terminal transition carries
            # CLEAR_LEASE, so the outcome and the release are one write. The case
            # this covers is that transition *losing* — it loses because the
            # document moved on, which leaves our lease sitting on someone else's
            # status for its full TTL.
            #
            # Still ``submitting`` is the opposite case and must NOT be released:
            # the run ended without recording an outcome, so whether the form was
            # actually submitted is unknown, and dropping the lease would invite a
            # redelivery straight back into the same document. Let it expire, which
            # is what cli/unwedge_submitting (and the reaper) exist to adjudicate.
            #
            # The two event names are the ones ``/tasks/apply`` emitted before the
            # claim moved in here, kept verbatim: the code moved, the operational
            # trail it leaves should not have to.
            if owner is not None:
                after = await asyncio.to_thread(ref.get)
                doc = after.to_dict() or {}
                if doc.get(state.STATUS_FIELD) == "submitting":
                    if state.lease_owner(doc) == owner:
                        task_log.warning("task.apply.finished_without_an_outcome")
                elif await asyncio.to_thread(state.release_lease, ref, after, owner):
                    task_log.warning("task.apply.lease_released_late")
    return True


def dispatch_apply(
    user_id: str, app_id: str, *, attempt: int, background_tasks: BackgroundTasks
) -> bool:
    """Submit an application the caller has **already** claimed.

    The task id carries ``attempt`` — the ``submit_attempts`` value written in
    the same compare-and-swap that took the ``submitting`` claim — rather than a
    timestamp. Same claim, same name, so the queue refuses a duplicate dispatch;
    a legitimate retry after a failure wins a *new* claim, advances the counter
    and gets a name of its own however soon it comes, which no time granularity
    can offer both of.

    ``attempt`` is passed in rather than re-read here on purpose: the name has to
    belong to *this* claim, and a fresh read can only ever return whatever the
    document says now.

    Returns False when the queue deduped, which after the above can only mean a
    name was reused — the caller must not report that as a scheduled submission.
    """
    if queues.enabled():
        return queues.enqueue(
            "apply",
            "/tasks/apply",
            {"user_id": user_id, "app_id": app_id},
            task_id=f"apply-{user_id}-{app_id}-{attempt}",
        )
    background_tasks.add_task(run_submission, user_id, app_id)
    return True


#: What the timeline says when a claim is rolled back because the work behind it
#: was never dispatched. User-facing: ``web/`` renders notes verbatim.
DISPATCH_FAILED_NOTE = "could not be scheduled — nothing was submitted. Try again."


def _abandon_unstarted_claim(ref, app_id: str) -> None:
    """Undo a ``submitting`` claim whose work was never dispatched.

    ``submitting`` is the one status a *user* cannot leave: Submit and
    Regenerate both 409 out of it and the undo path in ``jobs.decide`` refuses to
    delete a document in it, so a claim with nothing behind it wedges the
    application until an operator runs ``cli/unwedge_submitting`` — the reaper
    that would collect it is a later PR. Nothing was clicked (the lease is taken
    by the run, and no run started), so ``failed`` is both true and the one
    status the user can act on. Clicking Submit again then computes a *new*
    attempt number, which also breaks the task-name collision that is one of the
    two ways to get here.

    **The lease is claimed first, and that is the safety property rather than a
    formality.** An enqueue can report failure and still have created the task —
    a deadline that expires after the server committed — and ``AlreadyExists``
    means a task by that name exists, possibly one still pending. Either way a
    worker may be about to run, or already running, *this* document, and writing
    ``failed`` with CLEAR_LEASE underneath it would clear a live run's claim and
    throw away the confirmation evidence for an application that really was sent.
    :func:`state.try_claim_lease` answers exactly that question inside its own
    compare-and-swap — status *and* lease, re-checked on its retry — so losing it
    means "someone is running this", and the right response is to leave the
    document alone and let that run record its own outcome.
    """
    owner = state.new_owner()
    if not state.try_claim_lease(ref, ref.get(), "submitting", owner=owner):
        log.warning("application.submit_claim_left_alone", app_id=app_id)
        return
    if state.try_transition(
        ref,
        ref.get(),
        "failed",
        note=DISPATCH_FAILED_NOTE,
        lease=state.CLEAR_LEASE,
        allowed_from={"submitting"},
    ):
        log.info("application.submit_claim_rolled_back", app_id=app_id)
        return
    # The document moved on under us between the two writes. Hand back the lease
    # we just took, or it sits on someone else's status for its full TTL.
    state.release_lease(ref, ref.get(), owner)
    log.warning("application.submit_claim_not_rolled_back", app_id=app_id)


@router.post("/applications/{app_id}/submit")
def submit(
    app_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
) -> dict:
    """Submit the tailored application to the live ATS (explicit user action).

    Idempotency-locked by compare-and-swap: only a ``ready_for_review`` (or
    previously ``failed``) application may be submitted, and the check and the
    claim are the *same write*. Two clicks racing on two instances both read
    ``ready_for_review``, but only one write survives the update-time
    precondition; the loser re-reads, finds ``submitting``, and gets a 409. That
    is what keeps a duplicate real job application from going out.

    **Claim first, dispatch second, always in that order.** The claim is the
    thing that stops a second submission; the dispatch only says where the work
    runs. Enqueue first and the worker can be reading the document before this
    request writes it — it would find a ``ready_for_review`` application with no
    claim on it and refuse to run, which is the whole submission silently lost.
    In the other order the worst case is a claim with nothing behind it, and
    **nothing was ever clicked** — so that claim is rolled back to ``failed``
    here rather than left for a reaper that doesn't exist yet. See
    :func:`_abandon_unstarted_claim`: ``submitting`` is a status the user has no
    way out of, and the two ways a dispatch fails (a transient Cloud Tasks
    error, and a task name reused after the undo path deleted and re-created the
    document) are both reachable in an ordinary session.
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    doc = snap.to_dict() or {}
    status = doc.get(state.STATUS_FIELD)
    # Bumped *inside* the swap below, never outside it. The counter names the
    # apply task, so two claims sharing a number would share a task name and the
    # second would be deduped into silence; only a claim that wins advances it,
    # and every winning claim advances it exactly once. Computed from the same
    # snapshot the swap is conditioned on, so the write that carries it is the
    # write that proves nothing moved in between. (try_transition's one retry
    # re-reads and re-checks legality: anything that could have bumped this
    # counter also left the status somewhere the retry refuses.)
    attempt = int(doc.get("submit_attempts") or 0) + 1
    if not state.try_transition(
        ref,
        snap,
        "submitting",
        extra={
            "last_submitted_at": _now(),
            "submit_attempts": attempt,
            # **Per attempt, not per document.** ``submit_attempted_at`` means
            # "a browser clicked Submit *on this attempt*" — it is the single
            # fact ``tools.applications.reaper``'s apply fork reads. Left
            # standing from an earlier attempt it blunts that fork on exactly
            # the documents most likely to be retried: after a
            # ``release_uncertain`` the user retries from ``failed``, and if
            # *that* run dies before ever reaching the button the reaper reports
            # it as uncertain again — about a run that provably never clicked.
            #
            # Cleared here rather than anywhere else because this swap runs in
            # the API request, before the apply task is dispatched and therefore
            # before any browser exists: there is no window in which this write
            # can erase a marker that a live run just set.
            #
            # ``submission_uncertain`` is deliberately *not* cleared. That flag
            # is the durable record that some past submission of this
            # application may already be with the employer, and it stays true
            # however many times the user tries again.
            "submit_attempted_at": firestore.DELETE_FIELD,
        },
    ):
        raise HTTPException(
            status_code=409, detail=f"cannot submit from status '{status}'"
        )
    log.info(
        "application.submit_requested",
        app_id=app_id,
        user_id=user_id,
        status_was=status,
        attempt=attempt,
    )
    try:
        dispatched = dispatch_apply(
            user_id, app_id, attempt=attempt, background_tasks=background_tasks
        )
    except Exception as e:
        # A transient Cloud Tasks error (503, DEADLINE_EXCEEDED, quota) is an
        # ordinary event, and this PR is what put that RPC on the submit path.
        log.exception(
            "application.submit_not_dispatched", app_id=app_id, attempt=attempt
        )
        _abandon_unstarted_claim(ref, app_id)
        raise HTTPException(
            status_code=503, detail="could not schedule the submission"
        ) from e
    if not dispatched:
        # A reused task name — see dispatch_apply. Not a double-click: that one
        # lost the swap above and never got here. The reachable way in is the
        # undo path: revert deletes the application, re-approving recreates it at
        # the same deterministic id with the counter gone, and the next submit
        # rebuilds a name whose Cloud Tasks tombstone is still alive.
        log.error("application.submit_dispatch_deduped", app_id=app_id, attempt=attempt)
        _abandon_unstarted_claim(ref, app_id)
        raise HTTPException(status_code=503, detail="could not schedule the submission")
    return {"ok": True}


@router.get("/applications/{app_id}/events")
async def events(
    app_id: str,
    request: Request,
    user_id: str = Depends(verify_user_query),
) -> EventSourceResponse:
    """Server-sent progress for a submission, until it reaches a terminal status."""
    ref = _apps(user_id).document(app_id)

    async def gen():
        seen = 0
        while True:
            if await request.is_disconnected():
                break
            snap = await asyncio.to_thread(ref.get)
            if not snap.exists:
                yield {"event": "error", "data": "not found"}
                break
            d = snap.to_dict()
            timeline = d.get("timeline", [])
            for ev in timeline[seen:]:
                yield {"event": "progress", "data": json.dumps(ev)}
            seen = len(timeline)
            status = d.get("status")
            yield {"event": "status", "data": status}
            if status in TERMINAL or status == "failed":
                break
            await asyncio.sleep(1.5)

    return EventSourceResponse(gen())
