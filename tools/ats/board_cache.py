# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cross-user board cache: a board is fetched once per TTL, for everyone.

Every user's discovery cycle walks the same ~198 boards independently, and
the boards do not know who is asking: two users' cycles were measured
fetching 13,083 jobs each with byte-identical per-platform splits. This module
removes that duplication the way :mod:`tools.matching.jd_cache` removed
duplicated parses, storing content once keyed by what it is.

GCS, not Firestore: a Greenhouse board fetched with ``?content=true`` is
megabytes, well past Firestore's 1 MiB document limit.

    gs://{resume_bucket}/board_cache/{platform}/{quoted-slug}.json

The ``board_cache/`` prefix is deliberately outside ``users/{uid}/``, which is
the only prefix ``cli/reset_user.py`` deletes: wiping one user for a demo reset
must not evict the cache every other user shares.

The payload is user-independent, which is the premise: ``user_id`` and
``discovered_at`` are dropped on the way in and re-stamped per user on the way
out, and ``tests/unit/test_board_cache.py`` pins the rest field by field.

``jd_raw`` must survive byte-for-byte. ``jd_cache.jd_hash`` is
``sha256(jd_raw.encode())`` with no normalization, so one changed byte misses
the cache on every existing parse and this module costs money instead of
saving it. Nothing here touches the text, and JSON is used because it
round-trips ``str`` exactly.

Off by default: ``BOARD_CACHE_TTL_SECONDS`` defaults to 0, which builds no GCS
client and reads or writes no blob.

No lock and no generation precondition — two cycles that miss the same board
both fetch and both write, which is idempotent and is what happens with no
cache at all, where an ``if_generation_match`` would make the loser raise.
Every failure, read or write, is swallowed: a cache that breaks a cycle is
worse than no cache.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from urllib.parse import quote

from pydantic import ValidationError

from models.job import Job
from obs.logging import get_logger
from tools.tailoring.render import resume_bucket_name

log = get_logger("tools.ats.board_cache")

#: Object-name prefix. Chosen to sit outside ``users/`` — see the module
#: docstring on ``cli/reset_user.py``.
PREFIX = "board_cache"

#: Payload shape version. Bumping it invalidates every entry at once (old blobs
#: read as a miss and are overwritten by the next fetch), which is the migration
#: story for any change to what is stored or stripped.
PAYLOAD_VERSION = 1

#: Fields dropped on the way in and re-stamped on the way out. Everything else
#: on ``Job`` describes the posting, not the reader.
PER_USER_FIELDS = ("user_id", "discovered_at")


