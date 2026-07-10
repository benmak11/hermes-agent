# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Resumable batch scoring pipelines (Phase C ops architecture).

``tools.matching.batch`` runs both Vertex batch legs inside one long-lived
process — fine for a babysat CLI run, fatal for fire-and-forget: if the
process dies, a paid batch finishes on Vertex with nobody left to ingest it
(exactly what forced a hand-written resume script on 2026-07-10). This module
splits the same pipeline at its natural seams and persists the position in a
top-level ``batch_runs`` collection (server-only, like ``jd_cache``), so any
later process can pick a run up — in practice the hermes-worker, whose hourly
cron tick enqueues a resume pass while runs are in flight:

- :func:`start`  consults the free jd_cache, submits the Flash parse batch,
  records the run. Seconds of work, no polling — safe inside a request.
- :func:`resume` polls each running run's Vertex job once; finished output is
  ingested and the run advances: parse output feeds jd_cache + job docs and
  the Pro score batch goes out; score output persists matches/tombstones and
  the run completes.

Ingestion is stateless by design. Batch output lines echo their request text,
and request texts are content-derived — ``jd_raw`` for parse, match context +
job block for score (the context is stashed in the run's GCS dir at submit
time). Resuming therefore needs nothing from the submitting process's memory:
it reloads the user's pending jobs and joins on content. Jobs whose lines
failed simply stay pending for a future run, and jobs persisted by an earlier
partial ingest drop out of the reload — which is what makes re-ingesting
after a crash safe.

Runs advance under a claim (update-time precondition + TTL), so a manual
``--batch-resume`` racing the worker's tick can't double-submit a paid Pro
stage. One window stays open: a crash between ``submit_batch`` returning and
the run doc recording the job name leaves a paid batch untracked. It's
milliseconds wide; the job's ``display_name`` carries the run tag, so the
Vertex console finds it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from google.api_core.exceptions import FailedPrecondition
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from obs.logging import get_logger
from tools.matching import jd_cache
from tools.matching.batch import (
    _DONE_STATES,
    _PERSIST_CONCURRENCY,
    _USABLE_STATES,
    BATCH_FLASH_MODEL,
    BATCH_PRO_MODEL,
    _request_text,
    batch_bucket_name,
    build_parse_request,
    build_score_request,
    download_text,
    fetch_batch_output,
    get_batch_job,
    join_parse_responses,
    join_score_responses,
    submit_batch,
    upload_text,
)
from tools.matching.pipeline import (
    OUT_OF_FAMILY,
    build_match_context,
    build_match_job_block,
)
from tools.matching.score import (
    load_profile_and_pending,
    persist_jd_parsed,
    persist_result,
    score_pending_jobs,
)

log = get_logger("tools.matching")

COLLECTION = "batch_runs"

# Backlogs at/above this size score as a resumable batch run when the queue
# architecture is available: half-price LLM calls, no long-lived process.
# Below it, online scoring answers in seconds and the Phase 3.2 context cache
# keeps it cheap — the scheduler's hourly increments live down here.
BATCH_MIN_PENDING = 50

# A claim this old is considered abandoned (its resume pass died) and the run
# can be claimed again; ingestion being idempotent makes the retry safe. Must
# comfortably exceed the longest plausible ingest so an hourly tick can't
# double-submit a Pro batch under a slow-but-alive ingest.
_CLAIM_TTL_SECONDS = 45 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


async def _persist_all(pairs, persist) -> list:
    """Run ``persist(ref, job, ...)`` over pairs at Firestore-write concurrency."""
    sem = asyncio.Semaphore(_PERSIST_CONCURRENCY)

    async def _one(args):
        async with sem:
            return await persist(*args)

    return await asyncio.gather(*(_one(args) for args in pairs))


