# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Google search -> company slug extraction.

Backend-agnostic: implement either DirectGoogleBackend or SerperBackend.
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import yaml

from obs.logging import get_logger
from tools.ats._http import NOT_FOUND, OK, board_client
from tools.ats.probe import BoardProbe, probe_board
from tools.companies import PLATFORMS, Platform, append_unvetted, new_unvetted_slugs

log = get_logger("tools.discovery.sweep")

# Greenhouse is mid-migration from boards.greenhouse.io to the new
# job-boards.greenhouse.io app; both host live postings, so sweep both.
PLATFORM_DOMAINS: dict[Platform, list[str]] = {
    "greenhouse": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
    "lever": ["jobs.lever.co"],
    "ashby": ["jobs.ashbyhq.com"],
    "workable": ["apply.workable.com"],
}

# URL patterns to extract company slug. Tested against real URLs from each
# platform. Greenhouse matches legacy, new, and EU (job-boards.eu) hosts.
SLUG_REGEXES: dict[Platform, re.Pattern] = {
    "greenhouse": re.compile(
        r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([a-zA-Z0-9_-]+)"
    ),
    "lever": re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"),
    "workable": re.compile(r"apply\.workable\.com/([a-zA-Z0-9_-]+)"),
}

# Platform-internal paths that look like slugs but aren't real companies.
# ``j`` is Workable's single-job short link (apply.workable.com/j/{shortcode}).
_NON_SLUGS = {"jobs", "search", "api", "v1", "boards", "j"}


class SearchBackend(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[str]:
        """Return list of result URLs."""
        ...


class SerperBackend(SearchBackend):
    """https://serper.dev — ~$0.30 per 1000 queries.

    NOTE: the free tier caps ``num`` at 10. Higher values return a 400 with the
    misleading message "Query pattern not allowed for free accounts" — it is the
    page size, not the site: operator, that is rejected. Paid plans allow up to
    100, so bump ``num`` if you upgrade.
    """

    def __init__(self, api_key: str, num: int = 10):
        self.api_key = api_key
        self.num = num

    async def search(self, query: str) -> list[str]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": self.api_key.strip(),
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": self.num},
            )
            if response.status_code != 200:
                # Surface Serper's actual message instead of a bare HTTP error.
                raise RuntimeError(
                    f"Serper {response.status_code} for query {query!r}: "
                    f"{response.text[:300]}"
                )
            data = response.json()
            return [r["link"] for r in data.get("organic", [])]


