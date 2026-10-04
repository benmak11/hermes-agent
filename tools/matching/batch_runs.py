# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Resumable batch scoring pipelines.

``tools.matching.batch`` runs both Vertex batch legs inside one long-lived
process, which is fatal for fire-and-forget: if the process dies, a paid batch
finishes on Vertex with nobody left to ingest it. This module splits the same
pipeline at its seams and persists the position in a top-level ``batch_runs``
collection (server-only, like ``jd_cache``), so any later process — in
practice the worker's hourly cron tick — can pick a run up:

- :func:`start`  consults the free jd_cache, submits the Flash parse batch,
  records the run. Seconds of work, no polling — safe inside a request.
- :func:`resume` polls each running run's Vertex job once; finished output is
  ingested and the run advances: parse output feeds jd_cache + job docs and
  the Pro score batch goes out; score output persists matches/tombstones and
  the run completes.

Ingestion is stateless by design: batch output lines echo their request text,
and request texts are content-derived (``jd_raw`` for parse, match context +
job block for score, the context stashed in the run's GCS dir at submit time).
Resuming therefore needs nothing from the submitting process's memory — it
reloads the user's pending jobs and joins on content. Jobs whose lines failed
stay pending for a future run, and jobs persisted by an earlier partial ingest
drop out of the reload, which is what makes re-ingesting after a crash safe.

Spend is the one thing statelessness does *not* make safe: re-reading the
output re-prices the same calls, and the run ledger banks with
``firestore.Increment``. Each leg's flush is therefore gated on a
``cost_banked_at`` marker so it reaches the ledger exactly once — see the flush
in :func:`resume` for which way that guard fails.

Runs advance under a claim (update-time precondition + TTL), so a manual
``--batch-resume`` racing the worker's tick cannot double-submit a paid Pro
stage. One window stays open: a crash between ``submit_batch`` returning and
the run doc recording the job name leaves a paid batch untracked. The job's
``display_name`` carries the run tag, so the Vertex console finds it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from functools import partial

import structlog
from google.api_core.exceptions import FailedPrecondition
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from obs.llm_cost import reset_run_cost
from obs.logging import current_run_id, get_logger
from tools.matching import budget, jd_cache, rates
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
    build_match_context,
    build_match_job_block,
    geo_enforce_enabled,
    prefilter,
)
from tools.matching.score import (
    EMPTY_GEO_COUNTS,
    enforced_geo_gate,
    load_profile_and_pending,
    persist_jd_parsed,
    persist_result,
    score_pending_jobs,
    scored_with,
    unbudgeted_limit,
)
from tools.run_costs import DONE, FAILED, RUNNING, persist_run_cost

log = get_logger("tools.matching")

COLLECTION = "batch_runs"

# Backlogs at/above this size score as a resumable batch run: half-price LLM
# calls, no long-lived process. Below it, online scoring answers in seconds and
# the context cache keeps it cheap.
#
# Coupled to SCORING_BUDGET_PER_CYCLE in a way that does not look coupled: the
# reservation is taken first, so this threshold sees min(backlog, grant). Set
# the per-cycle budget below this and every run routes online, however much
# backlog is waiting.
BATCH_MIN_PENDING = 50

# A claim this old is considered abandoned (its resume pass died) and the run
# can be claimed again; ingestion being idempotent makes the retry safe. Must
# comfortably exceed the longest plausible ingest so an hourly tick can't
# double-submit a Pro batch under a slow-but-alive ingest.
_CLAIM_TTL_SECONDS = 45 * 60

#: Run states whose committed estimate may still be un-ingested. ``done`` is
#: excluded: both its legs are banked, so counting the estimate too would
#: double the same money.
OUTSTANDING_STATES = ("running", "failed")


def _now() -> datetime:
    return datetime.now(UTC)


def _committed(requests: int, *, leg: str, model: str) -> dict:
    """What this leg has just committed us to paying Google.

    A Vertex batch is billed when Google runs it but priced by our ledger hours
    later, at ingest, so an abandoned run was once billed and recorded nowhere.
    The write that makes a batch trackable therefore also records its price.

    An estimate, explicitly, and never mixed with actuals — see
    :func:`outstanding_committed`.
    """
    low, high = rates.committed_usd(requests, leg=leg)
    return {
        "requests": requests,
        "usd_low": low,
        "usd_high": high,
        "model": model,
        "at": _now().isoformat(),
    }


