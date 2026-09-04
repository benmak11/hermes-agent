# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Profile endpoints: the front of the funnel (onboarding) + the Profile surface.

Nothing populated the ``users/{uid}`` profile doc that Discovery and Matching
read — these endpoints add it:

- ``GET  /profile``         first-run gate: is there a profile yet?
- ``POST /profile/extract`` upload a resume (file or pasted text) → Gemini →
                            draft profile saved with ``onboarding_complete=false``.
- ``PUT  /profile``         save the reviewed/edited profile and mark onboarding
                            complete (the "Looks good — find me jobs" action, and
                            later edits from the Profile page).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from google.cloud import firestore

from api.deps import verify_user
from models.profile import MasterProfile
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import queues
from tools.profile.extract import extract_profile, read_resume_text
from tools.run_costs import persist_run_cost

log = get_logger("api.profile")

# Cap upload size so a hostile/huge file can't blow up memory (design says 10 MB).
MAX_RESUME_BYTES = 10 * 1024 * 1024

router = APIRouter(tags=["profile"])

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _user_ref(user_id: str):
    return _client().collection("users").document(user_id)


def _ensure_data_epoch(user_id: str, data: dict) -> dict:
    """Stamp ``data_epoch`` on a profile that predates it, and return the data.

    The epoch identifies *this incarnation* of the user's server-side data. The
    browser stores its review tallies against it and discards them when it
    changes, which is the only way a server-side wipe can reach counts that
    live in ``localStorage`` — see ``web/src/lib/session.ts``.

    Stamped lazily on read rather than written at onboarding, for one reason:
    :func:`tools.account.delete.wipe_user_data` **deletes the user document
    outright**, so a value written at onboarding does not survive to be bumped.
    A fresh document simply has no epoch, gets a new one here, and the mismatch
    clears the stale counts. Doing it on read also covers documents created by
    paths that never touch onboarding at all (the CLI profile sync), and costs
    one write per user, once, ever.
    """
    if data.get("data_epoch"):
        return data
    epoch = uuid.uuid4().hex
    _user_ref(user_id).set({"data_epoch": epoch}, merge=True)
    log.info("profile.data_epoch_stamped", user_id=user_id, data_epoch=epoch)
    return {**data, "data_epoch": epoch}


@router.get("/profile")
def get_profile(user_id: str = Depends(verify_user)) -> dict:
    """Return the user's profile and onboarding state (the first-run gate).

    ``profile`` is null when the user has never onboarded (the doc is absent or
    holds only a jobs subcollection / settings with no ``full_name``). Profiles
    synced via the CLI predate the flag, so a missing flag counts as complete.
    """
    snap = _user_ref(user_id).get()
    data = snap.to_dict() if snap.exists else None
    if not data or not data.get("full_name"):
        return {"profile": None, "onboarding_complete": False}
    data = _ensure_data_epoch(user_id, data)
    return {
        "profile": data,
        "onboarding_complete": data.get("onboarding_complete", True),
    }


@router.post("/profile/extract")
async def extract(
    user_id: str = Depends(verify_user),
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
) -> dict:
    """Extract a draft profile from an uploaded resume or pasted text.

    Saves the result to ``users/{uid}`` as a draft (``onboarding_complete=false``)
    and returns it for the review screen. The blocking Gemini call runs off the
    event loop.
    """
    if file is not None:
        raw = await file.read()
        if len(raw) > MAX_RESUME_BYTES:
            raise HTTPException(status_code=413, detail="Resume exceeds 10 MB limit.")
        filename = file.filename or "resume.pdf"
        resume_text = read_resume_text(raw, filename)
    elif text and text.strip():
        resume_text = text
    else:
        raise HTTPException(status_code=400, detail="Provide a resume file or text.")

    if not resume_text.strip():
        raise HTTPException(
            status_code=422, detail="Could not read any text from that resume."
        )

    source = "file" if file is not None else "text"
    log.info("profile.extract.request", source=source, chars=len(resume_text))
    try:
        with run_context("profile_extract", user_id=user_id) as run_id:
            started_at = datetime.now(UTC).isoformat()
            started = log_agent_start(
                log,
                "profile_extract",
                user_id=user_id,
                source=source,
                chars=len(resume_text),
            )
            try:
                profile = await asyncio.to_thread(extract_profile, resume_text, user_id)
                log_agent_end(
                    log,
                    "profile_extract",
                    started,
                    outcome="completed",
                    roles=len(profile.experience),
                )
            finally:
                # The first paid call a new user ever triggers, and it binds a
                # run_id — flush it here or its cost sits in the API process
                # forever, unbanked. A parse that failed validation still
                # spent the tokens, hence the finally.
                await persist_run_cost(
                    _client,
                    user_id,
                    run_id,
                    runner="profile_extract",
                    trigger=source,
                    started_at=started_at,
                )
    except Exception as e:  # extraction/validation failure → 422 for the UI
        log.exception("profile.extract.failed", chars=len(resume_text))
        log_agent_end(
            log, "profile_extract", started, outcome="failed", error=str(e)[:300]
        )
        raise HTTPException(
            status_code=422, detail=f"Could not parse that resume: {e}"
        ) from e

    payload = profile.model_dump(mode="json")
    _user_ref(user_id).set({**payload, "onboarding_complete": False}, merge=True)
    log.info("profile.extract.saved", roles=len(profile.experience))
    return {"profile": payload}