async def start(
    user_id: str,
    *,
    limit: int | None = None,
    min_pending: int | None = None,
    db: firestore.AsyncClient | None = None,
) -> dict:
    """Submit a resumable batch run for the user's pending backlog.

    Free work happens inline (jd_cache hits, and — when nothing needs Flash —
    the family filter and Pro submission); paid batches are submitted but
    never awaited. Returns ``{"started": False, "pending": n}`` when
    ``min_pending`` says the backlog is too small to bother.
    """
    db = db or firestore.AsyncClient()
    profile, pending = await load_profile_and_pending(db, user_id, limit)
    if min_pending is not None and len(pending) < min_pending:
        return {"started": False, "pending": len(pending)}

    run_tag = _now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    gcs_root = f"gs://{batch_bucket_name()}/vertex-batch/{run_tag}"
    counts = {"scored": 0, "discarded": 0, "failed": 0, "parse_failed": 0}

    # Cheapest parse source first: the cross-user jd_cache is free.
    to_parse: dict[str, list[Job]] = {}
    for _, job in pending:
        if job.jd_parsed is None and job.jd_raw.strip():
            to_parse.setdefault(job.jd_raw, []).append(job)
    ref_by_id = {job.id: ref for ref, job in pending}
    if to_parse:
        cached = await jd_cache.lookup_many(db, list(to_parse))
        hits: list[Job] = []
        for text, parsed in cached.items():
            for job in to_parse.pop(text):
                job.jd_parsed = parsed
                hits.append(job)
        if hits:
            await _persist_all([(ref_by_id[j.id], j) for j in hits], persist_jd_parsed)
            log.info("batch_runs.jd_cache_hits", hits=len(hits), to_flash=len(to_parse))

    run_ref = db.collection(COLLECTION).document(run_tag)
    doc = {
        "user_id": user_id,
        "state": "running",
        "stage": "parse",
        "job_name": None,
        "gcs_root": gcs_root,
        "counts": counts,
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
        # Born claimed: resume must not touch the doc until the submit below
        # has recorded its job name.
        "claimed_at": _now().isoformat(),
    }

    if to_parse:
        await run_ref.set(doc)
        job_name = await submit_batch(
            model=BATCH_FLASH_MODEL,
            lines=[build_parse_request(text) for text in to_parse],
            gcs_dir=f"{gcs_root}/parse",
            display_name=f"hermes-parse-{run_tag}",
        )
        await run_ref.update({"job_name": job_name, "claimed_at": None})
        log.info(
            "batch_runs.started",
            run=run_tag,
            user_id=user_id,
            stage="parse",
            pending=len(pending),
            parse_requests=len(to_parse),
        )
        return {
            "started": True,
            "run": run_tag,
            "stage": "parse",
            "pending": len(pending),
            "counts": counts,
        }

    # Everything already parsed (cache hits / prior runs): skip straight to
    # the score stage — or straight to done when nothing is in-family.
    await run_ref.set(doc)
    stage = await _submit_score_stage(
        db, run_ref, run_tag, gcs_root, profile, pending, counts
    )
    log.info(
        "batch_runs.started",
        run=run_tag,
        user_id=user_id,
        stage=stage,
        pending=len(pending),
        parse_requests=0,
    )
    return {
        "started": True,
        "run": run_tag,
        "stage": stage,
        "pending": len(pending),
        "counts": counts,
    }


async def _submit_score_stage(
    db, run_ref, run_tag: str, gcs_root: str, profile, pending, counts: dict
) -> str:
    """Family-filter parsed pending jobs, then submit the Pro batch.

    Out-of-family jobs tombstone immediately through the same persistence
    path as every other scorer (free, no LLM). Unparsed jobs are left alone —
    their parse failed, and a future run retries them. Returns the resulting
    run stage ("score", or "done" when nothing needs Pro).
    """
    targets = {f.lower() for f in profile.preferences.target_role_families}
    tombstones: list[tuple] = []
    to_score: list[tuple] = []
    for ref, job in pending:
        if job.jd_parsed is None:
            continue
        if job.jd_parsed.role_family not in targets:
            match = OUT_OF_FAMILY.model_copy()
            match.job_id = job.id
            tombstones.append((ref, job, match))
        else:
            to_score.append((ref, job))
    for outcome in await _persist_all(tombstones, persist_result):
        counts[outcome] = counts.get(outcome, 0) + 1

    if not to_score:
        await run_ref.update(
            {
                "state": "done",
                "stage": "done",
                "counts": counts,
                "claimed_at": None,
                "updated_at": _now().isoformat(),
            }
        )
        log.info("batch_runs.completed", run=run_tag, **counts)
        return "done"

    context = build_match_context(profile)
    by_block: dict[str, list[Job]] = {}
    for _, job in to_score:
        by_block.setdefault(build_match_job_block(job), []).append(job)
    # The score output can only be joined with the exact context it was
    # prompted with, and the profile may change while the batch runs — so the
    # context travels with the run, not with the profile.
    await upload_text(f"{gcs_root}/score/context.txt", context)
    job_name = await submit_batch(
        model=BATCH_PRO_MODEL,
        lines=[build_score_request(context, block) for block in by_block],
        gcs_dir=f"{gcs_root}/score",
        display_name=f"hermes-score-{run_tag}",
    )
    await run_ref.update(
        {
            "stage": "score",
            "job_name": job_name,
            "counts": counts,
            "claimed_at": None,
            "updated_at": _now().isoformat(),
        }
    )
    log.info(
        "batch_runs.score_submitted",
        run=run_tag,
        score_requests=len(by_block),
        **counts,
    )
    return "score"


