# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Application endpoints: tailoring lifecycle + the diff/review surface.

The approval hook in ``jobs.decide`` creates an Application in ``queued`` state
and schedules ``run_tailoring`` as a background task, which claims the work by
moving it to ``tailoring``; these endpoints let the web app poll, edit the
objective, regenerate, and hand off to submission.

Every ``status`` write in here goes through ``tools.applications.state`` — the
compare-and-swap that stops a double-click on Submit from starting two live ATS
submissions. Nothing in this module writes a status field directly.
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
from tools.applications import state
from tools.ats.validate import check_posting
from tools.run_costs import persist_run_cost
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
    user_ref.collection("jobs").document(job.id).update(
        {"user_decision": "dismissed", "posting_removed_at": _now()}
    )
    # The caller stops either way — the posting really is gone. A refused
    # transition means the document moved out from under us: either someone
    # already parked it somewhere terminal, or it is now owned by a run this
    # caller must not interrupt.
    if not state.try_transition(
        app_ref,
        app_ref.get(),
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
            if not state.try_transition(
                app_ref,
                app_ref.get(),
                "tailoring",
                lease=state.lease_for("tailoring", owner=state.new_owner()),
            ):
                task_log.info("tailoring.not_claimed")
                log_agent_end(task_log, "tailoring", started, outcome="not_claimed")
                return

            profile = MasterProfile.model_validate(user_ref.get().to_dict())
            job_doc = user_ref.collection("jobs").document(job_id).get()
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
            decision = (
                user_ref.collection("jobs").document(job_id).get().to_dict() or {}
            ).get("user_decision")
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
            app_ref.update(content)
            # CLEAR_LEASE: the run is over. A tailoring lease left on a
            # ready_for_review document would still be live when the user clicks
            # Submit, and the worker's own claim on ``submitting`` would find it
            # held and refuse — a submission silently dropped by a lease nobody
            # owns any more.
            if not state.try_transition(
                app_ref, app_ref.get(), "ready_for_review", lease=state.CLEAR_LEASE
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
            if not app_ref.get().exists:
                log_agent_end(task_log, "tailoring", started, outcome="discarded")
                return  # discarded by a revert while we ran — don't resurrect
            state.try_transition(
                app_ref,
                app_ref.get(),
                "failed",
                note=str(e)[:300],
                lease=state.CLEAR_LEASE,
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
    submitted, posting removed) — the background task must not be scheduled when
    the state change didn't happen.

    An application that is *already* queued is re-scheduled rather than
    refused: a background task that never fired leaves the doc stuck there, and
    this is the user's only manual way out of it. Safe because ``run_tailoring``
    claims — a duplicate scheduling can't spend a second LLM run.
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    doc = snap.to_dict()
    job_id = doc["job_id"]
    if doc.get("status") != state.INITIAL and not state.try_transition(
        ref, snap, state.INITIAL, note="regenerate"
    ):
        raise HTTPException(
            status_code=409,
            detail=f"cannot regenerate from status '{doc.get('status')}'",
        )
    log.info("application.regenerate", app_id=app_id, job_id=job_id, user_id=user_id)
    background_tasks.add_task(run_tailoring, user_id, job_id)
    return {"ok": True}


async def run_submission(user_id: str, app_id: str, *, dry_run: bool = False) -> None:
    """Background task: submit the application to the live ATS and record evidence.

    Downloads the tailored resume, runs the per-source submitter, uploads pre/post
    screenshots to GCS, and writes the terminal status (submitted/failed). Progress
    is appended to the timeline as it goes so the SSE stream can relay it live.

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
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        return
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
        # Timeline only. The submitter's second argument is a display label for
        # the step ("Opening ...", "Attaching resume"), not a lifecycle edge —
        # it emits "submitted" the moment it sees a confirmation page, and
        # honouring that as a transition would lock out the real terminal write
        # below, which is the one carrying the screenshots and confirmation.
        if dry_run:
            # A rehearsal's steps are the *same* steps, so unmarked they render
            # on the tracking page as a submission in progress — "Opening…",
            # "Attaching resume" — against a document nobody submitted. Keep the
            # entry's status where the document actually is and label the note.
            state.append_note(
                ref, found_status or "ready_for_review", DRY_RUN_NOTE + message
            )
            return
        state.append_note(ref, status, message)

    # run_id context so the submitter's own log lines (tools.submitters, the
    # Playwright steps) stitch to this submission in Cloud Logging.
    with run_context("submission", user_id=user_id, app_id=app_id, job_id=job_id):
        started = log_agent_start(task_log, "submission", source=app.get("job_source"))
        try:
            resume_uri = app.get("resume_variant_uri")
            if not resume_uri:
                raise ValueError("No tailored resume to submit — run tailoring first.")
            profile = MasterProfile.model_validate(user_ref.get().to_dict())
            job = Job.model_validate(
                user_ref.collection("jobs").document(job_id).get().to_dict()
            )

            # Last-line check: never drive a browser at a posting the ATS says
            # is gone. Fail-open — a flaky board proceeds and fails visibly.
            if await _dismiss_if_posting_removed(
                user_ref, ref, job, task_log, allowed_from=owned
            ):
                log_agent_end(
                    task_log, "submission", started, outcome="posting_removed"
                )
                return

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
                # wherever the worker found it (a submittable status — the
                # handler checks), and there is no edge that would put it back
                # if a dry run moved it, so it moves it nowhere.
                state.append_note(
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
                recorded = state.try_transition(
                    ref,
                    ref.get(),
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
                    task_log.error(
                        "submission.result_not_recorded",
                        submitted_at=submitted_at,
                        confirmation_uri=confirm_uri,
                        screenshot_uris=[s["uri"] for s in shots],
                        status_now=(ref.get().to_dict() or {}).get("status"),
                    )
            elif not state.try_transition(
                ref,
                ref.get(),
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
                state.append_note(
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
                return
            if not state.try_transition(
                ref, ref.get(), "failed", note=str(e)[:300], lease=state.CLEAR_LEASE
            ):
                # Leaves the document wedged in ``submitting``; cli/unwedge_submitting
                # is the manual way out until the reaper lands.
                task_log.warning("submission.failure_not_recorded", error=str(e)[:300])
            log_agent_end(
                task_log, "submission", started, outcome="failed", error=str(e)[:300]
            )


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
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    status = snap.to_dict().get("status")
    if not state.try_transition(
        ref, snap, "submitting", extra={"last_submitted_at": _now()}
    ):
        raise HTTPException(
            status_code=409, detail=f"cannot submit from status '{status}'"
        )
    log.info(
        "application.submit_requested",
        app_id=app_id,
        user_id=user_id,
        status_was=status,
    )
    # NOTE FOR THE PR THAT MOVES THIS TO THE QUEUE. The lease that fences
    # ``/tasks/apply`` fences *nothing here*: this path takes the status claim
    # and then submits in-process, without ever writing a lease. That is fine
    # while it is the only path — but it stops being fine during the cutover.
    # Cloud Run shifts traffic between revisions gradually, so for the length of
    # the migration the old revision (in-process, no lease) and the new one
    # (enqueue, leased) both serve, on the same documents, and neither can see
    # the other's claim. The status CAS above is the only thing keeping the two
    # apart, and it is enough *only* because both paths take it before doing any
    # work: keep it that way, or move the claim into ``run_submission`` so both
    # paths are fenced by the same lease before splitting traffic.
    background_tasks.add_task(run_submission, user_id, app_id)
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
