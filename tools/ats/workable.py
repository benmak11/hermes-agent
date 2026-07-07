# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Workable widget API.

https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true
Public endpoint, no auth required. Returns {"name", "description", "jobs"};
``details=true`` inlines each job's full HTML description.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from models.job import Job
from tools.ats._http import fetch_board_json
from tools.text import html_to_text

BASE = "https://apply.workable.com/api/v1/widget/accounts"


def _location(raw: dict) -> str | None:
    parts = [raw.get("city"), raw.get("state"), raw.get("country")]
    loc = ", ".join(p for p in parts if p)
    if raw.get("telecommuting"):
        return f"Remote{f' — {loc}' if loc else ''}"
    return loc or None


async def fetch_workable_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch all open jobs for a Workable-hosted company.

    Args:
        company_slug: The Workable account, e.g. 'blueground' for
            apply.workable.com/blueground
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on Workable
    """
    url = f"{BASE}/{company_slug}?details=true"
    data = await fetch_board_json("workable", company_slug, url)
    if data is None:  # company not on Workable, or fetch failed (logged)
        return []
    jobs: list[Job] = []
    for raw in data.get("jobs", []):
        source_id = str(raw.get("shortcode", ""))
        if not source_id:
            continue
        job_id = hashlib.sha256(f"workable:{source_id}".encode()).hexdigest()[:16]
        jobs.append(
            Job(
                id=job_id,
                user_id=user_id,
                source="workable",
                source_id=source_id,
                company=company_slug,
                title=raw.get("title", ""),
                url=raw.get("url") or f"https://apply.workable.com/j/{source_id}",
                location=_location(raw),
                jd_raw=html_to_text(raw.get("description", "")),
                discovered_at=datetime.now(UTC),
            )
        )
    return jobs