async def _ingest_parse(db, run_ref, run: dict) -> None:
    """Parse batch finished: feed jd_cache + job docs, then submit scoring."""
    run_tag = run_ref.id
    out_lines = await fetch_batch_output(f"{run['gcs_root']}/parse")
    profile, pending = await load_profile_and_pending(db, run["user_id"])

    # Join strictly on texts the batch echoed: pending jobs discovered after
    # submission aren't failures, they're just not part of this run.
    texts = {t for t in (_request_text(line) for line in out_lines) if t}
    by_text: dict[str, list[Job]] = {}
    for _, job in pending:
        if job.jd_parsed is None and job.jd_raw in texts:
            by_text.setdefault(job.jd_raw, []).append(job)
    failed = join_parse_responses(out_lines, by_text)

    counts = run.get("counts") or {}
    counts["parse_failed"] = counts.get("parse_failed", 0) + len(failed)

    parsed_jobs = [j for jobs in by_text.values() for j in jobs if j.jd_parsed]
    if parsed_jobs:
        # Shared property first (any user's future run skips Flash), then the
        # per-job docs so this run's stage 2 never re-pays either.
        await jd_cache.store_many(
            db,
            {
                text: jobs[0].jd_parsed
                for text, jobs in by_text.items()
                if jobs[0].jd_parsed is not None
            },
            model=BATCH_FLASH_MODEL,
        )
        ref_by_id = {job.id: ref for ref, job in pending}
        await _persist_all(
            [(ref_by_id[j.id], j) for j in parsed_jobs], persist_jd_parsed
        )
    log.info(
        "batch_runs.parse_ingested",
        run=run_tag,
        parsed=len(parsed_jobs),
        parse_failed=len(failed),
    )
    await _submit_score_stage(
        db, run_ref, run_tag, run["gcs_root"], profile, pending, counts
    )


async def _ingest_score(db, run_ref, run: dict) -> None:
    """Score batch finished: persist matches/tombstones, complete the run."""
    run_tag = run_ref.id
    context = await download_text(f"{run['gcs_root']}/score/context.txt")
    out_lines = await fetch_batch_output(f"{run['gcs_root']}/score")
    _, pending = await load_profile_and_pending(db, run["user_id"])

    # Same restriction as parse ingest: only blocks the batch echoed count,
    # so join_score_responses's failed list means "line failed", not "job
    # wasn't in this run".
    prefix = f"{context}\n\n"
    echoed_blocks = {
        t.removeprefix(prefix)
        for t in (_request_text(line) for line in out_lines)
        if t and t.startswith(prefix)
    }
    by_block: dict[str, list[Job]] = {}
    ref_by_id = {}
    for ref, job in pending:
        block = build_match_job_block(job)
        if block in echoed_blocks:
            by_block.setdefault(block, []).append(job)
            ref_by_id[job.id] = ref
    matches, failed = join_score_responses(out_lines, context, by_block)

    counts = run.get("counts") or {}
    counts["failed"] = counts.get("failed", 0) + len(failed)
    to_persist = [
        (ref_by_id[job.id], job, matches[job.id])
        for jobs in by_block.values()
        for job in jobs
        if job.id in matches
    ]
    for outcome in await _persist_all(to_persist, persist_result):
        counts[outcome] = counts.get(outcome, 0) + 1

    await run_ref.update(
        {
            "state": "done",
            "stage": "done",
            "counts": counts,
            "claimed_at": None,
            "updated_at": _now().isoformat(),
        }
    )
    log.info("batch_runs.completed", run=run_tag, **counts)


