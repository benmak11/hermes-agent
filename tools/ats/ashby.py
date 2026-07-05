# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Ashby Job Board Posting API.

https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
Public endpoint, no auth required. Returns {"jobs": [...]}.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from models.job import Job
from tools.ats._http import fetch_board_json
from tools.text import html_to_text

BASE = "https://api.ashbyhq.com/posting-api/job-board"


async def fetch_ashby_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch all open jobs for an Ashby-hosted company.

    Args:
        company_slug: The Ashby org name, e.g. 'ramp' for jobs.ashbyhq.com/ramp
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on Ashby
    """
    url = f"{BASE}/{company_slug}?includeCompensation=true"
    data = await fetch_board_json("ashby", company_slug, url)
    if data is None:  # company not on Ashby, or fetch failed (logged)
        return []
    jobs: list[Job] = []
    for raw in data.get("jobs", []):
        source_id = str(raw.get("id", ""))
        if not source_id:
            continue
        job_id = hashlib.sha256(f"ashby:{source_id}".encode()).hexdigest()[:16]
        # Prefer the plain-text description; fall back to stripping the HTML one.
        description = raw.get("descriptionPlain") or raw.get("descriptionHtml") or ""
        jobs.append(
            Job(
                id=job_id,
                user_id=user_id,
                source="ashby",
                source_id=source_id,
                company=company_slug,
                title=raw.get("title", ""),
                url=raw.get("jobUrl") or raw.get("applyUrl", ""),
                location=raw.get("location"),
                jd_raw=html_to_text(description),
                discovered_at=datetime.now(UTC),
            )
        )
    return jobs
