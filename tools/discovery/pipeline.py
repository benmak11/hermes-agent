# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deterministic discovery pipeline.

Fetches all jobs from all configured sources for every known + unvetted company,
then persists only previously-unseen jobs to Firestore. This is the cron-driven
engine (run via cli/run_discovery.py); it is intentionally not an LLM agent.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable

from google.cloud import firestore

from models.job import Job
from obs.logging import get_logger
from tools.ats import board_cache
from tools.ats._http import board_client
from tools.ats.ashby import fetch_ashby_jobs
from tools.ats.google_jobs import fetch_google_jobs
from tools.ats.greenhouse import fetch_greenhouse_jobs
from tools.ats.lever import fetch_lever_jobs
from tools.ats.meta_jobs import fetch_meta_jobs
from tools.companies import Platform, all_active_companies
from tools.company_prefs import load_exclusions

log = get_logger("tools.discovery")

# Dispatch table — keeps the fetcher choice data-driven, not a giant if/elif.
FETCHERS: dict[Platform, Callable[[str, str], Awaitable[list[Job]]]] = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
    "google_jobs": fetch_google_jobs,
    "meta_jobs": fetch_meta_jobs,
}


#: Boards fetched at once. ``all_active_companies()`` is ~198 slugs and the
#: gather below used to start every one of them simultaneously — 198 sockets
#: opening at once, against a handful of hosts (72 Ashby, 64 Greenhouse, 60
#: Lever), which is both the shape most likely to earn a 429 and the reason the
#: shared client in ``tools.ats._http`` had no pool to reuse. Matches the
#: ``persist_new_jobs`` bound below and ``tools.ats.sweep``'s 10.
_FETCH_CONCURRENCY = 20


async def _fetch_with_meta(fetcher, slug, user_id, platform, source):
    """One board, from the shared cache if it is warm. Returns metadata alongside.

    The cache is consulted *here* rather than inside the fetchers so that the
    fetchers stay exactly what they are — a board API and its parse — and so
    ``html_to_text`` keeps running before anything is cached.

    ``tools.ats.board_cache`` never raises: a miss, a cold cache, a corrupt
    payload and a GCS outage all arrive as ``None``, so anything caught below
    still means the *fetcher* failed. With ``BOARD_CACHE_TTL_SECONDS`` unset
    (the shipped default) both calls are no-ops and this is the old code path.
    """
    cached = await board_cache.load_jobs(platform, slug, user_id)
    if cached is not None:
        return (platform, slug, source, cached, True)
    try:
        jobs = await fetcher(slug, user_id)
    except Exception as e:
        # Re-raise so gather captures it; we only get here on programmer errors.
        raise RuntimeError(f"{platform}/{slug}: {e}") from e
    await board_cache.store_jobs(platform, slug, jobs)
    return (platform, slug, source, jobs, False)


async def run_discovery(
    user_id: str, concurrency: int = _FETCH_CONCURRENCY, db=None
) -> dict:
    """Fetch all jobs from all sources for all known + unvetted companies.

    The user's company exclusions are read **once**, here, before the fan-out —
    not per board. Two reasons, and the second is the real one: a lookup per
    board would be ~198 Firestore reads instead of one, and a write landing
    mid-cycle would apply to the boards not yet reached and not to the ones
    already fetched, so a single cycle would run against two different views of
    the world. One read, one immutable snapshot, the whole cycle.

    The snapshot is allowed to be slightly stale; the next cycle picks up
    anything written during this one. Nothing here is a compare-and-swap.

    Returns a summary dict for SLI tracking later.
    """
    started = time.monotonic()
    db = db or firestore.AsyncClient()
    exclusions = await load_exclusions(db, user_id)
    companies = all_active_companies(exclusions)
    log.info("discovery.start", company_count=len(companies), excluded=len(exclusions))

    sem = asyncio.Semaphore(concurrency)

    async def _fetch_bounded(platform, slug, source):
        async with sem:
            return await _fetch_with_meta(
                FETCHERS[platform], slug, user_id, platform, source
            )

    # One client for the whole fan-out: 196 of the 198 boards go through
    # fetch_board_json, and each was building and tearing down its own.
    async with board_client():
        results = await asyncio.gather(
            *(_fetch_bounded(p, s, src) for p, s, src in companies),
            return_exceptions=True,
        )

    jobs: list[Job] = []
    failures: list[dict] = []
    empty_boards: list[dict] = []
    jobs_by_platform: Counter[str] = Counter()
    # How much of this cycle the shared board cache absorbed. Both counters are
    # reported so the pair adds up to the boards attempted — a lone hit count
    # cannot tell "the cache is off" from "the cache is cold".
    boards_cached = boards_fetched = 0

    for (platform, slug, source), result in zip(companies, results, strict=True):
        # gather(return_exceptions=True) can hand back BaseExceptions too;
        # narrowing to Exception would leave those to crash the unpack below.
        if isinstance(result, BaseException):
            # A failure can only come from the fetcher (the cache swallows its
            # own errors), so the board was genuinely fetched.
            boards_fetched += 1
            # HTTP failures are already logged (and absorbed) by the fetchers;
            # an exception here is a parse/programmer error worth a traceback.
            log.error(
                "discovery.fetch_exception",
                platform=platform,
                slug=slug,
                source=source,
                error=str(result),
            )
            failures.append({"platform": platform, "slug": slug, "error": str(result)})
            continue
        platform, slug, source, fetched, from_cache = result
        if from_cache:
            boards_cached += 1
        else:
            boards_fetched += 1
        if not fetched:
            empty_boards.append({"platform": platform, "slug": slug, "source": source})
            continue
        # Attach provenance to each job (useful in matching + UI).
        for j in fetched:
            j.discovered_via = source
        jobs_by_platform[platform] += len(fetched)
        jobs.extend(fetched)

    duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "discovery.complete",
        jobs_fetched=len(jobs),
        failures=len(failures),
        empty_boards=len(empty_boards),
        jobs_by_platform=dict(jobs_by_platform),
        boards_cached=boards_cached,
        boards_fetched=boards_fetched,
        duration_ms=duration_ms,
    )
    return {
        "jobs": jobs,
        "failures": failures,
        "empty_boards": empty_boards,
        "jobs_by_platform": dict(jobs_by_platform),
        "boards_cached": boards_cached,
        "boards_fetched": boards_fetched,
        "duration_ms": duration_ms,
    }


