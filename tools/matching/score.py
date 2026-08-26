# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Score pending, unscored jobs and persist the results.

Extracted from ``cli/run_matching.py`` so the auto-discovery scheduler and the
CLI share one implementation.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.match import JobMatch
from models.profile import MasterProfile
from obs.logging import current_run_id, get_logger
from tools.matching import budget, geo, jd_cache
from tools.matching.pipeline import (
    FLASH_MODEL,
    create_match_cache,
    delete_match_cache,
    geo_enforce_enabled,
    match_job,
    parse_jd,
    prefilter,
)

log = get_logger("tools.matching")

# (job, match, error) — match is None when scoring failed and error says why.
OnResult = Callable[[Job, JobMatch | None, str | None], None]

# Belt and braces. The budget reservation is the real cap, but a run that
# takes no reservation (``ignore_budget``, or some future caller that reaches
# this function without passing the gate) must still not stream an unbounded
# backlog into memory — 13K job docs is what this exists to prevent.
SCORE_LIMIT_CEILING = 300

# Cache TTL bounds, in seconds. The estimate below (~30s per Pro call per
# concurrency slot) is what actually sizes the TTL; these only bound it, and
# both are deliberately generous, because the two failure modes are not
# symmetric. A TTL shorter than the run means the tail bills the static
# context block uncached at 10x — real money, on every run that overruns the
# estimate. A TTL longer than the run costs nothing at all unless the run is
# killed before its finally-block delete, and ``reap_match_caches`` buries
# that corpse on the next run. So: err long.
#
# The floor keeps a two-job run from being sized off a 12-second estimate; it
# binds under ~600 jobs at concurrency 5, which includes a default-sized cycle
# — that run gets headroom over its estimate rather than exactly none. (A
# measured 100-job run took 170s at concurrency 5, so the estimate itself runs
# well ahead of reality; the floor is what makes that safe rather than tight.)
# The ceiling is NOT derived from SCORING_BUDGET_PER_CYCLE and sits far enough
# above it that raising that knob can't silently push runs past it (a cost
# regression hidden behind a cost knob); it binds only for ``ignore_budget``
# backlog runs, over ~14,400 jobs at concurrency 5.
_CACHE_TTL_FLOOR_SECONDS = 3600
_CACHE_TTL_CEILING_SECONDS = 24 * 3600


def unbudgeted_limit(limit: int | None) -> int:
    """The job cap for a run that took no budget reservation.

    Shared by all three scoring entry points so ``--ignore-budget`` means the
    same thing on every one of them: an explicit ``--limit`` if the operator
    gave one, otherwise the ceiling.
    """
    return limit or SCORE_LIMIT_CEILING


def cache_ttl_seconds(pending: int, concurrency: int) -> int:
    """How long this run's context cache should live (see the bounds above)."""
    estimate = pending * 30 // concurrency
    return min(max(_CACHE_TTL_FLOOR_SECONDS, estimate), _CACHE_TTL_CEILING_SECONDS)


# Jobs scoring at or below this never stay in the `jobs` collection: 0 is the
# out-of-family sentinel and the matching prompt caps geographically
# ineligible roles at 20, so everything down here is a job the user cannot or
# would not take. (The UI already hides anything under 60.)
DISCARD_AT_OR_BELOW = 20


def should_discard(match: JobMatch) -> bool:
    """True when the job is not worth keeping in the user's jobs collection."""
    return match.overall_score <= DISCARD_AT_OR_BELOW


# --------------------------------------------------------- geo gate, in shadow
#
# ``tools.matching.geo`` decides for free what Rule 6 of the scoring prompt
# currently buys a Pro call to decide (69.4% of every Pro call ever made on the
# main user came back capped at exactly 20 — geographically ineligible). The
# replay against history proved the gate never *wrongly* rejects, but it cannot
# prove what it would *save*: ``persist_result`` tombstones every capped score
# out of the `jobs` collection and ``discard_tombstone`` carries no
# ``jd_parsed``, so the gate has nothing to replay against exactly where its
# upside lives. Live recording is the only way to measure it.
#
# So the gate runs on every scored job and its verdict is written down next to
# what Pro said — and nothing else. It skips no call, changes no score, and
# changes no discard decision. Acting on it is a later phase, and that phase
# gets to make its case out of this data.

