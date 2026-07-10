# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Task handlers for the hermes-worker service (Phase B).

Cloud Tasks pushes queued work here. The routes exist in every deployment of
the shared image but are enabled only where ``WORKER_MODE`` is on — on the
public hermes-api service they 404, so the only way in is through the private
worker service, where Cloud Run's platform-level OIDC has already
authenticated the caller (the ``hermes-tasks`` invoker SA).

Handlers run the work inline (not as background tasks) so the HTTP status
reflects the outcome and Cloud Tasks' retry policy applies to infrastructure
failures (e.g. instance death mid-run). Cycle functions swallow their own
work-level exceptions by design — a failed *cycle* waits for the next
scheduler tick rather than hot-retrying paid LLM calls.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.routes.discovery import run_discovery_cycle, run_sweep_cycle
from obs.logging import get_logger
from tools.matching.score import score_pending_jobs
from tools.queues import worker_mode

log = get_logger("api.worker")

router = APIRouter(prefix="/tasks", tags=["worker"])


class CycleTask(BaseModel):
    user_id: str
    trigger: str = "queued"


class ScoreTask(BaseModel):
    user_id: str
    limit: int | None = None


def _require_worker() -> None:
    if not worker_mode():
        # 404 (not 403): on the public API service these routes don't exist.
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/discovery")
async def task_discovery(body: CycleTask) -> dict:
    """Full discovery cycle: fetch -> filter -> persist -> score."""
    _require_worker()
    await run_discovery_cycle(body.user_id, trigger=body.trigger)
    return {"ok": True}


@router.post("/sweep")
async def task_sweep(body: CycleTask) -> dict:
    """Liveness sweep: dismiss postings the ATS took down."""
    _require_worker()
    await run_sweep_cycle(body.user_id, trigger=body.trigger)
    return {"ok": True}


@router.post("/score")
async def task_score(body: ScoreTask) -> dict:
    """Standalone scoring of pending jobs (online mode)."""
    _require_worker()
    counts = await score_pending_jobs(body.user_id, limit=body.limit)
    return {"ok": True, **counts}