async def resume(
    *,
    user_id: str | None = None,
    db: firestore.AsyncClient | None = None,
) -> dict:
    """One pass over in-flight runs: poll Vertex, ingest whatever finished.

    Cheap when nothing is ready (one Vertex GET per running run). Designed to
    be fired repeatedly — the worker's hourly tick — and safely in parallel
    with a manual pass: each run is claimed via an update-time precondition
    before any ingest work, and a claim younger than the TTL is skipped.
    """
    db = db or firestore.AsyncClient()
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("state", "==", "running")
    )
    if user_id:
        query = query.where(filter=FieldFilter("user_id", "==", user_id))

    summary = {"checked": 0, "running": 0, "advanced": 0, "completed": 0, "failed": 0}
    now = _now()
    async for snap in query.stream():
        summary["checked"] += 1
        run = snap.to_dict() or {}
        run_ref = snap.reference

        claimed = _parse_iso(run.get("claimed_at"))
        if claimed and (now - claimed).total_seconds() < _CLAIM_TTL_SECONDS:
            summary["running"] += 1  # someone else is on it (or a fresh start)
            continue
        if not run.get("job_name"):
            # start() died between submit and recording the name; the Vertex
            # console finds the orphan by display_name hermes-*-{run_tag}.
            await run_ref.update(
                {
                    "state": "failed",
                    "error": "no job_name recorded — check Vertex console for "
                    f"display_name hermes-*-{snap.id}",
                    "updated_at": now.isoformat(),
                }
            )
            summary["failed"] += 1
            log.error("batch_runs.orphaned", run=snap.id)
            continue

        job = await get_batch_job(run["job_name"])
        if job.state not in _DONE_STATES:
            summary["running"] += 1
            continue

        # Claim before any paid/ingest work: the precondition makes racing
        # resumers lose loudly instead of double-submitting a Pro batch.
        try:
            await run_ref.update(
                {"claimed_at": now.isoformat()},
                option=db.write_option(last_update_time=snap.update_time),
            )
        except FailedPrecondition:
            summary["running"] += 1
            continue

        try:
            if job.state not in _USABLE_STATES:
                await run_ref.update(
                    {
                        "state": "failed",
                        "error": f"{job.state}: {job.error}",
                        "updated_at": now.isoformat(),
                    }
                )
                summary["failed"] += 1
                log.error(
                    "batch_runs.vertex_failed",
                    run=snap.id,
                    state=str(job.state),
                    error=str(job.error)[:200],
                )
            elif run.get("stage") == "parse":
                await _ingest_parse(db, run_ref, run)
                summary["advanced"] += 1
            else:
                await _ingest_score(db, run_ref, run)
                summary["completed"] += 1
        except Exception:
            # Leave the run claimed; after the TTL the next pass retries the
            # (idempotent) ingest. Never let one bad run kill the whole pass.
            log.exception("batch_runs.resume_failed", run=snap.id)

    if summary["checked"]:
        log.info("batch_runs.resume_pass", **summary)
    return summary


async def score_or_start_run(user_id: str) -> dict:
    """The discovery cycle's scoring seam: online for small backlogs,
    a resumable batch run for big ones.

    Returns the online scorer's counts dict either way; when a batch run was
    started, the LLM outcomes are zero-so-far (results land when the worker's
    resume ticks ingest them) and ``batch_run`` carries the run tag.
    """
    run = await start(user_id, min_pending=BATCH_MIN_PENDING)
    if not run.get("started"):
        return await score_pending_jobs(user_id)
    counts = run["counts"]
    return {
        "scored": counts["scored"],
        "discarded": counts["discarded"],
        "failed": counts["failed"],
        "pending": run["pending"],
        "batch_run": run["run"],
    }
