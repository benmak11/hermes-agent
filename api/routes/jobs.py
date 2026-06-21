# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Job vetting endpoints: list scored pending jobs, record a decision."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from api.deps import verify_user

router = APIRouter(tags=["jobs"])

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


class Decision(BaseModel):
    decision: Literal["approved", "rejected", "starred"]


@router.post("/jobs/{job_id}/decide")
def decide(
    job_id: str, body: Decision, user_id: str = Depends(verify_user)
) -> dict:
    """Record the user's decision on a job."""
    (
        _client()
        .collection("users")
        .document(user_id)
        .collection("jobs")
        .document(job_id)
        .update({"user_decision": body.decision})
    )
    return {"ok": True}
