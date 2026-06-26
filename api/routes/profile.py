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

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from google.cloud import firestore

from api.deps import verify_user
from models.profile import MasterProfile
from obs.logging import get_logger
from tools.profile.extract import extract_profile, read_resume_text

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

    log.info(
        "profile.extract.request",
        source="file" if file is not None else "text",
        chars=len(resume_text),
    )
    try:
        profile = await asyncio.to_thread(extract_profile, resume_text, user_id)
    except Exception as e:  # extraction/validation failure → 422 for the UI
        log.exception("profile.extract.failed", chars=len(resume_text))
        raise HTTPException(
            status_code=422, detail=f"Could not parse that resume: {e}"
        ) from e

    payload = profile.model_dump(mode="json")
    _user_ref(user_id).set({**payload, "onboarding_complete": False}, merge=True)
    log.info("profile.extract.saved", roles=len(profile.experience))
    return {"profile": payload}


@router.put("/profile")
def save_profile(
    body: MasterProfile, user_id: str = Depends(verify_user)
) -> dict:
    """Persist the reviewed/edited profile and mark onboarding complete.

    The body is validated as a full :class:`MasterProfile`; ``user_id`` is forced
    to the authenticated user so a client can't write someone else's profile.
    """
    body.user_id = user_id
    _user_ref(user_id).set(
        {**body.model_dump(mode="json"), "onboarding_complete": True}, merge=True
    )
    return {"ok": True}
