# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Application endpoints: tailoring lifecycle + the diff/review surface.

The approval hook in ``jobs.decide`` creates an Application in ``tailoring`` state
and schedules ``run_tailoring`` as a background task; these endpoints let the web
app poll, edit the objective, regenerate, and hand off to submission.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from tools.submitters.router import submit_application
from tools.submitters.storage import download_resume, upload_screenshot
from tools.tailoring.pipeline import application_id, tailor_application

# Statuses from which a fresh submission is allowed (failed permits a retry).
SUBMITTABLE = {"ready_for_review", "failed"}
TERMINAL = {"submitted", "responded"}

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
    apps.sort(
        key=lambda a: (a.get("timeline") or [{}])[-1].get("at", ""), reverse=True
    )
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


async def run_submission(user_id: str, app_id: str) -> None:
    """Background task: submit the application to the live ATS and record evidence.

    Downloads the tailored resume, runs the per-source submitter, uploads pre/post
    screenshots to GCS, and writes the terminal status (submitted/failed). Progress
    is appended to the timeline as it goes so the SSE stream can relay it live.
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        return
    app = snap.to_dict()
    job_id = app["job_id"]
    user_ref = _client().collection("users").document(user_id)

    def progress(message: str, status: str) -> None:
        ref.set(
            {
                "timeline": firestore.ArrayUnion(
                    [{"at": _now(), "status": status, "note": message}]
                )
            },
            merge=True,
        )

    try:
        resume_uri = app.get("resume_variant_uri")
        if not resume_uri:
            raise ValueError("No tailored resume to submit — run tailoring first.")
        profile = MasterProfile.model_validate(user_ref.get().to_dict())
        job = Job.model_validate(
            user_ref.collection("jobs").document(job_id).get().to_dict()
        )
        resume_path = download_resume(resume_uri)

        result = await submit_application(
            job, profile, resume_path, dry_run=False, headless=True, on_progress=progress
        )

        shots: list[dict] = []
        for key, name in (
            ("pre_submit_screenshot", "pre_submit.png"),
            ("confirmation_screenshot", "confirmation.png"),
        ):
            local = result.get(key)
            if local and os.path.exists(local):
                shots.append(
                    {"name": name, "uri": upload_screenshot(Path(local), user_id, job_id, name)}
                )

        if result.get("success"):
            confirm_uri = next(
                (s["uri"] for s in shots if s["name"] == "confirmation.png"), None
            )
            ref.set(
                {
                    "status": "submitted",
                    "screenshots": shots,
                    "confirmation": {
                        "submitted_at": _now(),
                        "screenshot_uri": confirm_uri,
                    },
                    "timeline": firestore.ArrayUnion(
                        [{"at": _now(), "status": "submitted"}]
                    ),
                },
                merge=True,
            )
        else:
            ref.set(
                {
                    "status": "failed",
                    "screenshots": shots,
                    "timeline": firestore.ArrayUnion(
                        [
                            {
                                "at": _now(),
                                "status": "failed",
                                "note": (result.get("error") or "submission failed")[:300],
                            }
                        ]
                    ),
                },
                merge=True,
            )
    except Exception as e:  # record failure for the UI
        ref.set(
            {
                "status": "failed",
                "timeline": firestore.ArrayUnion(
                    [{"at": _now(), "status": "failed", "note": str(e)[:300]}]
                ),
            },
            merge=True,
        )


@router.post("/applications/{app_id}/submit")
def submit(
    app_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(verify_user),
) -> dict:
    """Submit the tailored application to the live ATS (explicit user action).

    Idempotency-locked: only a ``ready_for_review`` (or previously ``failed``)
    application may be submitted; ``submitting``/``submitted`` are rejected so a
    job is never auto-resubmitted.
    """
    ref = _apps(user_id).document(app_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="application not found")
    status = snap.to_dict().get("status")
    if status not in SUBMITTABLE:
        raise HTTPException(
            status_code=409, detail=f"cannot submit from status '{status}'"
        )
    ref.set(
        {
            "status": "submitting",
            "last_submitted_at": _now(),
            "timeline": firestore.ArrayUnion(
                [{"at": _now(), "status": "submitting"}]
            ),
        },
        merge=True,
    )
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
