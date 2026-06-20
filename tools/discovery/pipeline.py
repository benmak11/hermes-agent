# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deterministic discovery pipeline.

Fetches all jobs from all configured sources for every known + unvetted company,
then persists only previously-unseen jobs to Firestore. This is the cron-driven
engine (run via cli/run_discovery.py); it is intentionally not an LLM agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from google.cloud import firestore

from models.job import Job
from tools.ats.ashby import fetch_ashby_jobs
from tools.ats.greenhouse import fetch_greenhouse_jobs
from tools.ats.lever import fetch_lever_jobs
from tools.companies import Platform, all_active_companies

log = structlog.get_logger()

# Dispatch table — keeps the fetcher choice data-driven, not a giant if/elif.
FETCHERS: dict[Platform, Callable[[str, str], Awaitable[list[Job]]]] = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}


async def _fetch_with_meta(fetcher, slug, user_id, platform, source):
    """Wrapper that returns metadata alongside the fetch result."""
    try:
        jobs = await fetcher(slug, user_id)
        return (platform, slug, source, jobs)
    except Exception as e:
        # Re-raise so gather captures it; we only get here on programmer errors.
        raise RuntimeError(f"{platform}/{slug}: {e}") from e


async def run_discovery(user_id: str) -> dict:
    """Fetch all jobs from all sources for all known + unvetted companies.

    Returns a summary dict for SLI tracking later.
    """
    companies = all_active_companies()
    log.info("discovery.start", company_count=len(companies))

    tasks = [
        _fetch_with_meta(FETCHERS[platform], slug, user_id, platform, source)
        for platform, slug, source in companies
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    jobs: list[Job] = []
    failures: list[dict] = []
    empty_boards: list[dict] = []

    for result in results:
        if isinstance(result, Exception):
            log.error("discovery.fetch_exception", error=str(result))
            failures.append({"error": str(result)})
            continue
        platform, slug, source, fetched = result
        if not fetched:
            empty_boards.append({"platform": platform, "slug": slug, "source": source})
            continue
        # Attach provenance to each job (useful in matching + UI).
        for j in fetched:
            j.discovered_via = source
        jobs.extend(fetched)

    log.info(
        "discovery.complete",
        jobs_fetched=len(jobs),
        failures=len(failures),
        empty_boards=len(empty_boards),
    )
    return {"jobs": jobs, "failures": failures, "empty_boards": empty_boards}


async def persist_new_jobs(jobs: list[Job], concurrency: int = 20) -> int:
    """Write only previously-unseen jobs to Firestore. Returns count of new jobs."""
    # De-dupe within this run (a slug can appear in both known + unvetted).
    unique = {j.id: j for j in jobs}
    db = firestore.AsyncClient()
    sem = asyncio.Semaphore(concurrency)
    counter = {"new": 0}

    async def _persist_one(job: Job) -> None:
        async with sem:
            doc_ref = (
                db.collection("users")
                .document(job.user_id)
                .collection("jobs")
                .document(job.id)
            )
            snap = await doc_ref.get()
            if snap.exists:
                return
            await doc_ref.set(job.model_dump(mode="json"))
            counter["new"] += 1

    await asyncio.gather(*(_persist_one(j) for j in unique.values()))
    return counter["new"]