def _log_committed(run_tag: str, user_id: str | None, leg: str, committed: dict):
    """Log one line with this leg's committed dollars on it, so the spend is
    visible in Cloud Logging without reading the ``batch_runs`` collection.
    """
    log.info(
        "batch.committed",
        run=run_tag,
        user_id=user_id,
        leg=leg,
        requests=committed["requests"],
        usd_low=committed["usd_low"],
        usd_high=committed["usd_high"],
        model=committed["model"],
    )


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _leg_delta(before: dict, after: dict) -> dict[str, int]:
    """What one ingest leg added to the run's cumulative ``counts``.

    ``run["counts"]`` is a running total across both legs, but
    ``persist_run_cost`` applies everything under ``jobs`` with
    ``firestore.Increment``, so handing it the total would re-add the parse
    leg's outcomes on the score leg's flush. A delta is only correct applied
    exactly once, which is what the per-leg ``cost_banked_at`` marker in
    :func:`resume` buys.

    Keys are whatever the legs produced, so a new outcome name flows through
    without a change here. Deliberately no ``pending``: that is a level, not a
    delta, and the cycle that ordered the run already banked it.
    """
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


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
    cycle_id: budget.CycleId = budget.CURRENT_RUN,
    ignore_budget: bool = False,
) -> dict:
    """Submit a resumable batch run for the user's pending backlog.

    **Commits spend**: the batches are submitted but never awaited, and
    cancelling the caller does not cancel them. Free work happens inline
    (jd_cache hits, and the family filter and Pro submission when nothing needs
    Flash). Returns ``{"started": False, "pending": n}`` when ``min_pending``
    says the backlog is too small to bother.

    Budgeted like the online scorer: the reservation is taken before anything
    is loaded and caps how much backlog one run may submit; ``cycle_id``
    behaves as it does there.
    """
    db = db or firestore.AsyncClient()
    reservation = None
    if ignore_budget:
        log.warning("matching.budget_ignored", user_id=user_id, mode="batch_start")
        limit = unbudgeted_limit(limit)
    else:
        reservation = await budget.reserve(db, user_id, limit, cycle_id=cycle_id)
        limit = reservation.granted
        if not limit:
            return {
                "started": False,
                "pending": 0,
                **budget.summary(reservation, drawn=0),
            }

    attempted = 0
    try:
        profile, pending = await load_profile_and_pending(db, user_id, limit)
        if min_pending is not None and len(pending) < min_pending:
            # Nothing was submitted, so nothing is owed. ``drawn=0``, not
            # ``len(pending)``: ``attempted`` is still 0, so the ``finally``
            # refunds the whole grant and ``budget.summary`` must report that
            # same refund or the Profile card under-reports the budget.
            return {
                "started": False,
                "pending": len(pending),
                **budget.summary(reservation, drawn=0),
            }
        # Committed before any submission: _start pays for a batch and only
        # then records its job name, so a Firestore failure on that write must
        # not hand back slots whose requests are already in flight.
        attempted = len(pending)
        return await _start(db, user_id, profile, pending, reservation=reservation)
    finally:
        if reservation is not None:
            await budget.release(
                db,
                user_id,
                reservation.granted - attempted,
                cycle_id=reservation.cycle_id,
            )


async def _start(
    db: firestore.AsyncClient,
    user_id: str,
    profile,
    pending: list[tuple],
    *,
    reservation: budget.Reservation | None,
) -> dict:
    """The submission itself, over an already-budgeted job set."""
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
        # The batch's tokens are only priced when a later process ingests the
        # output, so without this the spend would be attributed to whichever
        # worker tick picked the run up. resume() rebinds it.
        "origin_run_id": current_run_id(),
        # The jobs this run reserved budget for, and therefore the only ones
        # its score stage may submit to Pro. Ingest reloads all pending jobs
        # for the content join and would otherwise sweep in every parsed job
        # an earlier run left behind, against this run's grant.
        "job_ids": [job.id for _, job in pending],
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
        # The committed estimate rides with the job name in the same write,
        # so there is no state in which a run is known and its price is not.
        # Dotted key, so the score leg's entry can be added without clobbering
        # this one.
        committed = _committed(len(to_parse), leg="parse", model=BATCH_FLASH_MODEL)
        await run_ref.update(
            {
                "job_name": job_name,
                "claimed_at": None,
                "committed.parse": committed,
            }
        )
        _log_committed(run_tag, user_id, "parse", committed)
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
            **budget.summary(reservation, drawn=len(pending)),
        }

    # Everything already parsed (cache hits / prior runs): skip straight to
    # the score stage — or straight to done when nothing is in-family.
    await run_ref.set(doc)
    stage = await _submit_score_stage(
        db, run_ref, run_tag, user_id, gcs_root, profile, pending, counts
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
        **budget.summary(reservation, drawn=len(pending)),
    }