@router.put("/profile")
def save_profile(
    body: MasterProfile,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
) -> dict:
    """Persist the reviewed/edited profile and mark onboarding complete.

    The body is validated as a full :class:`MasterProfile`; ``user_id`` is forced
    to the authenticated user so a client can't write someone else's profile.

    The *first* time a user completes onboarding, this also kicks off one
    discovery cycle (fetch + score) so the "Discovery and Matching are running
    now" promise on the review screen is actually true — nothing else in the
    app fires an initial run, and ``auto_discovery`` defaults to off. Later
    profile edits (re-PUTting an already-complete profile) don't repeat this.

    That kickoff enqueues **inside the request** where there is a queue to
    enqueue to. It is one RPC, and it is the only thing that ever fires for a
    brand-new user, so it must not be the one part of this route that depends on
    the instance still having CPU after the response — the same failure mode
    that made "discovery never runs" a bug in the first place.

    **And if that RPC fails, ``onboarding_complete`` does not stick.** The flag
    is the only thing that makes this kickoff fire again, so leaving it set on a
    failed enqueue re-creates the very bug under a different cause: onboarded
    user, error on screen, discovery never runs, nothing to retry. Rolling it
    back costs the user one more click of a button whose contents are already
    saved, and that click re-fires the kickoff. (Without a queue the kickoff is
    a background task whose failure this request cannot see, exactly as before —
    the flag is written and stays written.)
    """
    existing = _user_ref(user_id).get().to_dict() or {}
    first_completion = not existing.get("onboarding_complete")

    body.user_id = user_id
    _user_ref(user_id).set(
        {**body.model_dump(mode="json"), "onboarding_complete": True}, merge=True
    )
    log.info(
        "profile.saved",
        user_id=user_id,
        roles=len(body.experience),
        skill_groups=len(body.skills),
    )
    if first_completion:
        from api.routes.discovery import dispatch_cycle, enqueue_cycle

        log.info("profile.onboarding_discovery_kickoff", user_id=user_id)
        if queues.enabled():
            try:
                queued = enqueue_cycle("discovery", user_id, trigger="onboarding")
            except Exception as e:
                # Everything that can throw here is environmental — Cloud Tasks
                # 503, a missing IAM binding, an unset WORKER_URL/TASKS_SA_EMAIL
                # (queues.enqueue reads those with os.environ[...]) — and none of
                # it means the profile save failed. Give the flag back so the
                # retry is a real retry, and tell the user something to retry.
                _user_ref(user_id).set({"onboarding_complete": False}, merge=True)
                log.exception("profile.onboarding_kickoff_failed", user_id=user_id)
                raise HTTPException(
                    status_code=503,
                    detail="profile saved, but the first search could not be "
                    "started — please save again",
                ) from e
            # Deduped means a kickoff for this user and hour is already queued,
            # which is the outcome we wanted; it is not a failure.
            log.info(
                "profile.onboarding_kickoff_queued",
                user_id=user_id,
                deduped=not queued,
            )
        else:
            # No queue: dispatch_cycle would run the whole discovery-and-scoring
            # cycle right here, which is minutes of work and cannot happen
            # inside the request. Deferred, as before.
            background_tasks.add_task(
                dispatch_cycle, "discovery", user_id, trigger="onboarding"
            )
    return {"ok": True}
