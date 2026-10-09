# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Probe one ATS board: does it answer, how many jobs, and the company's name.

Used offline (the company sweep and ``cli.board_names``) and by the board
health probe round at the end of a discovery cycle, never on the request path. The jobs fetch goes through :func:`tools.ats._http.fetch_board_json`
inside a :func:`~tools.ats._http.capture_outcome` scope, so the outcome is
classified exactly as discovery classifies it. Both GETs use the pooled
client when the caller has opened a :func:`~tools.ats._http.board_client`.

A missing, generic or unparseable name is ``None``: a board with no name is
the one later tooling quarantines rather than reroutes.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

from obs.logging import get_logger
from tools.ats import ashby, greenhouse, lever
from tools.ats._http import ERROR, OK, capture_outcome, fetch_board_json
from tools.ats._http import _get_with_retry as get_with_retry

log = get_logger("tools.ats.probe")

PROBED_PLATFORMS = ("greenhouse", "lever", "ashby")

#: The hosted board pages whose ``<title>`` carries the company name. Lever's
#: and Ashby's posting APIs have no name field.
LEVER_PAGE = "https://jobs.lever.co"
ASHBY_PAGE = "https://jobs.ashbyhq.com"

#: Titles that name the platform or the page, not the company.
_GENERIC_NAMES = frozenset(
    {"", "jobs", "careers", "job board", "lever", "ashby", "greenhouse", "error"}
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_ASHBY_SUFFIX_RE = re.compile(r"\s+jobs$", re.IGNORECASE)


@dataclass(frozen=True)
class BoardProbe:
    """What one board answered. ``job_count`` is ``None`` unless ``outcome`` is ok."""

    outcome: str
    status: int | None
    job_count: int | None
    name: str | None


def _clean_name(raw: Any) -> str | None:
    """Unescape and collapse whitespace; a generic or empty name is ``None``."""
    if not isinstance(raw, str):
        return None
    name = " ".join(html.unescape(raw).split())
    return None if name.casefold() in _GENERIC_NAMES else name


def parse_greenhouse_name(data: Any) -> str | None:
    """The ``name`` field of Greenhouse's board JSON."""
    return _clean_name(data.get("name")) if isinstance(data, dict) else None


def parse_page_title(page: str) -> str | None:
    """The page's ``<title>``, cleaned; ``None`` when absent or generic."""
    match = _TITLE_RE.search(page)
    return _clean_name(match.group(1)) if match else None


def parse_lever_name(page: str) -> str | None:
    """Lever's hosted board titles the page with the bare company name."""
    return parse_page_title(page)


def parse_ashby_name(page: str) -> str | None:
    """Ashby's hosted board titles the page "<Company> Jobs"; the suffix is dropped."""
    title = parse_page_title(page)
    if title is None:
        return None
    return _clean_name(_ASHBY_SUFFIX_RE.sub("", title))


def _jobs_url(platform: str, slug: str) -> str:
    # Greenhouse drops ``content=true``: the count needs no descriptions, and
    # the status is the same endpoint's answer either way.
    if platform == "greenhouse":
        return f"{greenhouse.BASE}/{slug}/jobs"
    if platform == "lever":
        return f"{lever.BASE}/{slug}?mode=json"
    return f"{ashby.BASE}/{slug}?includeCompensation=true"


def _count_jobs(platform: str, data: Any) -> int | None:
    if platform == "lever":
        return len(data) if isinstance(data, list) else None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return len(jobs) if isinstance(jobs, list) else None


async def _fetch_name(platform: str, slug: str) -> str | None:
    """The company's name for a board known to exist; ``None`` on any failure."""
    url = {
        "greenhouse": f"{greenhouse.BASE}/{slug}",
        "lever": f"{LEVER_PAGE}/{slug}",
        "ashby": f"{ASHBY_PAGE}/{slug}",
    }[platform]
    try:
        response = await get_with_retry(url)
        if platform == "greenhouse":
            return parse_greenhouse_name(response.json())
        if platform == "lever":
            return parse_lever_name(response.text)
        return parse_ashby_name(response.text)
    except (httpx.HTTPError, ValueError) as e:
        log.info(
            "ats.probe.name_failed",
            platform=platform,
            slug=slug,
            error=f"{type(e).__name__}: {e}",
        )
        return None


async def probe_board(platform: str, slug: str) -> BoardProbe:
    """Fetch a board's jobs and, if it answered, its company name.

    Never raises on an HTTP problem; that comes back as the outcome. A body
    that is not JSON is ``error``. Raises ``ValueError`` for a platform other
    than greenhouse, lever or ashby.
    """
    if platform not in PROBED_PLATFORMS:
        raise ValueError(f"cannot probe a {platform!r} board")
    with capture_outcome() as seen:
        try:
            data = await fetch_board_json(platform, slug, _jobs_url(platform, slug))
        except ValueError:
            return BoardProbe(ERROR, None, None, None)
    outcome = seen.outcome or ERROR
    if outcome != OK:
        return BoardProbe(outcome, seen.status, None, None)
    return BoardProbe(
        outcome,
        seen.status,
        _count_jobs(platform, data),
        await _fetch_name(platform, slug),
    )
