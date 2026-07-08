# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Meta Careers (metacareers.com, not an ATS).

The job search is a GraphQL endpoint that works unauthenticated with the LSD
token embedded in the /jobsearch/ page. The listing carries no JD, so each
posting needs a detail query — same N+1 shape as SmartRecruiters, so the same
cap + bounded concurrency. Like ``google_jobs``, the "slug" is a *search
query*, and the company is always Meta.

``DOC_ID_*`` are Relay persisted-query ids scraped from Meta's JS bundles
(verified live 2026-07-07). They rotate when Meta redeploys; a rotation shows
up as ``ats.fetch.failed`` / empty boards, and the fix is re-scraping the ids
from the bundle (grep ``CareersJobSearchResultsDataQuery_candidate_portal``).

Liveness needs no special casing: a removed posting's URL 301s to
``/jobs/position-not-available/`` which returns a real 404, so the default
``check_posting`` URL probe handles it.
"""

from __future__ import annotations

import asyncio
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

SEARCH_PAGE = "https://www.metacareers.com/jobsearch/"
GRAPHQL = "https://www.metacareers.com/graphql"
DOC_ID_SEARCH = "27506805582236862"  # CareersJobSearchResultsDataQuery
DOC_ID_DETAIL = "27371134039243725"  # CandidatePortalJobDetailsViewQuery

MAX_POSTINGS = 300
DETAIL_CONCURRENCY = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    )
}


def job_url(source_id: str) -> str:
    return f"https://www.metacareers.com/jobs/{source_id}/"


def _html(field: Any) -> str:
    """Unwrap Meta's rich-text shapes (verified live 2026-07-07).

    ``description`` comes back as a *JSON-encoded string* ``'{"__html": ...}'``;
    the qualification/responsibility lists hold ``{"item": "..."}`` dicts; page
    embeds use plain ``{"__html": ...}`` dicts. Unwrap recursively until text.
    """
    if isinstance(field, str):
        if field.lstrip().startswith("{"):
            try:
                return _html(json.loads(field))
            except ValueError:
                return field
        return field
    if isinstance(field, dict):
        return _html(field.get("__html") or field.get("item") or "")
    return str(field or "")


def _jd_text(detail: dict) -> str:
    parts = [_html(detail.get("description"))]
    for section, items in (
        ("Responsibilities", detail.get("responsibilities")),
        ("Minimum Qualifications", detail.get("minimum_qualifications")),
        ("Preferred Qualifications", detail.get("preferred_qualifications")),
        ("Compensation", detail.get("public_compensation")),
    ):
        if items:
            body = "\n".join(f"- {_html(i)}" for i in items)
            parts.append(f"{section}:\n{body}")
    return html_to_text("\n\n".join(p for p in parts if p.strip()))


async def _lsd_token(client: httpx.AsyncClient) -> str | None:
    response = await client.get(SEARCH_PAGE)
    response.raise_for_status()
    m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', response.text)
    return m.group(1) if m else None


async def _graphql(
    client: httpx.AsyncClient, lsd: str, doc_id: str, variables: str
) -> dict:
    response = await client.post(
        GRAPHQL,
        headers={"x-fb-lsd": lsd},
        data={"lsd": lsd, "doc_id": doc_id, "variables": variables},
    )
    response.raise_for_status()
    return response.json()


async def fetch_meta_jobs(query: str, user_id: str) -> list[Job]:
    """Fetch Meta Careers postings matching ``query`` (with full JDs)."""
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers=HEADERS
    ) as client:
        try:
            lsd = await _lsd_token(client)
            if not lsd:
                raise ValueError("LSD token not found on /jobsearch/ page")
            variables = json.dumps(
                {
                    "search_input": {"q": query},
                    "isLoggedIn": False,
                    "viewasUserID": None,
                }
            )
            data = await _graphql(client, lsd, DOC_ID_SEARCH, variables)
            listings = data["data"]["job_search_with_featured_jobs"]["all_jobs"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning(
                "ats.fetch.failed",
                platform="meta_jobs",
                slug=query,
                error=f"{type(e).__name__}: {e}",
            )
            return []

        if len(listings) > MAX_POSTINGS:
            log.info(
                "ats.meta_jobs.capped",
                slug=query,
                total=len(listings),
                fetched=MAX_POSTINGS,
            )
            listings = listings[:MAX_POSTINGS]

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        jobs: list[Job] = []

        async def _fetch_detail(listing: dict) -> None:
            source_id = str(listing["id"])
            try:
                detail_vars = json.dumps(
                    {
                        "requisitionID": source_id,
                        "renderLoggedInView": False,
                        "viewasUserID": None,
                    }
                )
                async with sem:
                    data = await _graphql(client, lsd, DOC_ID_DETAIL, detail_vars)
                detail = data["data"]["xcp_requisition_job_description"]
                jd_raw = _jd_text(detail)
            except (httpx.HTTPError, KeyError, TypeError) as e:
                log.warning(
                    "ats.detail_fetch.failed",
                    platform="meta_jobs",
                    slug=query,
                    source_id=source_id,
                    error=f"{type(e).__name__}: {e}",
                )
                return
            jobs.append(
                Job(
                    id=hashlib.sha256(
                        f"meta_jobs:{source_id}".encode()
                    ).hexdigest()[:16],
                    user_id=user_id,
                    source="meta_jobs",
                    source_id=source_id,
                    company="Meta",
                    title=detail.get("title") or listing["title"],
                    url=job_url(source_id),
                    location="; ".join(detail.get("locations") or listing["locations"])
                    or None,
                    jd_raw=jd_raw,
                    discovered_at=datetime.now(UTC),
                )
            )

        await asyncio.gather(*(_fetch_detail(li) for li in listings))
    return jobs