# Language Pro reaches for when it rejects a job on geography, matched against
# ``red_flags_hit`` + ``reasoning``.
#
# **This is a disambiguator, not a signal.** It is deliberately loose — it also
# fires on ~1.5% of jobs Pro *kept* (17 of 1,127 in the historical corpus), so
# on its own it says almost nothing. Its one job is the ambiguous band: a score
# strictly between 0 and 20 is neither the out-of-family sentinel nor the geo
# cap, and 37 historical records sit there. For those, "did Pro's own prose
# mention geography?" is the only evidence available. Never read it as the
# primary signal; ``pro_capped`` is that.
#
# It has to be computed *here* or not at all: ``discard_tombstone`` writes a
# deliberately minimal record with no ``red_flags_hit``, and the discard path is
# where most geo rejections go — so this is the last moment the full ``JobMatch``
# still exists.
_PRO_GEO_LANGUAGE = re.compile(
    r"geograph|relocat|time ?zone|ineligib"
    r"|\bvisa\b|work (?:authoriz|permit)"
    r"|\bresiden|\bbased in\b|\blocated in\b"
    r"|\bon-?site\b|\bhybrid\b|\bin-office\b",
    re.IGNORECASE,
)


def pro_geo_flag(match: JobMatch) -> bool:
    """Does Pro's own prose mention geography? See ``_PRO_GEO_LANGUAGE``."""
    return bool(
        _PRO_GEO_LANGUAGE.search(" ".join([*match.red_flags_hit, match.reasoning]))
    )


def shadow_geo_gate(
    job: Job, match: JobMatch, profile: MasterProfile | None
) -> dict | None:
    """What the geo gate would have said about this job, ready to record.

    ``None`` — record nothing — whenever there is no honest comparison to make:

    - no ``profile``, so the caller opted out (or predates this phase);
    - no ``jd_parsed``, so the gate has no inputs;
    - ``overall_score == 0``, the ``pipeline.OUT_OF_FAMILY`` sentinel. That
      job was rejected by the free family pre-filter and never reached Pro, so
      there is no Pro decision to agree or disagree with. The same guard also
      drops a genuine Pro zero, which under-counts true positives and can never
      manufacture a false positive — the safe direction to be wrong in.

    What lands in Firestore is **raw inputs, not a conclusion**: no ``agree``
    boolean. The (0, 20) band is genuinely ambiguous, and storing the fields
    rather than a verdict-on-a-verdict lets the metric be redefined over data
    already collected instead of re-running anything.

    Never raises. A measurement that can break the thing it measures is worse
    than no measurement: this runs inside ``persist_result``, after a Pro call
    has already been paid for, so an exception here would throw away work worth
    real money to record a statistic.
    """
    if profile is None or job.jd_parsed is None or match.overall_score <= 0:
        return None
    try:
        decision = geo.evaluate(job.jd_parsed, profile)
    except Exception as e:
        log.warning("matching.geo_shadow_failed", job_id=job.id, error=str(e)[:200])
        return None
    return {
        "version": geo.GATE_VERSION,
        "verdict": decision.verdict,
        "rule": decision.rule,
        "residence_country": decision.residence_country,
        "job_country": decision.job_country,
        "pro_score": match.overall_score,
        # Exactly 20, not <=: a weighted score that merely lands under the
        # discard threshold is a bad match, not a geo rejection.
        "pro_capped": match.overall_score == DISCARD_AT_OR_BELOW,
        "pro_geo_flag": pro_geo_flag(match),
    }


def enforced_geo_gate(decision: geo.GeoDecision | None) -> dict | None:
    """The ``geo_gate`` record for a job the gate *skipped*, not merely watched.

    ``None`` in, ``None`` out: ``pipeline.prefilter`` returns a decision beside
    its sentinel only when the geo gate is what rejected the job, so a family
    miss (decision ``None``) produces no record and every caller can pipe the
    two straight through without branching.

    The shape deliberately diverges from :func:`shadow_geo_gate`'s in two ways.
    It carries **no ``pro_*`` keys at all** — not nulls, absent — because no Pro
    call was made and a null ``pro_score`` sitting next to 7,000 real ones is an
    invitation to average it in. And it carries ``enforced: True``, which is the
    *only* thing that distinguishes these tombstones from ``OUT_OF_FAMILY``
    ones: both score 0 (see :data:`pipeline.GEO_INELIGIBLE`), so score cannot do
    it. ``cli.geo_resurrect`` selects on exactly this field.
    """
    if decision is None:
        return None
    return {
        "version": geo.GATE_VERSION,
        "verdict": decision.verdict,
        "rule": decision.rule,
        "residence_country": decision.residence_country,
        "job_country": decision.job_country,
        "enforced": True,
    }


