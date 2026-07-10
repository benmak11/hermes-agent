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
from tools.matching.pipeline import (
    create_match_cache,
    delete_match_cache,
    match_job,
)

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


async def load_profile_and_pending(
    db: firestore.AsyncClient, user_id: str, limit: int | None = None
) -> tuple[MasterProfile, list[tuple]]:
    """The user's profile plus their pending, unscored ``(doc_ref, Job)`` pairs.

    Shared by the online scorer below and the batch scorer in
    ``tools.matching.batch``. Raises ``ValueError`` when the user has no
    profile to match against.
    """
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
    return profile, pending


async def persist_result(ref, job: Job, match: JobMatch) -> str:
    """Persist one scoring outcome; returns ``"discarded"`` or ``"scored"``.

    Discarding replaces the job doc with a ``discarded_jobs`` tombstone (see
    :func:`discard_tombstone`); anything else writes ``match`` + the parsed JD
    onto the job doc.
    """
    if should_discard(match):
        # ref.parent is the jobs collection; its parent is the user doc.
        user_ref = ref.parent.parent
        await user_ref.collection("discarded_jobs").document(job.id).set(
            discard_tombstone(job, match)
        )
        await ref.delete()
        log.info(
            "matching.discarded",
            job_id=job.id,
            company=job.company,
            score=match.overall_score,
        )
        return "discarded"
    await ref.update(
        {
            "match": match.model_dump(mode="json"),
            "jd_parsed": (
                job.jd_parsed.model_dump(mode="json") if job.jd_parsed else None
            ),
        }
    )
    return "scored"


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
    profile, pending = await load_profile_and_pending(db, user_id, limit)

    started = time.monotonic()

    # One Vertex context cache for the static scoring block (profile + rules),
    # shared by every job in this run — the block dominates input tokens and
    # cached input bills at a tenth of the standard rate. TTL is scaled to the
    # backlog (~30s per Pro call per concurrency slot, capped at 24h);
    # match_job falls back to the uncached prompt if the cache expires
    # mid-run, and create_match_cache returning None (e.g. block under the
    # model's minimum cacheable size) just means the run prices like before.
    cache_name: str | None = None
    if len(pending) >= 2:
        ttl_seconds = min(max(3600, len(pending) * 30 // concurrency), 86_400)
        cache_name = await create_match_cache(profile, ttl_seconds=ttl_seconds)

    log.info(
        "matching.start",
        pending=len(pending),
        concurrency=concurrency,
        context_cache=cache_name is not None,
    )
    sem = asyncio.Semaphore(concurrency)
    counts = {"scored": 0, "discarded": 0, "failed": 0, "pending": len(pending)}

    async def _score(ref, job: Job) -> None:
        async with sem:
            try:
                match = await match_job(job, profile, cached_content=cache_name)
                outcome = await persist_result(ref, job, match)
                counts[outcome] += 1
                if on_result:
                    on_result(job, match, None)
            except Exception as e:
                counts["failed"] += 1
                log.exception("match.failed", job_id=job.id, company=job.company)
                if on_result:
                    on_result(job, None, str(e))

    try:
        await asyncio.gather(*(_score(ref, job) for ref, job in pending))
    finally:
        if cache_name:
            await delete_match_cache(cache_name)
    log.info(
        "matching.done",
        duration_ms=int((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
