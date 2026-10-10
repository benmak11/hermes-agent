# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Profile endpoints: onboarding + the Profile surface.

These populate the ``users/{uid}`` profile doc that Discovery and Matching
read. ``POST /profile/extract`` and the first ``PUT /profile`` both commit real
Gemini spend — the extract calls Gemini directly, and the first completion of
onboarding kicks off a discovery cycle.
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
from models.settings import DiscoverySettings
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import queues
from tools.account import plan
from tools.discovery import budget as discovery_budget
from tools.matching import budget as matching_budget
from tools.profile import extract_budget
from tools.profile.extract import extract_profile, read_resume_text
from tools.run_costs import DONE, FAILED, RUNNING, open_run, persist_run_cost

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

    The epoch identifies this incarnation of the user's server-side data. The
    browser stores its review tallies against it and discards them when it
    changes, which is the only way a server-side wipe can reach counts living
    in ``localStorage``.

    Stamped lazily on read rather than at onboarding because
    :func:`tools.account.delete.wipe_user_data` deletes the user document
    outright, so a value written at onboarding does not survive to be bumped.
    Reading also covers documents created by paths that never touch onboarding,
    such as the CLI profile sync. One write per user, once.
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
        "plan": plan.view(
            data,
            now=datetime.now(UTC),
            scoring_per_day=matching_budget.Limits.for_doc(data).per_day,
        ),
    }


def _new_account_fields(existing: dict, now: datetime) -> dict:
    """What the first onboarding completion adds beside the profile.

    The plan's trial start, once (see :func:`plan.onboarding_fields`). And, only
    when the account has no ``discovery_settings`` at all, both loops on at
    24h, with a discovery lease so an opportunistic tick in the minutes after
    onboarding does not dispatch a second cycle on top of the kickoff — the
    kickoff's success write clears it, and it expires on its own otherwise.
    """
    fields = plan.onboarding_fields(existing, now=now)
    if "discovery_settings" not in existing:
        from api.routes.discovery import kickoff_lease

        fields["discovery_settings"] = DiscoverySettings(
            auto_discovery=True,
            discovery_interval_hours=24,
            liveness_sweep=True,
            sweep_interval_hours=24,
        ).model_dump()
        fields["discovery_state"] = {"discovery_lease": kickoff_lease(now)}
    return fields


def _extract_cap_429(charge: extract_budget.Charge) -> HTTPException:
    """The daily-extraction refusal. 429, not 402: a cap is not a price."""
    return HTTPException(
        status_code=429,
        detail={
            "reason": "extract_cap",
            "used": charge.used,
            "per_day": charge.per_day,
            # An ISO instant; the client renders it in the viewer's timezone.
            "resets_at": charge.resets_at,
        },
    )


async def _resume_text(file: UploadFile | None, text: str | None) -> str:
    """The résumé as plain text, or the 4xx that says why it can't be read."""
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
    return resume_text


