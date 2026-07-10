# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Google Careers (careers site, not an ATS).

Google renders https://www.google.com/about/careers/applications/jobs/results
server-side with the job data embedded in an ``AF_initDataCallback`` blob
(``ds:1`` on listing pages, ``ds:0`` on a job detail page). There is no public
JSON API — the old careers.google.com ``/api/v3/search`` returns 404 — so we
parse the blob, which is valid JSON once extracted.

Unlike the multi-tenant ATS fetchers, the "slug" here is a *search query*
(e.g. ``software engineer``); results are restricted to ``LOCATION``. Verified
live 2026-07-07: listing entry fields, total-count pagination, and the ds:0
live/removed discriminator on detail pages.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from models.job import Job
from obs.logging import get_logger
from tools.text import html_to_text

log = get_logger("tools.ats")

BASE = "https://www.google.com/about/careers/applications/jobs/results"
LOCATION = "United States"
PAGE_SIZE = 20
MAX_JOBS = 100  # pages are sequential fetches; cap the crawl per query

# The results page serves a consent interstitial to clients without a
# browser-looking User-Agent.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
}

# Listing-entry field offsets in the ds:1 blob (verified live).
_ID, _TITLE, _RESPONSIBILITIES, _QUALIFICATIONS = 0, 1, 3, 4
_COMPANY, _LOCATIONS, _DESCRIPTION = 7, 9, 10


def extract_ds(html: str, key: str) -> Any | None:
    """Pull one ``AF_initDataCallback({key: '<key>', ... data: [...]``` blob.

    The data payload is a JS array literal that is also valid JSON; find its
    start and walk brackets (string-aware) to the matching close.
    """
    m = re.search(
        rf"AF_initDataCallback\(\{{key: '{re.escape(key)}'.*?data:", html, re.S
    )
    if not m:
        return None
    i = html.index("[", m.end())
    depth, j = 0, i
    in_str = esc = False
    while j < len(html):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(html[i : j + 1])
        j += 1
    return None


def _job_from_entry(entry: list, query: str, user_id: str) -> Job:
    source_id = str(entry[_ID])
    job_id = hashlib.sha256(f"google_jobs:{source_id}".encode()).hexdigest()[:16]
    locations = [loc[0] for loc in (entry[_LOCATIONS] or []) if loc and loc[0]]
    jd_parts = [
        (entry[_DESCRIPTION] or [None, ""])[1],
        (entry[_QUALIFICATIONS] or [None, ""])[1],
        (entry[_RESPONSIBILITIES] or [None, ""])[1],
    ]
    return Job(
        id=job_id,
        user_id=user_id,
        source="google_jobs",
        source_id=source_id,
        company=entry[_COMPANY] or "Google",
        title=entry[_TITLE],
        url=f"{BASE}/{source_id}",
        location="; ".join(locations) or None,
        jd_raw=html_to_text("\n".join(p for p in jd_parts if p)),
        discovered_at=datetime.now(UTC),
    )


async def fetch_google_jobs(query: str, user_id: str) -> list[Job]:
    """Fetch US-located Google Careers postings matching ``query``."""
    jobs: list[Job] = []
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers=HEADERS
    ) as client:
        page, total = 1, None
        while len(jobs) < MAX_JOBS:
            params = {"q": f'"{query}"', "location": LOCATION, "page": page}
            try:
                response = await client.get(BASE, params=params)
                response.raise_for_status()
            except httpx.HTTPError as e:
                log.warning(
                    "ats.fetch.failed",
                    platform="google_jobs",
                    slug=query,
                    error=f"{type(e).__name__}: {e}",
                    page=page,
                )
                break  # keep whatever earlier pages returned
            data = extract_ds(response.text, "ds:1")
            if not data or not isinstance(data[0], list):
                log.warning(
                    "ats.fetch.failed",
                    platform="google_jobs",
                    slug=query,
                    error="ds:1 blob missing or unparseable",
                    page=page,
                )
                break
            for entry in data[0]:
                try:
                    jobs.append(_job_from_entry(entry, query, user_id))
                except Exception as e:
                    log.warning(
                        "ats.entry_unparseable",
                        platform="google_jobs",
                        slug=query,
                        error=f"{type(e).__name__}: {e}",
                    )
            total = int(data[2]) if len(data) > 2 and data[2] else 0
            if page * PAGE_SIZE >= min(total, MAX_JOBS) or not data[0]:
                break
            page += 1
    if total and total > MAX_JOBS:
        log.info("ats.google_jobs.capped", slug=query, total=total, fetched=len(jobs))
    return jobs[:MAX_JOBS]