async def _persist_prefiltered(ref, job: Job, match, geo_gate: dict | None) -> str:
    """``persist_result`` with the geo record positional, for ``_persist_all``.

    Passes no ``profile`` (see the note at the call site) and no
    ``provenance``, so these tombstones carry no ``scored_with`` at all. That
    is deliberate: no scoring model ran, so ``match_model`` could only be
    ``None``, and the parse is not attributable from here either — this shim
    sees reloaded documents, not the frame that knows which model parsed them.
    A record whose only non-null field is ``scored_at`` dates a write, not a
    score, so the honest answer is no record.

    Known cost: the same OUT_OF_FAMILY job scored through ``batch.py`` gets a
    ``parse_model`` while this path gets nothing. Information loss, never a
    wrong attribution. Closing it needs the parse model threaded through the
    ``batch_runs`` document, a schema change on a live record.
    """
    return await persist_result(ref, job, match, geo_gate=geo_gate)


async def _submit_score_stage(
    db,
    run_ref,
    run_tag: str,
    user_id: str,
    gcs_root: str,
    profile,
    pending,
    counts: dict,
) -> str:
    """Pre-filter parsed pending jobs, then submit the Pro batch. **Spends.**

    Whatever the pre-filter rejects is tombstoned immediately through the same
    persistence path as every other scorer, for free. Unparsed jobs are left
    alone for a future run to retry. Returns the resulting run stage
    ("score", or "done" when nothing needs Pro).

    ``pending`` must already be narrowed to the jobs this run reserved budget
    for (:func:`_owned_pending`): every parsed entry becomes a paid Pro
    request, so a full reload is how a 300-slot run submits 900 jobs.
    """
    tombstones: list[tuple] = []
    to_score: list[tuple] = []
    enforce_geo = geo_enforce_enabled()
    for ref, job in pending:
        if job.jd_parsed is None:
            continue
        match, decision = prefilter(job, profile, enforce=enforce_geo)
        if match is not None:
            enforced = enforced_geo_gate(decision)
            if enforced is not None:
                counts["geo_skipped"] = counts.get("geo_skipped", 0) + 1
            tombstones.append((ref, job, match, enforced))
        else:
            to_score.append((ref, job))
    # No ``profile``, so no geo *shadow* record: these were rejected without a
    # Pro call, so there is no decision to measure the gate against. An
    # enforced geo tombstone carries its own record explicitly instead.
    for outcome in await _persist_all(tombstones, _persist_prefiltered):
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
    # prompted with, and the profile may change while the batch runs, so the
    # context travels with the run.
    await upload_text(f"{gcs_root}/score/context.txt", context)
    job_name = await submit_batch(
        model=BATCH_PRO_MODEL,
        lines=[build_score_request(context, block) for block in by_block],
        gcs_dir=f"{gcs_root}/score",
        display_name=f"hermes-score-{run_tag}",
    )
    # Same rule as the parse leg: job name and price in one write. This is the
    # more expensive leg and it is submitted hours after the user's click, by
    # ``/tasks/batch/resume``. The consent captured at the click covers it only
    # because the estimate is quoted per job over the whole grant rather than
    # per leg; narrow that to one leg and this submission spends unasked.
    committed = _committed(len(by_block), leg="score", model=BATCH_PRO_MODEL)
    await run_ref.update(
        {
            "stage": "score",
            "job_name": job_name,
            "counts": counts,
            "claimed_at": None,
            "updated_at": _now().isoformat(),
            "committed.score": committed,
        }
    )
    _log_committed(run_tag, user_id, "score", committed)
    log.info(
        "batch_runs.score_submitted",
        run=run_tag,
        score_requests=len(by_block),
        **counts,
    )
    return "score"


