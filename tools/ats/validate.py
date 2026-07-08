# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Posting-liveness check used before acting on a job (tailoring/submission).

Postings die between discovery and submission. This asks the ATS whether the
posting still exists so the pipeline can dismiss it instead of tailoring a
resume for — or driving a browser at — a tombstone page.

The contract is **fail open**: only a definitive "gone" answer (404/410, or a
verified absence from a successfully fetched Ashby board) returns ``removed``.
Timeouts, 5xx, rate limits, and malformed responses return ``unknown``, which
callers must treat as live — a flaky board must never mass-dismiss the
pipeline; the submitter itself will fail visibly if the page really is gone.
"""

from __future__ import annotations

from typing import Literal

import httpx

from models.job import Job
from obs.logging import get_logger
from tools.ats.ashby import BASE as ASHBY_BASE
from tools.ats.google_jobs import BASE as GOOGLE_BASE
from tools.ats.google_jobs import HEADERS as GOOGLE_HEADERS
from tools.ats.google_jobs import extract_ds
from tools.ats.greenhouse import BASE as GREENHOUSE_BASE
from tools.ats.lever import BASE as LEVER_BASE

log = get_logger("tools.ats")

Liveness = Literal["live", "removed", "unknown"]

# Statuses that definitively mean "this posting no longer exists".
_GONE = {404, 410}


async def check_posting(
    job: Job, transport: httpx.AsyncBaseTransport | None = None
) -> Liveness:
    """Ask the job's ATS whether the posting is still up.

    Greenhouse and Lever expose per-posting endpoints; Ashby only lists the
    whole board, so we refetch it and look for the posting's id. Google
    Careers always answers 200, so its detail page's embedded data blob is
    inspected instead. Anything else — including meta_jobs, whose dead
    postings redirect to a real 404 — falls back to probing ``job.url``.

    ``transport`` is injectable for tests only.
    """
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, transport=transport
    ) as client:
        if job.source == "greenhouse":
            url = f"{GREENHOUSE_BASE}/{job.company}/jobs/{job.source_id}"
        elif job.source == "lever":
            url = f"{LEVER_BASE}/{job.company}/{job.source_id}"
        elif job.source == "ashby":
            return await _check_ashby_board(client, job)
        elif job.source == "google_jobs":
            return await _check_google_detail(client, job)
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


async def _check_ashby_board(client: httpx.AsyncClient, job: Job) -> Liveness:
    """Ashby has no per-posting endpoint — check the board listing instead.

    A 404 board (org gone) counts as removed; any fetch/parse failure is
    ``unknown`` per the fail-open contract.
    """
    try:
        response = await client.get(f"{ASHBY_BASE}/{job.company}")
    except httpx.HTTPError as e:
        return _log_result(job, "unknown", error=f"{type(e).__name__}: {e}")
    if response.status_code in _GONE:
        return _log_result(job, "removed", status=response.status_code)
    if not response.is_success:
        return _log_result(job, "unknown", status=response.status_code)
    try:
        listed = {str(raw.get("id")) for raw in response.json().get("jobs", [])}
    except (ValueError, AttributeError):
        return _log_result(job, "unknown", error="unparseable board response")
    if job.source_id in listed:
        return _log_result(job, "live", status=response.status_code)
    return _log_result(job, "removed", reason="absent from board")


async def _check_google_detail(client: httpx.AsyncClient, job: Job) -> Liveness:
    """Google Careers soft-404s: dead ids still render 200 with a generic
    "Jobs search" page. The discriminator is the ``ds:0`` data blob — a live
    detail page embeds the job record (``[["<id>", ...]]``); a dead one embeds
    an ErrorDetails structure. Only a parsed, mismatching blob counts as
    removed; anything unparseable is ``unknown`` per the fail-open contract.
    """
    try:
        response = await client.get(
            f"{GOOGLE_BASE}/{job.source_id}", headers=GOOGLE_HEADERS
        )
    except httpx.HTTPError as e:
        return _log_result(job, "unknown", error=f"{type(e).__name__}: {e}")
    if response.status_code in _GONE:
        return _log_result(job, "removed", status=response.status_code)
    if not response.is_success:
        return _log_result(job, "unknown", status=response.status_code)
    try:
        ds0 = extract_ds(response.text, "ds:0")
    except ValueError:
        ds0 = None
    if ds0 is None:
        return _log_result(job, "unknown", error="ds:0 blob missing/unparseable")
    if ds0 and isinstance(ds0[0], list) and ds0[0] and str(ds0[0][0]) == job.source_id:
        return _log_result(job, "live", status=response.status_code)
    return _log_result(job, "removed", reason="detail page has no job record")


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