@router.post("/profile/extract")
async def extract(
    user_id: str = Depends(verify_user),
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
) -> dict:
    """Extract a draft profile from an uploaded resume or pasted text. Spends
    real money on Gemini.

    Saves the result to ``users/{uid}`` as a draft
    (``onboarding_complete=false``) and returns it for the review screen. The
    blocking Gemini call runs off the event loop.

    Charged against the daily extraction cap first (429 ``extract_cap`` when
    it is spent); the slot is refunded if the upload cannot be read, but not
    once Gemini has been called. See :mod:`tools.profile.extract_budget`.
    """
    charge = await asyncio.to_thread(extract_budget.charge, _client(), user_id)
    if not charge.granted:
        raise _extract_cap_429(charge)

    try:
        resume_text = await _resume_text(file, text)
    except Exception:
        # Nothing reached the model, so the slot was never spent.
        if charge.charged:
            await asyncio.to_thread(
                extract_budget.refund, _client(), user_id, day=charge.day
            )
        raise

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
            ledger_state = RUNNING
            await open_run(
                _client,
                user_id,
                run_id,
                runner="profile_extract",
                trigger=source,
                started_at=started_at,
            )
            try:
                profile = await asyncio.to_thread(extract_profile, resume_text, user_id)
                ledger_state = DONE
                log_agent_end(
                    log,
                    "profile_extract",
                    started,
                    outcome="completed",
                    roles=len(profile.experience),
                )
            except Exception:
                # Re-raised untouched (the outer handler turns it into a 422);
                # this clause only records the outcome on the ledger.
                ledger_state = FAILED
                raise
            finally:
                # The first paid call a new user triggers, and it binds a
                # run_id — flush it here or its cost sits unbanked in the API
                # process. In the finally because a parse that failed
                # validation still spent the tokens.
                await persist_run_cost(
                    _client,
                    user_id,
                    run_id,
                    runner="profile_extract",
                    trigger=source,
                    state=ledger_state,
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

    **The first completion kicks off a discovery cycle (fetch + score), which
    commits real Gemini spend**, with no separate button. Later edits to an
    already-complete profile do not repeat it, and the run is charged against
    the weekly search allowance here, at dispatch. The same write starts the
    trial and, for an account with no discovery settings, turns both loops on
    (see :func:`_new_account_fields`).

    The body is validated as a full :class:`MasterProfile`; ``user_id`` is
    forced to the authenticated user so a client cannot write someone else's
    profile.

    Where there is a queue, the kickoff enqueues inside the request rather than
    deferring one RPC to an instance that may have no CPU after the response.
    If that RPC fails, ``onboarding_complete`` is rolled back: the flag is the
    only thing that makes the kickoff fire again, so leaving it set would mean
    an onboarded user whose discovery never runs and has nothing to retry.
    Without a queue the kickoff is a background task whose failure this request
    cannot see, and the flag stays written.
    """
    existing = _user_ref(user_id).get().to_dict() or {}
    first_completion = not existing.get("onboarding_complete")

    body.user_id = user_id
    extra = _new_account_fields(existing, datetime.now(UTC)) if first_completion else {}
    _user_ref(user_id).set(
        {**body.model_dump(mode="json"), "onboarding_complete": True, **extra},
        merge=True,
    )
    log.info(
        "profile.saved",
        user_id=user_id,
        roles=len(body.experience),
        skill_groups=len(body.skills),
    )
    if first_completion:
        from api.routes.discovery import dispatch_cycle, enqueue_cycle

        # The third and last site that charges the weekly search allowance,
        # here at dispatch and never inside the cycle. Synchronous because
        # this route is.
        reservation = discovery_budget.reserve_sync(_client(), user_id)
        if reservation.granted <= 0:
            # A brand-new account cannot hit this; a re-completion after a
            # failed enqueue can. Fail closed: the user has already had this
            # week's searches, and the scheduled loop picks them up next week.
            log.info("profile.onboarding_kickoff_capped", user_id=user_id)
            return {"ok": True}

        log.info("profile.onboarding_discovery_kickoff", user_id=user_id)
        if queues.enabled():
            try:
                queued = enqueue_cycle("discovery", user_id, trigger="onboarding")
            except Exception as e:
                # Everything that can throw here is environmental (Cloud Tasks
                # 503, a missing IAM binding, unset config) and none of it means
                # the profile save failed. Give the flag back so the retry is a
                # real retry, and tell the user something to retry.
                _user_ref(user_id).set({"onboarding_complete": False}, merge=True)
                # The retry re-fires the kickoff, so it must not pay twice.
                discovery_budget.release_sync(
                    _client(), user_id, 1, week=reservation.week_key
                )
                log.exception("profile.onboarding_kickoff_failed", user_id=user_id)
                raise HTTPException(
                    status_code=503,
                    detail="profile saved, but the first search could not be "
                    "started — please save again",
                ) from e
            if not queued:
                # Deduped: the queued task is the kickoff, and one search is
                # one charge.
                discovery_budget.release_sync(
                    _client(), user_id, 1, week=reservation.week_key
                )
            # Deduped means a kickoff for this user and hour is already
            # queued, which is the outcome we wanted, not a failure.
            log.info(
                "profile.onboarding_kickoff_queued",
                user_id=user_id,
                deduped=not queued,
            )
        else:
            # No queue: dispatch_cycle would run the whole discovery-and-
            # scoring cycle here, which is minutes of work and cannot happen
            # inside the request.
            background_tasks.add_task(
                dispatch_cycle, "discovery", user_id, trigger="onboarding"
            )
    return {"ok": True}