async def _ingest_parse(db, run_ref, run: dict) -> dict[str, int]:
    """Parse batch finished: feed jd_cache + job docs, then submit the paid
    score batch. Returns this leg's own outcome deltas for the cost ledger.
    """
    run_tag = run_ref.id
    out_lines = await fetch_batch_output(f"{run['gcs_root']}/parse")
    profile, pending = await load_profile_and_pending(db, run["user_id"])

    # Join strictly on texts the batch echoed: pending jobs discovered after
    # submission are not failures, they are just not part of this run.
    texts = {t for t in (_request_text(line) for line in out_lines) if t}
    by_text: dict[str, list[Job]] = {}
    for _, job in pending:
        if job.jd_parsed is None and job.jd_raw in texts:
            by_text.setdefault(job.jd_raw, []).append(job)
    failed = join_parse_responses(out_lines, by_text)

    counts = run.get("counts") or {}
    before = dict(counts)
    counts["parse_failed"] = counts.get("parse_failed", 0) + len(failed)

    parsed_jobs = [j for jobs in by_text.values() for j in jobs if j.jd_parsed]
    if parsed_jobs:
        # Shared cache first, so any user's future run skips Flash, then the
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
    owned = _owned_pending(run, pending, fallback=parsed_jobs)
    log.info(
        "batch_runs.parse_ingested",
        run=run_tag,
        parsed=len(parsed_jobs),
        parse_failed=len(failed),
        # Reloaded vs. this run's own: the gap is other runs' pending jobs,
        # which must not be scored against this run's budget.
        reloaded=len(pending),
        owned=len(owned),
    )
    # Mutates ``counts`` in place with the pre-filter's tombstone outcomes, so
    # the delta below covers the whole leg: failed parse lines and the jobs
    # this leg retired for free before Pro saw them.
    await _submit_score_stage(
        db, run_ref, run_tag, run["user_id"], run["gcs_root"], profile, owned, counts
    )
    return _leg_delta(before, counts)


def _owned_pending(run: dict, pending: list[tuple], *, fallback: list[Job]) -> list:
    """The reloaded pending pairs this run reserved budget for.

    Ingest must reload everything pending, because the content join is what
    makes resuming stateless and truncating would drop jobs whose responses
    this run already paid for. But that reload is a superset — jobs from other
    runs would each become a paid Pro request — so it is narrowed to the ids
    recorded at submit time.

    ``fallback`` covers live ``batch_runs`` docs written before ``job_ids``
    existed: those score only the jobs this run's own batch echoed, and a later
    ``start()`` picks up the remainder under its own budget. It is almost
    always a subset of what the run reserved; the one leak is a posting
    mirrored on a second board and discovered mid-batch, which costs next to
    nothing because identical blocks collapse into one Pro request.
    """
    recorded = run.get("job_ids")
    owned = set(recorded) if recorded else {job.id for job in fallback}
    return [(ref, job) for ref, job in pending if job.id in owned]


async def _ingest_score(db, run_ref, run: dict) -> dict[str, int]:
    """Score batch finished: persist matches/tombstones, complete the run.

    Returns this leg's own outcome deltas for the cost ledger. This is the leg
    that makes ``jobs.scored`` non-zero, and so the only one that makes
    cost-per-scored-job derivable for a batch run.
    """
    run_tag = run_ref.id
    context = await download_text(f"{run['gcs_root']}/score/context.txt")
    out_lines = await fetch_batch_output(f"{run['gcs_root']}/score")
    # The profile is kept only to feed the geo shadow recording below. The
    # verdict is recomputed at ingest rather than carried from submit, so
    # nothing the submitting process knew has to survive.
    profile, pending = await load_profile_and_pending(db, run["user_id"])

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
    before = dict(counts)
    counts["failed"] = counts.get("failed", 0) + len(failed)
    to_persist = [
        (ref_by_id[job.id], job, matches[job.id])
        for jobs in by_block.values()
        for job in jobs
        if job.id in matches
    ]
    # Bound here rather than inside ``_persist_all``, which stays dumb because
    # ``_submit_score_stage`` hands it the same function with no profile bound.
    # ``BATCH_PRO_MODEL``, not ``PRO_MODEL``: they hold the same string today,
    # but they are independent constants and either can move alone.
    # ``parse_model`` is ``None`` on purpose — the parse leg ran in an earlier
    # process and this stateless ingest has no record of what produced those
    # parses. One timestamp for the whole ingest is correct: ``scored_at``
    # dates the write pass, not each document.
    provenance = scored_with(parse_model=None, match_model=BATCH_PRO_MODEL)
    for outcome in await _persist_all(
        to_persist, partial(persist_result, profile=profile, provenance=provenance)
    ):
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
    return _leg_delta(before, counts)


