# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Recruitee careers-site API.

https://{slug}.recruitee.com/api/offers/
Public endpoint, no auth required. Returns {"offers": [...]} with the full
HTML description and requirements inline.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from models.job import Job
from tools.ats._http import fetch_board_json
from tools.text import html_to_text


def board_url(company_slug: str) -> str:
    return f"https://{company_slug}.recruitee.com/api/offers/"


async def fetch_recruitee_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch all open jobs for a Recruitee-hosted company.

    Args:
        company_slug: The Recruitee subdomain, e.g. 'sendcloud' for
            sendcloud.recruitee.com
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on Recruitee
    """
    data = await fetch_board_json("recruitee", company_slug, board_url(company_slug))
    if data is None:  # company not on Recruitee, or fetch failed (logged)
        return []
    jobs: list[Job] = []
    for raw in data.get("offers", []):
        source_id = str(raw.get("id", ""))
        if not source_id:
            continue
        job_id = hashlib.sha256(f"recruitee:{source_id}".encode()).hexdigest()[:16]
        # Description and requirements are separate HTML blobs — the JD is both.
        jd_html = (raw.get("description") or "") + (raw.get("requirements") or "")
        jobs.append(
            Job(
                id=job_id,
                user_id=user_id,
                source="recruitee",
                source_id=source_id,
                company=company_slug,
                title=raw.get("title", ""),
                url=raw.get("careers_url")
                or f"https://{company_slug}.recruitee.com/o/{raw.get('slug', '')}",
                location=raw.get("location"),
                jd_raw=html_to_text(jd_html),
                discovered_at=datetime.now(UTC),
            )
        )
    return jobs
