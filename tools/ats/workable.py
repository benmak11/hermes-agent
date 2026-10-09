# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Workable widget API.

https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
Public endpoint, no auth required. Returns {"name": ..., "jobs": [...]};
``details=true`` adds each job's HTML ``description``. A missing account 404s.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from models.job import Job
from tools.ats._http import fetch_board_json
from tools.text import html_to_text

BASE = "https://apply.workable.com/api/v1/widget/accounts"


def build_location(raw: dict[str, Any]) -> str | None:
    """``City, State, Country`` with blanks dropped, prefixed ``Remote - `` if remote.

    A remote job with no place is ``Remote``; no place and not remote is ``None``.
    """
    parts = [
        value.strip()
        for value in (raw.get("city"), raw.get("state"), raw.get("country"))
        if isinstance(value, str) and value.strip()
    ]
    place = ", ".join(parts)
    if raw.get("telecommuting") is True:
        return f"Remote - {place}" if place else "Remote"
    return place or None


async def fetch_workable_jobs(company_slug: str, user_id: str) -> list[Job]:
    """Fetch all open jobs for a Workable-hosted company.

    Args:
        company_slug: The Workable account, e.g. 'acme' for apply.workable.com/acme
        user_id: The user this discovery run is for

    Returns:
        List of Job records, may be empty if the company isn't on Workable
    """
    url = f"{BASE}/{company_slug}?details=true"
    data = await fetch_board_json("workable", company_slug, url)
    if not isinstance(data, dict):  # not on Workable, fetch failed (logged), or junk
        return []
    jobs: list[Job] = []
    for raw in data.get("jobs") or []:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("shortcode") or "")
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
                title=raw.get("title") or "",
                url=raw.get("url")
                or raw.get("shortlink")
                or f"https://apply.workable.com/j/{source_id}",
                location=build_location(raw),
                jd_raw=html_to_text(raw.get("description") or ""),
                discovered_at=datetime.now(UTC),
            )
        )
    return jobs