def count_geo_gate(counts: dict, record: dict | None) -> None:
    """Tally one shadow verdict into a scorer's counts dict.

    Only the two verdicts that would change anything are counted: ``ineligible``
    is the Pro call a later phase could skip, ``abstain`` is the coverage this
    gate leaves on the table. ``eligible`` is reached only by the ``us_remote_ok``
    exception and would skip nothing, so it has no counter.
    """
    if record is not None and record["verdict"] in ("ineligible", "abstain"):
        counts[f"geo_{record['verdict']}"] += 1


#: Zeroed geo tallies, spread into every counts dict a scorer can return —
#: including the ones it returns without scoring anything. A counts contract
#: whose key set depends on which branch produced it is one KeyError waiting
#: for whoever reads these numbers next.
EMPTY_GEO_COUNTS = {"geo_ineligible": 0, "geo_abstain": 0, "geo_skipped": 0}


def restore_payload(job: Job) -> dict:
    """Everything needed to rebuild this ``Job`` from its tombstone, later.

    **Why a tombstone carries a copy of the job at all.** ``discarded_jobs`` is
    not a log, it is discovery's dedupe mechanism: ``discovery.pipeline``
    checks the tombstone *before* the job doc, so a tombstoned posting is never
    re-persisted and never re-scored while it stays live on a board. Under
    enforcement a wrong ``ineligible`` verdict is therefore not "one job lost" —
    it is that posting permanently suppressed, on every future re-discovery,
    with nothing anywhere recording that a machine decided it.

    **Why carry the job rather than just delete the tombstone and let discovery
    re-find the posting.** A ``geo.GATE_VERSION`` bump has to be resolvable
    *offline and for free*: stream the enforced tombstones, re-run the current
    gate over the parse stored here, resurrect exactly the ones whose verdict
    changed (``cli.geo_resurrect``). That is deterministic and unit-testable,
    depends on no posting still being live on a board months later, and re-pays
    for no Flash parse. Deleting the tombstone instead would hand the correction
    to a crawl we do not control and cannot replay.

    The whole ``Job`` is dumped rather than a hand-picked field list, so
    restoring is ``Job.model_validate(restore)`` with nothing to keep in sync —
    and note ``jd_raw`` is *required* by ``models.job.Job``, which is why the
    minimal tombstone can restore nothing at all. It is the tombstone's one
    heavy field, and it is written only on enforced tombstones.
    """
    return job.model_dump(mode="json")


def discard_tombstone(
    job: Job,
    match: JobMatch,
    *,
    scored_run_id: str | None = None,
    geo_gate: dict | None = None,
    restore: dict | None = None,
) -> dict:
    """Minimal `discarded_jobs` record.

    Exists so discovery's seen-check still recognizes the posting and never
    re-persists (and re-pays Flash/Pro to re-score) it while it stays live on
    the board.

    ``scored_run_id`` is explicit rather than read from the ambient context so
    a backfill (``cli.purge_discarded``) can carry over the run that actually
    paid to score the job instead of stamping its own free run. ``geo_gate`` is
    explicit for the same reason — the caller decides whether there was
    anything worth recording; see :func:`shadow_geo_gate`.

    ``restore`` is :func:`restore_payload`, and belongs only on tombstones the
    geo gate issued under enforcement. Every other tombstone is a *Pro*
    decision: reversing it would need the Pro call re-run, not a stored copy of
    the job, so carrying the payload there would be pure weight.
    """
    stone = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "score": match.overall_score,
        "recommendation": match.recommendation,
        "reasoning": match.reasoning,
        "discarded_at": datetime.now(UTC).isoformat(),
        # Most of a cycle's Pro spend ends up here rather than in `jobs`
        # (71% of the 12K backlog tombstoned), so without this the tombstones
        # are the one place the money went that can't be traced. Same field
        # name as on the job docs — one query answers "what did this run buy?".
        "scored_run_id": scored_run_id,
    }
    if geo_gate is not None:
        # The one field that earns a place in an otherwise minimal record: the
        # geo rejections this whole gate exists to skip land *here*, not on job
        # docs, so a tombstone without it is a measurement that can never be
        # taken. Absent rather than null when there was nothing to record — the
        # analysis counts documents that carry a verdict, and "we didn't look"
        # must stay distinguishable from "we looked and found nothing".
        stone["geo_gate"] = geo_gate
    if restore is not None:
        stone["restore"] = restore
    return stone