class DirectGoogleBackend(SearchBackend):
    """Free but fragile. Throttles itself."""

    def __init__(self):
        self._last_request = 0.0

    async def search(self, query: str) -> list[str]:
        # Throttle: at least 30s between queries.
        now = time.time()
        sleep_for = max(0, 30 - (now - self._last_request))
        if sleep_for:
            await asyncio.sleep(sleep_for)

        async with httpx.AsyncClient(
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            response = await client.get(
                f"https://www.google.com/search?q={quote_plus(query)}&num=100"
            )
            self._last_request = time.time()
            # Crude link extraction — works for now, may break if Google changes markup.
            return re.findall(r'href="(https?://[^"]+)"', response.text)


def extract_slugs(urls: list[str], platform: Platform) -> set[str]:
    """Pull company slugs out of search result URLs."""
    regex = SLUG_REGEXES[platform]
    slugs: set[str] = set()
    for url in urls:
        match = regex.search(url)
        if match:
            slug = match.group(1)
            if slug in _NON_SLUGS:
                continue
            slugs.add(slug)
    return slugs


#: Boards probed at once while vetting a sweep's new slugs.
_PROBE_CONCURRENCY = 10

#: Why a new slug was not added. ``failing`` is any other non-ok outcome.
REJECT_REASONS = ("not_found", "empty", "failing")


@dataclass
class SweepResult:
    """Per platform: slugs added, and slugs rejected by reason."""

    added: dict[Platform, int] = field(default_factory=dict)
    rejected: dict[Platform, dict[str, int]] = field(default_factory=dict)


def _reject_reason(probe: BoardProbe) -> str | None:
    """``None`` when the board may be added: it answered ok with jobs."""
    if probe.outcome == NOT_FOUND:
        return "not_found"
    if probe.outcome != OK or probe.job_count is None:
        return "failing"
    if probe.job_count < 1:
        return "empty"
    return None


async def vet_slugs_detailed(
    platform: Platform, slugs: list[str]
) -> tuple[list[tuple[str, str | None]], list[tuple[str, str]]]:
    """Probe ``slugs``; return the ``(slug, name)`` pairs to add and the
    ``(slug, reason)`` pairs rejected, both in ``slugs`` order.

    At most :data:`_PROBE_CONCURRENCY` boards are probed at once, through one
    pooled client.
    """
    if not slugs:
        return [], []
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(slug: str) -> BoardProbe:
        async with sem:
            return await probe_board(platform, slug)

    async with board_client():
        probes = await asyncio.gather(*(one(s) for s in slugs))

    accepted: list[tuple[str, str | None]] = []
    rejected: list[tuple[str, str]] = []
    for slug, probe in zip(slugs, probes, strict=True):
        reason = _reject_reason(probe)
        if reason is None:
            accepted.append((slug, probe.name))
            continue
        rejected.append((slug, reason))
        log.info(
            "sweep.slug_rejected",
            platform=platform,
            slug=slug,
            reason=reason,
            outcome=probe.outcome,
            status=probe.status,
        )
    return accepted, rejected


async def vet_slugs(
    platform: Platform, slugs: list[str]
) -> tuple[list[tuple[str, str | None]], dict[str, int]]:
    """:func:`vet_slugs_detailed` with rejections counted by reason."""
    accepted, rejections = await vet_slugs_detailed(platform, slugs)
    rejected = dict.fromkeys(REJECT_REASONS, 0)
    for _, reason in rejections:
        rejected[reason] += 1
    return accepted, rejected


async def run_sweep(backend: SearchBackend) -> SweepResult:
    """Run all configured queries against every platform in :data:`PLATFORM_DOMAINS`.

    A slug new to the pool is probed first and added only if its board answers
    ok with at least one job, carrying the company name when the board has one.
    """
    queries_file = Path("data/companies/sweep_queries.yaml")
    config = yaml.safe_load(queries_file.read_text())
    queries: list[str] = config.get("queries", [])
    exclude: list[str] = config.get("exclude_keywords", [])

    result = SweepResult(
        added=dict.fromkeys(PLATFORMS, 0),
        rejected={p: dict.fromkeys(REJECT_REASONS, 0) for p in PLATFORMS},
    )

    for platform in PLATFORMS:
        # Single-company career sites (google_jobs, meta_jobs) have no company
        # slugs to dork for — their "slugs" are search queries managed by hand.
        if platform not in PLATFORM_DOMAINS:
            continue
        platform_slugs: set[str] = set()

        for domain in PLATFORM_DOMAINS[platform]:
            for query in queries:
                scoped_query = f"site:{domain} {query}"
                for exc in exclude:
                    scoped_query += f' -"{exc}"'

                try:
                    urls = await backend.search(scoped_query)
                except Exception:
                    # One bad query (rate limit, quota) shouldn't lose the rest
                    # of the sweep — record the failure point and continue.
                    log.exception("sweep.query_failed", platform=platform, query=query)
                    continue
                platform_slugs.update(extract_slugs(urls, platform))

        candidates = new_unvetted_slugs(platform, sorted(platform_slugs))
        accepted, rejected = await vet_slugs(platform, candidates)
        n = append_unvetted(platform, accepted)
        result.added[platform] = n
        result.rejected[platform] = rejected
        log.info(
            "sweep.platform_done",
            platform=platform,
            slugs_found=len(platform_slugs),
            probed=len(candidates),
            new_unvetted=n,
            **{f"rejected_{r}": c for r, c in rejected.items()},
        )

    return result
