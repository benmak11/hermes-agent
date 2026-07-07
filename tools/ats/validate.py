# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Posting-liveness check used before acting on a job (tailoring/submission).

Postings die between discovery and submission. This asks the ATS whether the
posting still exists so the pipeline can dismiss it instead of tailoring a
resume for — or driving a browser at — a tombstone page.

The contract is **fail open**: only a definitive "gone" answer (404/410, or a
verified absence from a successfully fetched board listing) returns
``removed``. Timeouts, 5xx, rate limits, and malformed responses return
``unknown``, which callers must treat as live — a flaky board must never
mass-dismiss the pipeline; the submitter itself will fail visibly if the page
really is gone.

Per-platform strategy:
- greenhouse / lever / smartrecruiters expose per-posting endpoints (200/404);
- ashby / workable / recruitee only list the whole board, so we refetch it and
  look for the posting's id (``BOARD_URLS`` + ``live_ids``, also used by the
  batch sweep in ``sweep.py``);
- anything else falls back to probing ``job.url`` directly.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from models.job import Job
from obs.logging import get_logger
from tools.ats.ashby import BASE as ASHBY_BASE
from tools.ats.greenhouse import BASE as GREENHOUSE_BASE
from tools.ats.lever import BASE as LEVER_BASE
from tools.ats.recruitee import board_url as recruitee_board_url
from tools.ats.smartrecruiters import BASE as SMARTRECRUITERS_BASE
from tools.ats.workable import BASE as WORKABLE_BASE

log = get_logger("tools.ats")

Liveness = Literal["live", "removed", "unknown"]

# Statuses that definitively mean "this posting no longer exists".
_GONE = {404, 410}

# Board list endpoints for platforms checked by membership (one fetch covers
# every posting on the board). SmartRecruiters is deliberately absent: its
# listing is paginated, so membership in one page proves nothing — it has a
# per-posting endpoint instead.
BOARD_URLS = {
    "greenhouse": lambda slug: f"{GREENHOUSE_BASE}/{slug}/jobs",
    "lever": lambda slug: f"{LEVER_BASE}/{slug}?mode=json",
    "ashby": lambda slug: f"{ASHBY_BASE}/{slug}",
    "workable": lambda slug: f"{WORKABLE_BASE}/{slug}",
    "recruitee": recruitee_board_url,
}

# Platforms whose check_posting goes through the board listing (no reliable
# per-posting endpoint).
_MEMBERSHIP_SOURCES = {"ashby", "workable", "recruitee"}


def live_ids(platform: str, data: Any) -> set[str]:
    """Extract the set of live posting ids from a board API response."""
    if platform == "lever":  # lever returns a bare JSON array
        rows = data or []
    elif platform == "recruitee":
        rows = (data or {}).get("offers", [])
    else:  # greenhouse, ashby, workable wrap the list in {"jobs": [...]}
        rows = (data or {}).get("jobs", [])
    key = "shortcode" if platform == "workable" else "id"
    return {str(r.get(key)) for r in rows if r.get(key) is not None}


async def check_posting(
    job: Job, transport: httpx.AsyncBaseTransport | None = None
) -> Liveness:
    """Ask the job's ATS whether the posting is still up.

    ``transport`` is injectable for tests only.
    """
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, transport=transport
    ) as client:
        if job.source == "greenhouse":
            url = f"{GREENHOUSE_BASE}/{job.company}/jobs/{job.source_id}"
        elif job.source == "lever":
            url = f"{LEVER_BASE}/{job.company}/{job.source_id}"
        elif job.source == "smartrecruiters":
            url = f"{SMARTRECRUITERS_BASE}/{job.company}/postings/{job.source_id}"
        elif job.source in _MEMBERSHIP_SOURCES:
            return await _check_board_membership(client, job)
        else:
            url = job.url
        return await _probe(client, job, url)


async def _probe(client: httpx.AsyncClient, job: Job, url: str) -> Liveness:
    try:
        response = await client.get(url)
    except httpx.HTTPError as e:
        return _log_result(job, "unknown", error=f"{type(e).__name__}: {e}")
    if response.status_code in _GONE:
        return _log_result(job, "removed", status=response.status_code)
    if response.is_success:
        return _log_result(job, "live", status=response.status_code)
    return _log_result(job, "unknown", status=response.status_code)


async def _check_board_membership(client: httpx.AsyncClient, job: Job) -> Liveness:
    """No per-posting endpoint — check the board listing instead.

    A 404 board (org gone) counts as removed; any fetch/parse failure is
    ``unknown`` per the fail-open contract.
    """
    try:
        response = await client.get(BOARD_URLS[job.source](job.company))
    except httpx.HTTPError as e:
        return _log_result(job, "unknown", error=f"{type(e).__name__}: {e}")
    if response.status_code in _GONE:
        return _log_result(job, "removed", status=response.status_code)
    if not response.is_success:
        return _log_result(job, "unknown", status=response.status_code)
    try:
        listed = live_ids(job.source, response.json())
    except (ValueError, AttributeError):
        return _log_result(job, "unknown", error="unparseable board response")
    if job.source_id in listed:
        return _log_result(job, "live", status=response.status_code)
    return _log_result(job, "removed", reason="absent from board")


def _log_result(job: Job, result: Liveness, **fields: object) -> Liveness:
    level = {"live": log.debug, "removed": log.info, "unknown": log.warning}[result]
    level(
        f"ats.validate.{result}",
        source=job.source,
        company=job.company,
        source_id=job.source_id,
        **fields,
    )
    return result