async def load_profile_and_pending(
    db: firestore.AsyncClient, user_id: str, limit: int | None = None
) -> tuple[MasterProfile, list[tuple]]:
    """The user's profile plus their pending, unscored ``(doc_ref, Job)`` pairs.

    Shared by the online scorer below and the batch scorer in
    ``tools.matching.batch``. Raises ``ValueError`` when the user has no
    profile to match against.
    """
    profile_doc = await db.collection("users").document(user_id).get()
    if not profile_doc.exists:
        raise ValueError(f"No profile at users/{user_id}.")
    profile = MasterProfile.model_validate(profile_doc.to_dict())

    jobs_ref = db.collection("users").document(user_id).collection("jobs")
    query = jobs_ref.where(filter=FieldFilter("user_decision", "==", "pending"))

    pending: list[tuple] = []
    async for snap in query.stream():
        d = snap.to_dict()
        if "match" in d:  # already scored
            continue
        pending.append((snap.reference, Job.model_validate(d)))
        if limit and len(pending) >= limit:
            break
    return profile, pending


async def persist_jd_parsed(ref, job: Job) -> None:
    """Persist the parse result the moment it exists, ahead of scoring.

    The Flash parse is paid work; until this write it lives only in process
    memory, so a scoring failure (or a dead process, in batch mode) re-pays it
    on the next run — 467 such re-pays observed in the 12K backlog. Best-effort
    by design: scoring can proceed without the write, so a Firestore hiccup
    here must not fail the job.
    """
    if job.jd_parsed is None:
        return
    try:
        await ref.update({"jd_parsed": job.jd_parsed.model_dump(mode="json")})
    except Exception:
        log.warning("matching.persist_jd_parsed_failed", job_id=job.id)


async def persist_result(
    ref,
    job: Job,
    match: JobMatch,
    *,
    profile: MasterProfile | None = None,
    geo_gate: dict | None = None,
) -> str:
    """Persist one scoring outcome; returns ``"discarded"`` or ``"scored"``.

    Discarding replaces the job doc with a ``discarded_jobs`` tombstone (see
    :func:`discard_tombstone`); anything else writes ``match`` + the parsed JD
    onto the job doc.

    ``profile`` turns on shadow recording of the geo gate: a ``geo_gate`` map
    goes onto whichever document this call writes. It changes nothing else —
    same outcome, same score, same discard decision, with it or without it.
    It is keyword-only and optional so that a caller with no profile to hand
    (``cli.purge_discarded``, a future backfill) keeps working unchanged.

    ``geo_gate`` supplies that map *verbatim* instead, and is how an enforced
    skip travels (:func:`enforced_geo_gate`). It cannot go through the shadow
    path: :func:`shadow_geo_gate` returns ``None`` for ``overall_score <= 0``,
    correctly, because there is no Pro decision to compare against — and an
    enforced skip is exactly a record with no Pro decision. Passing it here also
    keeps the record and the ``restore`` payload written by the same statement,
    so a tombstone can never come out carrying one and not the other.

    This is the seam the recording hangs off rather than ``match_job`` because
    it is the *one* function all three scorers go through. Instrumenting the
    scorers individually is how the cheap path ships silently uninstrumented.
    """
    if geo_gate is None:
        geo_gate = shadow_geo_gate(job, match, profile)
    # Only enforced tombstones are reversible, and only they need to be: see
    # :func:`restore_payload`. A Pro-issued discard is a judgement, not a
    # machine-provable claim, and carries no copy of the job.
    restore = restore_payload(job) if geo_gate and geo_gate.get("enforced") else None
    if should_discard(match):
        # ref.parent is the jobs collection; its parent is the user doc.
        user_ref = ref.parent.parent
        await (
            user_ref.collection("discarded_jobs")
            .document(job.id)
            .set(
                discard_tombstone(
                    job,
                    match,
                    scored_run_id=current_run_id(),
                    geo_gate=geo_gate,
                    restore=restore,
                )
            )
        )
        await ref.delete()
        log.info(
            "matching.discarded",
            job_id=job.id,
            company=job.company,
            score=match.overall_score,
        )
        return "discarded"
    fields = {
        "match": match.model_dump(mode="json"),
        "jd_parsed": (job.jd_parsed.model_dump(mode="json") if job.jd_parsed else None),
        # Spend attribution: `discovered_at` says when the posting showed
        # up, which is not when (or by which run) it was paid to be scored
        # — a backlog scored months later is the normal case.
        "scored_at": datetime.now(UTC).isoformat(),
        "scored_run_id": current_run_id(),
    }
    if geo_gate is not None:
        fields["geo_gate"] = geo_gate
    await ref.update(fields)
    return "scored"


