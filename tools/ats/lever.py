# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Lever Postings API.

https://api.lever.co/v0/postings/{company}?mode=json
Public endpoint, no auth required. Returns a JSON array of postings.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx

from models.job import Job
from tools.text import html_to_text

BASE = "https://api.lever.co/v0/postings"


async def fetch_lever_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch all open jobs for a Lever-hosted company.

    Args:
        company_slug: The Lever account name, e.g. 'netflix' for jobs.lever.co/netflix
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on Lever
    """
    url = f"{BASE}/{company_slug}?mode=json"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return []  # company not on Lever, or board is private

    data = response.json()  # list of postings
    jobs: list[Job] = []
    for raw in data:
        source_id = str(raw.get("id", ""))
        if not source_id:
            continue
        job_id = hashlib.sha256(f"lever:{source_id}".encode()).hexdigest()[:16]
        categories = raw.get("categories") or {}
        # Prefer the plain-text description; fall back to stripping the HTML one.
        description = raw.get("descriptionPlain") or raw.get("description") or ""
        jobs.append(
            Job(
                id=job_id,
                user_id=user_id,
                source="lever",
                source_id=source_id,
                company=company_slug,
                title=raw.get("text", ""),
                url=raw.get("hostedUrl") or raw.get("applyUrl", ""),
                location=categories.get("location"),
                jd_raw=html_to_text(description),
                discovered_at=datetime.now(UTC),
            )
        )
    return jobs
