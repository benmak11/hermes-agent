# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Board links mined from Hacker News "Ask HN: Who is hiring?" threads.

Not a job feed: only the greenhouse / lever / ashby / workable board slugs
linked from each thread's top-level posts are harvested, for ``cli.hn_boards``
to vet and append to unvetted.yaml. Reads the public HN Algolia API (no key, no spend).
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

from obs.logging import get_logger
from tools.companies import Platform

log = get_logger("tools.discovery.hn")

ALGOLIA = "https://hn.algolia.com/api/v1"
HIRING_PREFIX = "Ask HN: Who is hiring?"
HN_PLATFORMS: tuple[Platform, ...] = ("greenhouse", "lever", "ashby", "workable")
MAX_MONTHS = 6

_TIMEOUT = 20.0

#: ``whoishiring`` posts three stories a month (hiring, wants to be hired,
#: freelancer); ask for enough hits to find ``months`` hiring threads.
_HITS_PER_MONTH = 3
_HITS_SLACK = 6

# A slug runs to the next path, query, quote, tag or whitespace character.
_SLUG = r"([^\s/?#&\"'<>]+)"
_PATTERNS: tuple[tuple[Platform, re.Pattern[str]], ...] = (
    (
        "greenhouse",
        re.compile(
            r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board\?(?:[^\s\"'<>]*&)?for="
            + _SLUG,
            re.IGNORECASE,
        ),
    ),
    (
        "greenhouse",
        re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/" + _SLUG, re.IGNORECASE),
    ),
    ("lever", re.compile(r"jobs\.lever\.co/" + _SLUG, re.IGNORECASE)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _SLUG, re.IGNORECASE)),
    ("workable", re.compile(r"apply\.workable\.com/" + _SLUG, re.IGNORECASE)),
)

#: Platform paths that are not companies (``embed`` is greenhouse's embed
#: board, whose slug is in ``?for=``; ``j`` is a Workable single-job short
#: link, ``apply.workable.com/j/{shortcode}``, which names no account).
_NON_SLUGS = {"jobs", "search", "api", "v1", "boards", "embed", "j"}
_TRAILING = ".,;:!?)]}'\"*"


def extract_board_slugs(text: str) -> list[tuple[Platform, str]]:
    """The distinct ``(platform, slug)`` pairs linked from one post's HTML text.

    Entities are unescaped first (Algolia escapes ``/`` as ``&#x2F;``), slugs
    are percent-decoded and stripped of trailing punctuation, and spelling is
    kept. Order is first appearance; exact duplicates collapse.
    """
    text = html.unescape(text or "")
    found: list[tuple[int, Platform, str]] = []
    for platform, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            slug = unquote(match.group(1)).rstrip(_TRAILING).strip()
            if slug and slug.casefold() not in _NON_SLUGS:
                found.append((match.start(), platform, slug))
    found.sort(key=lambda f: f[0])
    out: list[tuple[Platform, str]] = []
    for _, platform, slug in found:
        if (platform, slug) not in out:
            out.append((platform, slug))
    return out


@dataclass(frozen=True)
class Thread:
    id: str
    title: str
    created_at: str


@dataclass
class Harvest:
    """Threads read, top-level posts scanned, slugs found and API failures.

    ``slugs[platform]`` maps each slug to the number of posts linking it, in
    first-seen order; case variants collapse onto the first spelling seen.
    """

    threads: list[Thread] = field(default_factory=list)
    posts: int = 0
    slugs: dict[Platform, dict[str, int]] = field(
        default_factory=lambda: {p: {} for p in HN_PLATFORMS}
    )
    errors: list[str] = field(default_factory=list)


async def _get_json(client: httpx.AsyncClient, url: str, **params) -> dict:
    response = await client.get(url, params=params or None)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object from {url}")
    return data


async def latest_hiring_threads(client: httpx.AsyncClient, months: int) -> list[Thread]:
    """The newest ``months`` "Who is hiring?" threads, newest first.

    Raises on an API error; :func:`harvest` turns that into a reported failure.
    """
    data = await _get_json(
        client,
        f"{ALGOLIA}/search_by_date",
        tags="story,author_whoishiring",
        hitsPerPage=months * _HITS_PER_MONTH + _HITS_SLACK,
    )
    threads = [
        Thread(str(hit["objectID"]), hit["title"], hit.get("created_at") or "")
        for hit in data.get("hits", [])
        if isinstance(hit, dict)
        and str(hit.get("title") or "").startswith(HIRING_PREFIX)
        and hit.get("objectID")
    ]
    threads.sort(key=lambda t: t.created_at, reverse=True)
    return threads[:months]


def _record(result: Harvest, text: str) -> None:
    for platform, slug in extract_board_slugs(text):
        bucket = result.slugs[platform]
        spelling = next((s for s in bucket if s.casefold() == slug.casefold()), slug)
        bucket[spelling] = bucket.get(spelling, 0) + 1


async def harvest(months: int = 1, client: httpx.AsyncClient | None = None) -> Harvest:
    """Board slugs from the newest ``months`` hiring threads (1 to :data:`MAX_MONTHS`).

    Only top-level comments are read; nested replies are not job posts. Never
    raises on an API error: a failed search or thread is recorded in
    ``errors`` and whatever was gathered so far is returned.
    """
    if not 1 <= months <= MAX_MONTHS:
        raise ValueError(f"months must be 1..{MAX_MONTHS}, got {months}")
    if client is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as owned:
            return await harvest(months, owned)

    result = Harvest()
    try:
        result.threads = await latest_hiring_threads(client, months)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        result.errors.append(f"thread search failed: {type(e).__name__}: {e}")
        log.warning("hn.search_failed", error=f"{type(e).__name__}: {e}")
        return result

    items = await asyncio.gather(
        *(_get_json(client, f"{ALGOLIA}/items/{t.id}") for t in result.threads),
        return_exceptions=True,
    )
    for thread, item in zip(result.threads, items, strict=True):
        if isinstance(item, BaseException):
            msg = f"{thread.title} ({thread.id}): {type(item).__name__}: {item}"
            result.errors.append(msg)
            log.warning("hn.thread_failed", thread=thread.id, error=msg)
            continue
        for post in item.get("children") or []:
            if not isinstance(post, dict):
                continue
            result.posts += 1
            _record(result, post.get("text") or "")
    return result
