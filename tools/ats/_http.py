# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared HTTP fetch for the ATS board APIs, with failure-visible logging.

The fetchers used to swallow every ``httpx.HTTPError`` and return ``[]``,
which made a rate-limited/broken board indistinguishable from an empty one in
the discovery summary. This helper keeps the "missing board is not an error"
contract (callers still get ``None`` → empty list) but logs *why* a board
returned nothing, so failure points show up in Cloud Logging.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from obs.logging import get_logger

log = get_logger("tools.ats")


async def fetch_board_json(platform: str, slug: str, url: str) -> Any | None:
    """GET a public board API and return the parsed JSON, or ``None`` on failure.

    - 404 → the slug is not on this platform (or the board was taken down);
      logged at info since discovery routinely probes stale unvetted slugs.
    - any other HTTP status / transport error (timeout, DNS, 429, 5xx) →
      logged at warning: these are the real failure points to watch.
    """
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        level = log.info if status == 404 else log.warning
        level(
            "ats.fetch.failed",
            platform=platform,
            slug=slug,
            status=status,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return None
    except httpx.HTTPError as e:
        log.warning(
            "ats.fetch.failed",
            platform=platform,
            slug=slug,
            error=f"{type(e).__name__}: {e}",
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return None

    data = response.json()
    log.debug(
        "ats.fetch.ok",
        platform=platform,
        slug=slug,
        duration_ms=round((time.perf_counter() - start) * 1000, 1),
    )
    return data
