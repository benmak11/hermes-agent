# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Journey endpoints: the ``/app/journeys`` board's storage.

One document per hiring process under ``users/{uid}/journeys/{id}``,
whole-document and client-owned (last write wins): the browser builds the
document, the server validates shape plus the two invariants on
:class:`models.journey.Journey` and forces the server-owned fields. No state
machine, no hooks into discovery or the reaper, no LLM.

- ``GET    /journeys``       every journey, most recently updated first
- ``POST   /journeys``       create; the server assigns id + timestamps
- ``PUT    /journeys/{id}``  full replace; ``created_at`` is preserved
- ``DELETE /journeys/{id}``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore

from api.deps import verify_user
from models.journey import Journey
from obs.logging import get_logger
from tools.journeys import journeys_ref

log = get_logger("api.journeys")

router = APIRouter(tags=["journeys"])

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _now() -> datetime:
    return datetime.now(UTC)


def _journeys(user_id: str):
    return journeys_ref(_client(), user_id)


@router.get("/journeys")
def list_journeys(user_id: str = Depends(verify_user)) -> dict:
    """All journeys for the user, most recently updated first."""
    journeys = [
        Journey.model_validate(s.to_dict()) for s in _journeys(user_id).stream()
    ]
    # Sorted here rather than with ``order_by``: no index, and the field is an
    # ISO string that sorts correctly as text.
    # Aware sentinel: a naive datetime.min would TypeError against aware stamps.
    journeys.sort(
        key=lambda j: j.updated_at or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return {"journeys": [j.model_dump(mode="json") for j in journeys]}


@router.post("/journeys")
def create_journey(body: Journey, user_id: str = Depends(verify_user)) -> dict:
    """Create a journey. Whatever the client sent for the server-owned fields
    (``id``, ``user_id``, timestamps) is overwritten."""
    body.id = str(uuid.uuid4())
    body.user_id = user_id
    body.created_at = body.updated_at = _now()
    dump = body.model_dump(mode="json")
    _journeys(user_id).document(body.id).set(dump)
    log.info(
        "journey.created",
        user_id=user_id,
        journey_id=body.id,
        source=body.source,
        stages=len(body.stages),
    )
    return dump


@router.put("/journeys/{journey_id}")
def save_journey(
    journey_id: str, body: Journey, user_id: str = Depends(verify_user)
) -> dict:
    """Replace a journey wholesale — a stage the client dropped must disappear,
    so this is ``set`` without ``merge``. A foreign id is a 404 by construction:
    the reference is always built under the caller's uid."""
    ref = _journeys(user_id).document(journey_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="journey not found")
    existing = snap.to_dict() or {}
    body.id = journey_id
    body.user_id = user_id
    # Re-validate so the stored ISO string comes back as a datetime; assigning
    # the raw string would make model_dump warn on every save.
    body.created_at = Journey.model_validate(existing).created_at
    body.updated_at = _now()
    dump = body.model_dump(mode="json")
    ref.set(dump)
    log.info(
        "journey.saved",
        user_id=user_id,
        journey_id=journey_id,
        outcome=body.outcome,
        stages=len(body.stages),
    )
    return dump


@router.delete("/journeys/{journey_id}")
def delete_journey(journey_id: str, user_id: str = Depends(verify_user)) -> dict:
    ref = _journeys(user_id).document(journey_id)
    if not ref.get().exists:
        raise HTTPException(status_code=404, detail="journey not found")
    ref.delete()
    log.info("journey.deleted", user_id=user_id, journey_id=journey_id)
    return {"ok": True}
