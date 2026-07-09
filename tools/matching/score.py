# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Score pending, unscored jobs and persist the results.

Extracted from ``cli/run_matching.py`` so the auto-discovery scheduler and the
CLI share one implementation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.match import JobMatch
from models.profile import MasterProfile
from obs.logging import get_logger
from tools.matching.pipeline import match_job

log = get_logger("tools.matching")

# (job, match, error) — match is None when scoring failed and error says why.
OnResult = Callable[[Job, JobMatch | None, str | None], None]

# Jobs scoring at or below this never stay in the `jobs` collection: 0 is the
# out-of-family sentinel and the matching prompt caps geographically
# ineligible roles at 20, so everything down here is a job the user cannot or
# would not take. (The UI already hides anything under 60.)
DISCARD_AT_OR_BELOW = 20


def should_discard(match: JobMatch) -> bool:
    """True when the job is not worth keeping in the user's jobs collection."""
    return match.overall_score <= DISCARD_AT_OR_BELOW


def discard_tombstone(job: Job, match: JobMatch) -> dict:
    """Minimal `discarded_jobs` record.

    Exists so discovery's seen-check still recognizes the posting and never
    re-persists (and re-pays Flash/Pro to re-score) it while it stays live on
    the board.
    """
    return {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "score": match.overall_score,
        "recommendation": match.recommendation,
        "reasoning": match.reasoning,
        "discarded_at": datetime.now(UTC).isoformat(),
    }


async def score_pending_jobs(
    user_id: str,
    *,
    limit: int | None = None,
    concurrency: int = 5,
    on_result: OnResult | None = None,
) -> dict:
    """Score every pending, unscored job against the user's profile.

    Persists ``match`` (and the parsed JD) onto each job doc — unless the job
    scores at/below ``DISCARD_AT_OR_BELOW``, in which case the doc is replaced
    by a tombstone in ``discarded_jobs`` so it never reaches the queue but is
    still deduped on future discovery runs. Returns ``{"scored": n,
    "discarded": n, "failed": n, "pending": n}``. Raises ``ValueError`` when
    the user has no profile to match against.
    """
    db = firestore.AsyncClient()

    profile_doc = await db.collection("users").document(user_id).get()
    if not profile_doc.exists:
        raise ValueError(f"No profile at users/{user_id}.")
    profile = MasterProfile.model_validate(profile_doc.to_dict())

    jobs_ref = db.collection("users").document(user_id).collection("jobs")
    query = jobs_ref.where(filter=FieldFilter("user_decision", "==", "pending"))

    pending: list[tuple] = []
    async for snap in query.stream():
        d = snap.to_dict()
        if "match" in d:  # already scored
            continue
        pending.append((snap.reference, Job.model_validate(d)))
        if limit and len(pending) >= limit:
            break

    started = time.monotonic()
    log.info("matching.start", pending=len(pending), concurrency=concurrency)
    sem = asyncio.Semaphore(concurrency)
    counts = {"scored": 0, "discarded": 0, "failed": 0, "pending": len(pending)}

    async def _score(ref, job: Job) -> None:
        async with sem:
            try:
                match = await match_job(job, profile)
                if should_discard(match):
                    # ref.parent is the jobs collection; its parent is the
                    # user doc.
                    user_ref = ref.parent.parent
                    await user_ref.collection("discarded_jobs").document(job.id).set(
                        discard_tombstone(job, match)
                    )
                    await ref.delete()
                    counts["discarded"] += 1
                    log.info(
                        "matching.discarded",
                        job_id=job.id,
                        company=job.company,
                        score=match.overall_score,
                    )
                    if on_result:
                        on_result(job, match, None)
                    return
                await ref.update(
                    {
                        "match": match.model_dump(mode="json"),
                        "jd_parsed": (
                            job.jd_parsed.model_dump(mode="json")
                            if job.jd_parsed
                            else None
                        ),
                    }
                )
                counts["scored"] += 1
                if on_result:
                    on_result(job, match, None)
            except Exception as e:
                counts["failed"] += 1
                log.exception("match.failed", job_id=job.id, company=job.company)
                if on_result:
                    on_result(job, None, str(e))

    await asyncio.gather(*(_score(ref, job) for ref, job in pending))
    log.info(
        "matching.done",
        duration_ms=int((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
