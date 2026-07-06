# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Liveness sweep: invalidate already-discovered postings that were taken down.

Board-batched — one list fetch per (platform, company) covers every swept job
on that board, instead of a per-job probe. Same fail-open contract as
``validate.py``: a board that fails to fetch is skipped entirely; only verified
absence from a successfully fetched board (or a definitive 404/410 for
non-board sources) dismisses anything.

Dismissal drops the job from the review queue and shelves
(``user_decision: dismissed``); an approved job whose application hasn't been
submitted yet also flips to the terminal ``posting_removed`` status so the
tracking page stops serving it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore

from models.job import Job
from obs.logging import get_logger
from tools.ats._http import fetch_board_json
from tools.ats.ashby import BASE as ASHBY_BASE
from tools.ats.greenhouse import BASE as GREENHOUSE_BASE
from tools.ats.lever import BASE as LEVER_BASE
from tools.ats.validate import check_posting
from tools.tailoring.pipeline import application_id

log = get_logger("tools.ats")

# Decisions whose jobs are still served somewhere (queue, shelves, pipeline).
SWEEPABLE_DECISIONS = {"pending", "approved", "starred", "rejected"}
# Application statuses that are still pre-submission — safe to invalidate.
ACTIVE_APP_STATUSES = {"queued", "tailoring", "ready_for_review", "failed"}

BOARD_URLS = {
    "greenhouse": lambda slug: f"{GREENHOUSE_BASE}/{slug}/jobs",
    "lever": lambda slug: f"{LEVER_BASE}/{slug}?mode=json",
    "ashby": lambda slug: f"{ASHBY_BASE}/{slug}",
}


def live_ids(platform: str, data: Any) -> set[str]:
    """Extract the set of live posting ids from a board API response."""
    if platform == "lever":  # lever returns a bare JSON array
        rows = data or []
    else:  # greenhouse + ashby wrap the list in {"jobs": [...]}
        rows = (data or {}).get("jobs", [])
    return {str(r.get("id")) for r in rows if r.get("id") is not None}


async def sweep_postings(user_id: str) -> dict:
    """Re-check every still-served posting for this user; dismiss dead ones.

    Returns ``{"checked": n, "removed": n, "boards_failed": n}``.
    """
    db = firestore.Client()
    user_ref = db.collection("users").document(user_id)
    jobs_ref = user_ref.collection("jobs")

    jobs: list[Job] = []
    for snap in jobs_ref.stream():
        d = snap.to_dict()
        if d.get("user_decision") not in SWEEPABLE_DECISIONS:
            continue
        try:
            jobs.append(Job.model_validate(d))
        except Exception:  # legacy/malformed doc — don't let it kill the sweep
            log.warning("sweep.job_unparseable", job_id=snap.id)

    boards: dict[tuple[str, str], list[Job]] = {}
    singles: list[Job] = []
    for job in jobs:
        if job.source in BOARD_URLS:
            boards.setdefault((job.source, job.company), []).append(job)
        else:
            singles.append(job)

    counts = {"checked": 0, "removed": 0, "boards_failed": 0}
    dead: list[Job] = []
    sem = asyncio.Semaphore(10)

    async def _sweep_board(platform: str, slug: str, board_jobs: list[Job]) -> None:
        async with sem:
            data = await fetch_board_json(platform, slug, BOARD_URLS[platform](slug))
        if data is None:  # fetch failed — fail open, skip this board
            counts["boards_failed"] += 1
            return
        ids = live_ids(platform, data)
        for job in board_jobs:
            counts["checked"] += 1
            if job.source_id not in ids:
                dead.append(job)

    async def _sweep_single(job: Job) -> None:
        async with sem:
            result = await check_posting(job)
        counts["checked"] += 1
        if result == "removed":
            dead.append(job)

    await asyncio.gather(
        *(_sweep_board(p, s, js) for (p, s), js in boards.items()),
        *(_sweep_single(j) for j in singles),
    )

    now = datetime.now(UTC).isoformat()
    for job in dead:
        jobs_ref.document(job.id).update(
            {"user_decision": "dismissed", "posting_removed_at": now}
        )
        app_ref = user_ref.collection("applications").document(application_id(job.id))
        app_snap = app_ref.get()
        if app_snap.exists and app_snap.to_dict().get("status") in ACTIVE_APP_STATUSES:
            app_ref.set(
                {
                    "status": "posting_removed",
                    "timeline": firestore.ArrayUnion(
                        [
                            {
                                "at": now,
                                "status": "posting_removed",
                                "note": (
                                    f"posting no longer available at {job.url}"
                                    " — application dismissed (liveness sweep)"
                                ),
                            }
                        ]
                    ),
                },
                merge=True,
            )
        counts["removed"] += 1
        log.info(
            "sweep.posting_removed",
            job_id=job.id,
            source=job.source,
            company=job.company,
            decision_was=job.user_decision,
        )

    log.info("sweep.done", user_id=user_id, **counts)
    return counts