async def score_pending_jobs(
    user_id: str,
    *,
    limit: int | None = None,
    concurrency: int = 5,
    on_result: OnResult | None = None,
    cycle_id: budget.CycleId = budget.CURRENT_RUN,
    ignore_budget: bool = False,
) -> dict:
    """Score every pending, unscored job against the user's profile.

    Persists ``match`` (and the parsed JD) onto each job doc — unless the job
    scores at/below ``DISCARD_AT_OR_BELOW``, in which case the doc is replaced
    by a tombstone in ``discarded_jobs`` so it never reaches the queue but is
    still deduped on future discovery runs. Returns ``{"scored": n,
    "discarded": n, "failed": n, "pending": n}`` plus the ``geo_*`` shadow
    tallies (:func:`count_geo_gate`) and the ``budget_*`` fields of
    :func:`tools.matching.budget.summary`. Raises ``ValueError`` when the user
    has no profile to match against.

    How many jobs this run may score is decided *before* anything is loaded,
    by one budget reservation (``tools.matching.budget``); what it grants
    becomes the query's limit. Slots the run doesn't end up drawing on are
    refunded when it ends. ``cycle_id`` defaults to the ambient run, which
    opens a new cycle window; pass ``None`` (as the worker's ad-hoc score task
    does) to draw down the window already open instead — note that an
    exhausted window then yields zero slots until a discovery cycle opens the
    next one, since the cycle counter has no time-based rollover.

    ``ignore_budget`` skips the gate entirely and is reachable only from
    ``cli.run_matching --ignore-budget`` — the operator workflow for
    hand-scoring a backlog, which is otherwise blocked by its own cap. Such a
    run is still bounded by ``limit`` or :data:`SCORE_LIMIT_CEILING`.
    """
    db = firestore.AsyncClient()
    reservation = None
    if ignore_budget:
        log.warning("matching.budget_ignored", user_id=user_id, limit=limit)
        limit = unbudgeted_limit(limit)
    else:
        reservation = await budget.reserve(db, user_id, limit, cycle_id=cycle_id)
        limit = reservation.granted
        if not limit:
            # Nothing left in this cycle/day. A normal outcome, not an error:
            # the backlog waits for the next window.
            return {
                "scored": 0,
                "discarded": 0,
                "failed": 0,
                "pending": 0,
                **EMPTY_GEO_COUNTS,
                **budget.summary(reservation, drawn=0),
            }

    # Counted as jobs finish rather than from the returned counts: the run can
    # be cancelled out from under us (worker shutdown, the 1800s Cloud Run
    # timeout) after paying for hundreds of Pro calls, and refunding those
    # slots because no counts dict came back is the one direction that costs
    # money. A hard SIGKILL skips the finally entirely, which fails safe.
    progress = {"attempted": 0}
    try:
        counts = await _score_pending(
            db, user_id, limit, concurrency, on_result, progress
        )
        return {**counts, **budget.summary(reservation, drawn=counts["pending"])}
    finally:
        if reservation is not None:
            await budget.release(
                db,
                user_id,
                reservation.granted - progress["attempted"],
                cycle_id=reservation.cycle_id,
            )


