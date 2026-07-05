# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Greenhouse Job Board API.

https://developers.greenhouse.io/job-board.html
Public endpoint, no auth required. Returns all jobs for a company.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from models.job import Job
from tools.ats._http import fetch_board_json
from tools.text import html_to_text

BASE = "https://boards-api.greenhouse.io/v1/boards"


async def fetch_greenhouse_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch all open jobs for a Greenhouse-hosted company.

    Args:
        company_slug: The Greenhouse board name, e.g. 'stripe' for stripe.com/jobs
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on Greenhouse
    """
    url = f"{BASE}/{company_slug}/jobs?content=true"
    data = await fetch_board_json("greenhouse", company_slug, url)
    if data is None:  # company not on Greenhouse, or fetch failed (logged)
        return []
    jobs: list[Job] = []
    for raw in data.get("jobs", []):
        source_id = str(raw["id"])
        job_id = hashlib.sha256(f"greenhouse:{source_id}".encode()).hexdigest()[:16]
        location = raw.get("location") or {}
        jobs.append(
            Job(
                id=job_id,
                user_id=user_id,
                source="greenhouse",
                source_id=source_id,
                company=company_slug,
                title=raw["title"],
                url=raw["absolute_url"],
                location=location.get("name"),
                jd_raw=html_to_text(raw.get("content", "")),
                discovered_at=datetime.now(UTC),
            )
        )
    return jobs
