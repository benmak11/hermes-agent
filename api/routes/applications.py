# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Application endpoints: tailoring lifecycle + the diff/review surface.

The approval hook in ``jobs.decide`` creates an Application in ``tailoring`` state
and schedules ``run_tailoring`` as a background task; these endpoints let the web
app poll, edit the objective, regenerate, and hand off to submission.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from google.cloud import firestore
from pydantic import BaseModel

from api.deps import verify_user
from models.job import Job
from models.profile import MasterProfile
from tools.tailoring.pipeline import application_id, tailor_application

router = APIRouter(tags=["applications"])

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _apps(user_id: str):
    return _client().collection("users").document(user_id).collection("applications")


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def run_tailoring(user_id: str, job_id: str) -> None:
    """Background task: tailor an approved job and persist the Application.

    Reads the profile + job, runs the tailoring pipeline, and writes the result
    onto the existing (``tailoring``-state) Application doc. On failure the doc is
    flipped to ``failed`` with a timeline note so the UI can surface it.
    """
    db = _client()
    user_ref = db.collection("users").document(user_id)
    app_ref = user_ref.collection("applications").document(application_id(job_id))
    try:
        profile = MasterProfile.model_validate(user_ref.get().to_dict())
        job_doc = user_ref.collection("jobs").document(job_id).get()
        if not job_doc.exists:
            raise ValueError(f"Job {job_id} not found")
        job = Job.model_validate(job_doc.to_dict())

        app = await tailor_application(job, profile, upload=True)
        app_ref.set(app.model_dump(mode="json"))
    except Exception as e:  # persist failure for the UI, surface in timeline
        app_ref.set(
            {
                "status": "failed",
                "timeline": firestore.ArrayUnion(
                    [{"at": _now(), "status": "failed", "note": str(e)[:300]}]
                ),
            },
            merge=True,
        )


@router.get("/applications")
def list_applications(user_id: str = Depends(verify_user)) -> dict:
    """All applications for the user, newest activity first."""
    apps = [s.to_dict() for s in _apps(user_id).stream()]
    apps.sort(
        key=lambda a: (a.get("timeline") or [{}])[-1].get("at", ""), reverse=True
    )
    return {"applications": apps}


@router.get("/applications/{app_id}")
def get_application(app_id: str, user_id: str = Depends(verify_user)) -> dict:
    snap = _apps(user_id).document(app_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    return snap.to_dict()


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
    return {"ok": True}


@router.post("/applications/{app_id}/regenerate")
def regenerate(
    app_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
) -> dict:
    """Re-run tailoring for this application's job (explicit user action)."""
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    job_id = snap.to_dict()["job_id"]
    ref.set(
        {
            "status": "tailoring",
            "timeline": firestore.ArrayUnion(
                [{"at": _now(), "status": "tailoring", "note": "regenerate"}]
            ),
        },
        merge=True,
    )
    background_tasks.add_task(run_tailoring, user_id, job_id)
    return {"ok": True}


@router.post("/applications/{app_id}/submit")
def submit(app_id: str, user_id: str = Depends(verify_user)) -> dict:
    """Approve the tailored materials and hand off to submission (Phase 7)."""
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    if snap.to_dict().get("status") != "ready_for_review":
        raise HTTPException(status_code=409, detail="application not ready for review")
    ref.set(
        {
            "status": "submitting",
            "timeline": firestore.ArrayUnion(
                [{"at": _now(), "status": "submitting"}]
            ),
        },
        merge=True,
    )
    return {"ok": True}
