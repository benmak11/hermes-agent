# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Job vetting endpoints: list scored pending jobs, record a decision."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from api.deps import verify_user
from api.routes.applications import application_id, run_tailoring
from obs.logging import get_logger

router = APIRouter(tags=["jobs"])
log = get_logger("api.jobs")

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


@router.get("/jobs/pending")
def list_pending_jobs(
    user_id: str = Depends(verify_user), min_score: int = 60
) -> dict:
    """Return scored, still-pending jobs above min_score, ranked high to low."""
    snaps = (
        _client()
        .collection("users")
        .document(user_id)
        .collection("jobs")
        .where(filter=FieldFilter("user_decision", "==", "pending"))
        .stream()
    )
    jobs = []
    for snap in snaps:
        d = snap.to_dict()
        match = d.get("match")
        if not match:  # not scored yet
            continue
        if match.get("overall_score", 0) < min_score:
            continue
        jobs.append({"id": snap.id, **d})
    jobs.sort(key=lambda j: j["match"]["overall_score"], reverse=True)
    return {"jobs": jobs}


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

    Approving a job kicks off tailoring: create the Application in ``tailoring``
    state and schedule the (LLM + render + upload) pipeline as a background task.
    Idempotent — an existing Application is left untouched.

    Reverting (``pending``) puts the job back in the review queue; an
    application the agent hasn't submitted yet is discarded so the pipeline
    view matches. A submitted/responded application is history and stays.
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

    if body.decision == "approved":
        app_ref = user_ref.collection("applications").document(application_id(job_id))
        if not app_ref.get().exists:
            now = datetime.now(UTC).isoformat()
            app_ref.set(
                {
                    "id": application_id(job_id),
                    "user_id": user_id,
                    "job_id": job_id,
                    "status": "tailoring",
                    "timeline": [{"at": now, "status": "tailoring"}],
                }
            )
            background_tasks.add_task(run_tailoring, user_id, job_id)
            log.info("job.tailoring_scheduled", job_id=job_id)

    return {"ok": True}
