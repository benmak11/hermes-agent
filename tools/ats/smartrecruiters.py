# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""SmartRecruiters Posting API.

https://api.smartrecruiters.com/v1/companies/{slug}/postings
Public endpoint, no auth required. Unlike the other boards the listing has no
job description — each posting needs a detail fetch (jobAd.sections), so this
fetcher is N+1 with a concurrency cap. Enterprise boards can be huge (Bosch:
~4700 postings, newest first), so the fetch is capped at MAX_POSTINGS.

Note: company identifiers are case-sensitive (e.g. 'BoschGroup').
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

from models.job import Job
from obs.logging import get_logger
from tools.ats._http import fetch_board_json
from tools.text import html_to_text

log = get_logger("tools.ats")

BASE = "https://api.smartrecruiters.com/v1/companies"

PAGE_SIZE = 100
# Postings are returned newest-first; cap so an enterprise board doesn't turn
# discovery into thousands of detail fetches.
MAX_POSTINGS = 300
DETAIL_CONCURRENCY = 10


def _location(raw: dict) -> str | None:
    loc = raw.get("location") or {}
    parts = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
    return ", ".join(p for p in parts if p) or None


def _jd_text(detail: dict) -> str:
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = []
    for section in sections.values():
        if isinstance(section, dict) and section.get("text"):
            parts.append(html_to_text(section["text"]))
    return "\n\n".join(parts)


async def fetch_smartrecruiters_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch open jobs (newest MAX_POSTINGS) for a SmartRecruiters company.

    Args:
        company_slug: The SmartRecruiters company identifier (case-sensitive),
            e.g. 'BoschGroup' for jobs.smartrecruiters.com/BoschGroup
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on SmartRecruiters
    """
    postings: list[dict] = []
    offset = 0
    while len(postings) < MAX_POSTINGS:
        url = f"{BASE}/{company_slug}/postings?limit={PAGE_SIZE}&offset={offset}"
        data = await fetch_board_json("smartrecruiters", company_slug, url)
        if data is None:  # not on SmartRecruiters, or fetch failed (logged)
            return []
        page = data.get("content", [])
        postings.extend(page)
        offset += PAGE_SIZE
        if len(page) < PAGE_SIZE or offset >= data.get("totalFound", 0):
            break
    postings = postings[:MAX_POSTINGS]

    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def _to_job(raw: dict) -> Job | None:
        source_id = str(raw.get("id", ""))
        if not source_id:
            return None
        async with sem:
            detail = await fetch_board_json(
                "smartrecruiters",
                company_slug,
                f"{BASE}/{company_slug}/postings/{source_id}",
            )
        if detail is None:  # posting vanished between list and detail — skip
            return None
        job_id = hashlib.sha256(
            f"smartrecruiters:{source_id}".encode()
        ).hexdigest()[:16]
        url = detail.get("postingUrl") or (
            f"https://jobs.smartrecruiters.com/{company_slug}/{source_id}"
        )
        return Job(
            id=job_id,
            user_id=user_id,
            source="smartrecruiters",
            source_id=source_id,
            company=company_slug,
            title=raw.get("name", ""),
            url=url,
            location=_location(raw),
            jd_raw=_jd_text(detail),
            discovered_at=datetime.now(UTC),
        )

    jobs = [j for j in await asyncio.gather(*(_to_job(p) for p in postings)) if j]
    if len(postings) == MAX_POSTINGS:
        log.info(
            "ats.smartrecruiters.capped",
            slug=company_slug,
            fetched=len(jobs),
            cap=MAX_POSTINGS,
        )
    return jobs