def ttl_seconds() -> int:
    """Cache lifetime in seconds. ``0`` (the default) disables the cache.

    The floor on ``discovery_interval_hours`` is 6, so any TTL up to 6h is
    invisible to a user's own cadence. The one behaviour it changes is a
    manual ``POST /settings/discovery/run`` clicked twice inside the window.
    """
    raw = os.getenv("BOARD_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return 0
    try:
        return max(int(raw), 0)
    except ValueError:
        log.warning("board_cache.ttl_env_invalid", value=raw[:40])
        return 0


def enabled() -> bool:
    """True when boards may be served from, and written to, the cache."""
    return ttl_seconds() > 0


def blob_path(platform: str, slug: str) -> str:
    """Object name for one board.

    The slug is percent-quoted with nothing safe: ``google_jobs`` and
    ``meta_jobs`` reuse the slot as a search query, and an unquoted space or
    slash would fold two different queries onto one blob.
    """
    return f"{PREFIX}/{platform}/{quote(slug, safe='')}.json"


_storage_client = None


def _client():
    """Memoised GCS client, built on first use only.

    Memoised because a cycle touches ~198 boards and every
    ``storage.Client()`` re-resolves ADC, a metadata round trip on Cloud Run.
    Safe to memoise — unlike the httpx client in ``tools.ats._http``, whose
    pool binds to an event loop — because this one is synchronous and only
    used inside ``asyncio.to_thread``.
    """
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage

        _storage_client = storage.Client()
    return _storage_client


def reset_client() -> None:
    """Drop the memoised GCS client (tests; never needed in production)."""
    global _storage_client
    _storage_client = None


def _blob(platform: str, slug: str):
    return _client().bucket(resume_bucket_name()).blob(blob_path(platform, slug))


def strip_user(job: Job) -> dict:
    """One job as it is stored: JSON-mode dump minus the per-user fields."""
    payload = job.model_dump(mode="json")
    for field in PER_USER_FIELDS:
        payload.pop(field, None)
    return payload


def _encode(platform: str, slug: str, jobs: list[Job]) -> bytes:
    """Serialize a board.

    ``ensure_ascii`` is left at its default ``True``: a ``\\ud800``-style
    escape in a posting yields a lone surrogate that ``str.encode("utf-8")``
    refuses, and escaping non-ASCII still round-trips the text exactly, which
    is what ``jd_raw`` depends on.
    """
    payload = {
        "version": PAYLOAD_VERSION,
        "platform": platform,
        "slug": slug,
        # Freshness travels inside the payload, not on blob metadata — see
        # the note in ``load_jobs``.
        "fetched_at": datetime.now(UTC).isoformat(),
        "jobs": [strip_user(j) for j in jobs],
    }
    return json.dumps(payload).encode("ascii")


def _is_fresh(payload: dict, ttl: int) -> bool:
    """Is this payload's own timestamp inside the TTL?

    A future timestamp is treated as a miss rather than as maximally fresh:
    it is corrupt or clock-skewed, and the other reading would pin a stale
    board in place indefinitely.
    """
    raw = payload.get("fetched_at")
    if not isinstance(raw, str):
        return False
    try:
        fetched_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if fetched_at.tzinfo is None:
        return False
    age = (datetime.now(UTC) - fetched_at).total_seconds()
    return 0 <= age <= ttl


def _decode(payload: dict, user_id: str) -> list[Job] | None:
    """Rehydrate a stored board for ``user_id``, or ``None`` if it no longer fits.

    Schema drift self-heals exactly as it does in ``jd_cache``: a payload that
    no longer validates against the current ``Job`` reads as a miss, the board
    is re-fetched, and the next :func:`store_jobs` overwrites the blob.
    """
    if payload.get("version") != PAYLOAD_VERSION:
        return None
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        return None
    now = datetime.now(UTC)
    jobs: list[Job] = []
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            return None
        try:
            jobs.append(
                Job.model_validate({**raw, "user_id": user_id, "discovered_at": now})
            )
        except ValidationError:
            return None
    return jobs


async def load_jobs(platform: str, slug: str, user_id: str) -> list[Job] | None:
    """A cached board re-stamped for ``user_id``, or ``None`` to go and fetch.

    ``None`` covers every reason not to use the cache — disabled, absent,
    stale, corrupt, schema-drifted, or a GCS error — and this never raises, so
    a broken bucket cannot break a cycle.

    One ``download_as_bytes()``, with freshness decided from those same bytes.
    Checking metadata first would be three round trips against a possibly
    different object, and ``download_as_bytes`` does not populate
    ``blob.updated``, so a post-download freshness check silently never
    passes.
    """
    ttl = ttl_seconds()
    if ttl <= 0:
        return None
    try:
        raw = await asyncio.to_thread(_blob(platform, slug).download_as_bytes)
        payload = json.loads(raw)
    except Exception as e:
        # Includes the ordinary miss (NotFound). Debug, not warning: on a cold
        # cache this is 198 lines of "the cache is cold".
        log.debug(
            "board_cache.read_miss",
            platform=platform,
            slug=slug,
            error=f"{type(e).__name__}: {e}",
        )
        return None
    if not isinstance(payload, dict) or not _is_fresh(payload, ttl):
        return None
    jobs = _decode(payload, user_id)
    if jobs is None:
        log.info("board_cache.payload_rejected", platform=platform, slug=slug)
        return None
    log.debug("board_cache.hit", platform=platform, slug=slug, jobs=len(jobs))
    return jobs


async def store_jobs(platform: str, slug: str, jobs: list[Job]) -> None:
    """Write a freshly fetched board back to the cache. Best effort, never raises.

    An empty board is never cached: the fetchers cannot tell "no open roles"
    from "the fetch failed", since ``fetch_board_json`` absorbs a 404, a spent
    429 and a timeout alike and every one arrives as ``[]``. Caching that
    would hand a transient board-side failure to every user for a whole TTL,
    which is the one way this module could lose jobs.
    """
    if not enabled() or not jobs:
        return
    try:
        payload = _encode(platform, slug, jobs)
        await asyncio.to_thread(
            _blob(platform, slug).upload_from_string,
            payload,
            content_type="application/json",
        )
        log.debug("board_cache.stored", platform=platform, slug=slug, jobs=len(jobs))
    except Exception as e:
        # Same contract as ``jd_cache.store_many``: the cycle already holds the
        # jobs, so a failed write only costs a future cycle one re-fetch.
        log.warning(
            "board_cache.store_failed",
            platform=platform,
            slug=slug,
            error=f"{type(e).__name__}: {e}",
        )