async def _ledger_state(run_ref, run_tag: str) -> str:
    """The originating run's ledger state, read back off the ``batch_runs`` doc.

    The origin run closes when the batch does, not when a leg does, so a parse
    leg that has just submitted the score batch keeps it ``running``. Read back
    rather than inferred from the leg name, because a parse leg with nothing
    left to score completes the whole run, and inferring would leave that
    ledger doc open forever.

    Falls back to ``running`` if the read fails — the state the doc already
    holds — because this must never cost the flush it feeds, which is money.
    """
    try:
        state = ((await run_ref.get()).to_dict() or {}).get("state")
    except Exception:
        log.exception("batch_runs.state_reread_failed", run=run_tag)
        return RUNNING
    if state == "done":
        return DONE
    if state == "failed":
        return FAILED
    return RUNNING


async def resume(
    *,
    user_id: str | None = None,
    db: firestore.AsyncClient | None = None,
) -> dict:
    """One pass over in-flight runs: poll Vertex, ingest whatever finished.

    **Can submit the paid Pro batch** when a parse leg finishes. Cheap when
    nothing is ready (one Vertex GET per running run), and designed to be fired
    repeatedly and in parallel with a manual pass: each run is claimed under an
    update-time precondition before any ingest work, and a claim younger than
    the TTL is skipped.
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

        # Ingest under the originating cycle's run_id: this is where the
        # batch's calls get priced, and pricing them under this tick's id would
        # scatter one cycle's spend across whichever worker passes ingested it.
        # Runs written before origin_run_id existed ingest unattributed.
        origin = run.get("origin_run_id")
        rebind = {"run_id": origin, "runner": "batch_resume"} if origin else {}
        leg = "parse" if run.get("stage") == "parse" else "score"
        banked = bool((run.get("cost_banked_at") or {}).get(leg))
        leg_counts: dict[str, int] | None = None
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
            else:
                with structlog.contextvars.bound_contextvars(**rebind):
                    try:
                        if leg == "parse":
                            leg_counts = await _ingest_parse(db, run_ref, run)
                            summary["advanced"] += 1
                        else:
                            leg_counts = await _ingest_score(db, run_ref, run)
                            summary["completed"] += 1
                    finally:
                        # In a finally so an ingest that dies after pricing
                        # its responses banks that spend rather than losing it,
                        # and so the accumulator entry is always released.
                        #
                        # ``Increment`` is not idempotent and the post-TTL
                        # retry re-prices the same calls, so the flush is gated
                        # on a per-leg ``cost_banked_at`` marker and each leg
                        # reaches the ledger exactly once.
                        #
                        # Bank first, mark second, never the reverse: a crash
                        # between them leaves the leg unmarked and the retry
                        # banks it twice, and over-counting is the safe
                        # direction for the budget cap this feeds. Marking
                        # first would instead lose real spend.
                        #
                        # ``leg_counts`` is None when the ingest raised, so
                        # that flush banks the spend without the outcome
                        # counts and the retry, correctly seeing the leg as
                        # banked, does not supply them later. The run doc's own
                        # ``counts`` still records them; only the ledger's
                        # ``jobs`` breakdown is short.
                        if origin and not banked:
                            await persist_run_cost(
                                db,
                                run["user_id"],
                                origin,
                                batch_run=snap.id,
                                state=await _ledger_state(run_ref, snap.id),
                                jobs=leg_counts,
                            )
                            await run_ref.update(
                                {f"cost_banked_at.{leg}": _now().isoformat()}
                            )
                        elif origin:
                            # Already banked by an earlier attempt: drop the
                            # re-priced totals rather than re-adding them.
                            # persist_run_cost would normally do this release.
                            reset_run_cost(origin)
                            log.info(
                                "batch_runs.cost_already_banked",
                                run=snap.id,
                                leg=leg,
                                run_id=origin,
                            )
        except Exception:
            # Leave the run claimed; after the TTL the next pass retries the
            # idempotent ingest. One bad run must not kill the whole pass.
            log.exception("batch_runs.resume_failed", run=snap.id)

    if summary["checked"]:
        log.info("batch_runs.resume_pass", **summary)
    return summary


async def score_or_start_run(
    user_id: str, *, cycle_id: budget.CycleId = budget.CURRENT_RUN
) -> dict:
    """The scoring seam: online for small backlogs, a resumable batch run for
    big ones. **Spends either way.**

    Returns the online scorer's counts dict in both cases; when a batch run was
    started the LLM outcomes are zero-so-far (results land when a resume tick
    ingests them) and ``batch_run`` carries the run tag.

    ``cycle_id`` passes straight through to both arms. The discovery cycle
    takes the default and opens a window under its own ``run_id``; the ad-hoc
    backlog score passes ``None`` and draws down whatever window is already
    open, so the per-cycle cap cannot be reset by clicking a button twice. That
    is the difference between a cap and a suggestion.
    """
    run = await start(user_id, min_pending=BATCH_MIN_PENDING, cycle_id=cycle_id)
    if not run.get("started"):
        # The unstarted run gave its reservation back, so the online scorer
        # takes its own against the same cycle window.
        return await score_pending_jobs(user_id, cycle_id=cycle_id)
    counts = run["counts"]
    return {
        "scored": counts["scored"],
        "discarded": counts["discarded"],
        "failed": counts["failed"],
        "pending": run["pending"],
        # Zero-so-far like the LLM outcomes above: the geo verdicts are
        # recorded by whichever worker tick ingests this run. Present rather
        # than omitted so both branches return the same key set.
        **EMPTY_GEO_COUNTS,
        # ...except the enforced skips, which cost nothing and are final
        # whenever ``start`` reached the score stage inline.
        "geo_skipped": counts.get("geo_skipped", 0),
        "batch_run": run["run"],
        **{k: v for k, v in run.items() if k.startswith("budget_")},
    }


async def outstanding_committed(db=None, user_id: str | None = None) -> dict:
    """Money committed to Google that our ledger has not yet priced.

    Defined as a query, never as arithmetic on the ledger. Adding the estimate
    at submit and subtracting it at ingest cannot be made correct, because
    ``tools.run_costs`` writes every leaf with ``firestore.Increment`` and the
    "bank first, mark second" window in :func:`resume` means the subtraction
    may run twice or not at all — silently yielding negative committed totals
    or double-counted actuals.

    So committed never touches the ledger doc. It lives on the ``batch_runs``
    doc under an idempotent ``set``, and "outstanding" is just the legs with no
    ``cost_banked_at`` entry, so re-running an ingest cannot corrupt it. A
    failed or orphaned run keeps its committed figure forever, which is the
    honest record of "Google billed this and we never ingested it".

    ``llm.cost_usd`` on the run ledger means actual, priced, ingested spend.
    The two are never summed in code; a UI wanting both shows two lines.
    """
    db = db or firestore.AsyncClient()
    total = {"usd_low": 0.0, "usd_high": 0.0, "runs": 0, "legs": []}
    # ``running`` and ``failed`` both: a failed run's batch was still paid
    # for. ``done`` is excluded because both its legs are banked by
    # definition, and the per-leg filter below would drop it anyway.
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("state", "in", list(OUTSTANDING_STATES))
    )
    if user_id:
        query = query.where(filter=FieldFilter("user_id", "==", user_id))
    async for snap in query.stream():
        run = snap.to_dict() or {}
        committed = run.get("committed") or {}
        banked = run.get("cost_banked_at") or {}
        counted = False
        for leg, entry in committed.items():
            if banked.get(leg):
                continue
            total["usd_low"] += float(entry.get("usd_low") or 0.0)
            total["usd_high"] += float(entry.get("usd_high") or 0.0)
            total["legs"].append(
                {
                    "run": snap.id,
                    "user_id": run.get("user_id"),
                    "leg": leg,
                    "state": run.get("state"),
                    **entry,
                }
            )
            counted = True
        if counted:
            total["runs"] += 1
    total["usd_low"] = round(total["usd_low"], 4)
    total["usd_high"] = round(total["usd_high"], 4)
    return total