async def _score_pending(
    db: firestore.AsyncClient,
    user_id: str,
    limit: int | None,
    concurrency: int,
    on_result: OnResult | None,
    progress: dict,
) -> dict:
    """The scoring run itself, inside whatever budget was granted for it."""
    profile, pending = await load_profile_and_pending(db, user_id, limit)

    started = time.monotonic()

    # One Vertex context cache for the static scoring block (profile + rules),
    # shared by every job in this run — the block dominates input tokens and
    # cached input bills at a tenth of the standard rate. TTL is sized to the
    # backlog by cache_ttl_seconds above. match_job falls back to the uncached
    # prompt if the cache expires mid-run, and create_match_cache returning
    # None (e.g. block under the model's minimum cacheable size) just means
    # the run prices like before.
    cache_name: str | None = None
    if len(pending) >= 2:
        cache_name = await create_match_cache(
            profile, ttl_seconds=cache_ttl_seconds(len(pending), concurrency)
        )

    # Read once per run, not per job: the env cannot change mid-run, and one
    # value per run is what makes "was this run enforcing?" answerable from the
    # log line below.
    enforce_geo = geo_enforce_enabled()
    log.info(
        "matching.start",
        pending=len(pending),
        concurrency=concurrency,
        context_cache=cache_name is not None,
        geo_enforce=enforce_geo,
    )
    sem = asyncio.Semaphore(concurrency)
    counts = {
        "scored": 0,
        "discarded": 0,
        "failed": 0,
        "pending": len(pending),
        **EMPTY_GEO_COUNTS,
    }

    async def _score(ref, job: Job) -> None:
        async with sem:
            try:
                # Parse here (not inside match_job) so the result is durable
                # before the Pro call gets a chance to fail. Cheapest source
                # first: the cross-user jd_cache, then Flash.
                if job.jd_parsed is None:
                    job.jd_parsed = await jd_cache.lookup(db, job.jd_raw)
                    if job.jd_parsed is None:
                        job.jd_parsed = await parse_jd(job)
                        await jd_cache.store(
                            db, job.jd_raw, job.jd_parsed, model=FLASH_MODEL
                        )
                    await persist_jd_parsed(ref, job)
                # Free rejections before the paid call, in the one place all
                # three scorers share. A non-None decision beside the sentinel
                # means the geo gate is what rejected it, not the family test.
                skipped, decision = prefilter(job, profile, enforce=enforce_geo)
                if skipped is not None:
                    enforced = enforced_geo_gate(decision)
                    if enforced is None:
                        # The log the pre-filter used to emit from inside
                        # match_job. It stays a call-site concern: the batch
                        # paths have never emitted it (they tombstone in bulk
                        # and report counts), and moving it into prefilter would
                        # start two new log streams.
                        log.info(
                            "matching.skip_out_of_family",
                            job_id=job.id,
                            company=job.company,
                            role_family=job.jd_parsed.role_family
                            if job.jd_parsed
                            else None,
                        )
                    else:
                        counts["geo_skipped"] += 1
                    outcome = await persist_result(
                        ref, job, skipped, profile=profile, geo_gate=enforced
                    )
                    counts[outcome] += 1
                    if on_result:
                        on_result(job, skipped, None)
                    return
                match = await match_job(job, profile, cached_content=cache_name)
                outcome = await persist_result(ref, job, match, profile=profile)
                counts[outcome] += 1
                # Recomputed rather than handed back by persist_result: the
                # gate is pure and costs microseconds, and one definition of
                # "is there anything to record here?" beats two. Widening
                # persist_result's return would also break its callers, who
                # index a counts dict with it.
                count_geo_gate(counts, shadow_geo_gate(job, match, profile))
                if on_result:
                    on_result(job, match, None)
            except Exception as e:
                counts["failed"] += 1
                log.exception("match.failed", job_id=job.id, company=job.company)
                if on_result:
                    on_result(job, None, str(e))
            finally:
                # A job that got as far as holding the semaphore has drawn its
                # slot, whatever happened next — including a cancellation
                # partway through a (billed) Pro call. Jobs still queued
                # behind the semaphore when a run is cancelled never run this,
                # so their slots are correctly refunded.
                progress["attempted"] += 1

    try:
        await asyncio.gather(*(_score(ref, job) for ref, job in pending))
    finally:
        if cache_name:
            await delete_match_cache(cache_name)
    log.info(
        "matching.done",
        duration_ms=int((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