#: Document references per ``get_all`` call. Firestore's BatchGetDocuments RPC
#: has no documented cap on the *number* of documents per request — unlike
#: ``in`` queries (30) or batched writes (500) — so the binding constraint is
#: the request/response size, not a count. 300 stays well inside it even when
#: every document comes back full, and turns a 300-job cycle's 600 sequential
#: round trips into 2.
#:
#: Note this changes round trips, not billed reads: Firestore bills per document
#: returned either way. The saving in *reads* comes from the tombstone-first
#: ordering below, not from the batching.
_GET_ALL_CHUNK = 300


async def _existing_ids(db, refs: list) -> set[str]:
    """Ids of the documents in ``refs`` that exist, fetched in batches.

    ``get_all`` yields a snapshot for every reference asked about, including
    missing ones (``exists`` False), and in no guaranteed order — so callers
    must match on identity, never on position.
    """
    found: set[str] = set()
    for start in range(0, len(refs), _GET_ALL_CHUNK):
        async for snap in db.get_all(refs[start : start + _GET_ALL_CHUNK]):
            if snap.exists:
                found.add(snap.id)
    return found


async def persist_new_jobs(jobs: list[Job], concurrency: int = 20) -> int:
    """Write only previously-unseen jobs to Firestore. Returns count of new jobs.

    "Seen" includes discarded jobs: matching moves zero/ineligible-scored jobs
    to a ``discarded_jobs`` tombstone, and postings stay live on boards for
    weeks — without this check every run would re-persist and re-score them.

    Jobs with an empty ``jd_raw`` never persist: there is nothing to parse or
    score (Vertex rejects empty input outright), so they would sit pending and
    re-fail every scoring run until deleted by hand — 13 such docs observed in
    the 12K backlog. An empty JD usually means the fetcher got no content for
    that posting, so drops are logged with their provenance.
    """
    # De-dupe within this run (a slug can appear in both known + unvetted).
    unique = {j.id: j for j in jobs}
    empty_jd = [j for j in unique.values() if not j.jd_raw.strip()]
    if empty_jd:
        unique = {i: j for i, j in unique.items() if j.jd_raw.strip()}
        log.warning(
            "discovery.empty_jd_dropped",
            count=len(empty_jd),
            postings=[f"{j.source}/{j.company}: {j.title}" for j in empty_jd[:10]],
        )
    db = firestore.AsyncClient()
    sem = asyncio.Semaphore(concurrency)

    async def _write(doc_ref, job: Job) -> None:
        async with sem:
            await doc_ref.set(job.model_dump(mode="json"))

    # Group by user so a chunk never mixes two users' collections, which keeps
    # the document id enough to identify a snapshot (get_all does not promise
    # to answer in the order it was asked).
    by_user: dict[str, list[Job]] = {}
    for job in unique.values():
        by_user.setdefault(job.user_id, []).append(job)

    new = seen_before = previously_discarded = 0
    for user_id, user_jobs in by_user.items():
        user_ref = db.collection("users").document(user_id)
        jobs_col = user_ref.collection("jobs")
        discarded_col = user_ref.collection("discarded_jobs")

        # Tombstones first, then the job docs for whatever survived — the same
        # precedence the per-job version had (a job that is both tombstoned and
        # present still resolves as discarded), but expressed as the order of
        # two batched round trips instead of an if/elif over two reads that
        # always both happened. Tombstoned jobs are the bulk of a cycle and now
        # cost one read each instead of two.
        tombstoned = await _existing_ids(
            db, [discarded_col.document(j.id) for j in user_jobs]
        )
        live = [j for j in user_jobs if j.id not in tombstoned]
        previously_discarded += len(user_jobs) - len(live)

        present = await _existing_ids(db, [jobs_col.document(j.id) for j in live])
        fresh = [j for j in live if j.id not in present]
        seen_before += len(live) - len(fresh)
        new += len(fresh)

        await asyncio.gather(*(_write(jobs_col.document(j.id), j) for j in fresh))

    log.info(
        "discovery.persisted",
        new_jobs=new,
        seen_before=seen_before,
        previously_discarded=previously_discarded,
    )
    return new
