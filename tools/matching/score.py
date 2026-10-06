# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Score pending, unscored jobs and persist the results.

Extracted from ``cli/run_matching.py`` so the auto-discovery scheduler and the
CLI share one implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
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
    MATCH_PROMPT_VERSION,
    PARSE_PROMPT_VERSION,
    PRO_MODEL,
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
# takes no reservation (``ignore_budget``, or a caller that reaches this
# function without passing the gate) must still not stream 13K job docs into
# memory.
SCORE_LIMIT_CEILING = 300

# Cache TTL bounds, in seconds. ``cache_ttl_seconds`` sizes the TTL; these only
# bound it, and both err long because the failure modes are asymmetric. A TTL
# shorter than the run bills the tail's static context block uncached at 10x;
# a TTL longer than the run costs nothing unless the process is killed before
# its finally-block delete, and ``reap_match_caches`` buries that on the next
# run.
#
# The floor keeps a two-job run from being sized off a 12-second estimate and
# binds under ~600 jobs at concurrency 5. The ceiling is deliberately NOT
# derived from SCORING_BUDGET_PER_CYCLE, so raising that knob cannot silently
# push runs past it; it binds only for ``ignore_budget`` backlog runs.
_CACHE_TTL_FLOOR_SECONDS = 3600
_CACHE_TTL_CEILING_SECONDS = 24 * 3600


def unbudgeted_limit(limit: int | None) -> int:
    """The job cap for a run that took no budget reservation: the operator's
    ``--limit`` if given, otherwise :data:`SCORE_LIMIT_CEILING`. Shared by all
    three scoring entry points so ``--ignore-budget`` means the same thing on
    each.
    """
    return limit or SCORE_LIMIT_CEILING


def cache_ttl_seconds(pending: int, concurrency: int) -> int:
    """How long this run's context cache should live (see the bounds above)."""
    estimate = pending * 30 // concurrency
    return min(max(_CACHE_TTL_FLOOR_SECONDS, estimate), _CACHE_TTL_CEILING_SECONDS)


# Jobs scoring at or below this never stay in the `jobs` collection: 0 is the
# out-of-family sentinel and the matching prompt caps geographically
# ineligible roles at exactly 20, so everything down here is a job the user
# cannot or would not take. (The UI already hides anything under 60.)
DISCARD_AT_OR_BELOW = 20


def should_discard(match: JobMatch) -> bool:
    """True when the job is not worth keeping in the user's jobs collection."""
    return match.overall_score <= DISCARD_AT_OR_BELOW


# ------------------------------------------------------- the exploration sample
#
# Every decision this product has collected was made on a job the scorer
# already liked, because the queue hides everything under 60. Labels therefore
# only cover the region above the scorer's own bar, and a model trained on them
# is graded on its own prior. This deterministically surfaces a slice of the
# hidden band so some labels come from outside that belief. Off by default, and
# costs nothing when off.

#: The score at and above which the queue already shows a job: the default
#: ``min_score`` of ``api.routes.jobs.list_pending_jobs``, which is the
#: user-visible contract this follows. Mirrored rather than imported because
#: ``tools`` must not depend on ``api``; the band test asserts they agree.
#:
#: Used as a strict upper bound, never a top of 59: ``overall_score`` is a
#: float, so 59.35 is hidden by the route and ``<= 59`` would miss exactly the
#: slice nearest the decision boundary.
QUEUE_DEFAULT_MIN_SCORE = 60


def exploration_rate() -> float:
    """``EXPLORATION_RATE`` as a fraction in [0, 1]; default ``0`` — off.

    Zero is the correct shipped state, unlike ``GEO_GATE_HOLDOUT``: sampling
    changes what a user is asked to review. Anything unparseable or out of
    range falls back to zero, so a typo means no sampling rather than every
    hidden job surfaced.
    """
    raw = os.getenv("EXPLORATION_RATE", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        log.warning("matching.exploration_rate_invalid", value=raw[:40])
        return 0.0
    if not 0.0 <= value <= 1.0:
        log.warning("matching.exploration_rate_invalid", value=raw[:40])
        return 0.0
    return value


def exploration_sample(job_id: str, rate: float) -> bool:
    """Is this job id in the exploration sample, at ``rate``?

    SHA-256, never :func:`hash`: Python salts ``hash(str)`` per process, so a
    ``hash()``-based sample reshuffles on every worker cold start while passing
    a single-process test. Same construction as ``pipeline.geo_holdout``.

    The id is prefixed before hashing so this and the geo hold-out are
    independent draws rather than the same set of ids at equal rates.
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(b"exploration:" + job_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64 < rate


def should_explore(job: Job, match: JobMatch, geo_gate: dict | None) -> bool:
    """Should this scored job be surfaced out of the hidden band?

    Three conditions, all required: the score is strictly inside
    (``DISCARD_AT_OR_BELOW``, ``QUEUE_DEFAULT_MIN_SCORE``) — strict at both
    ends, because the score is a float and 59.35 is in the band; the job is not
    geo-ineligible; and the id is in :func:`exploration_sample`.

    Only the ``geo_gate`` record's ``verdict`` can decide eligibility here. The
    free gate measured 0 false positives over 1,127 historical scores and again
    over 117 (``tools.matching.geo``). The alternatives cannot be used:
    ``pro_capped`` is ``overall_score == 20`` and so always false inside 21-59,
    and ``pro_geo_flag`` fires on ~1.5% of jobs Pro kept, which would bias the
    very sample this de-biases.

    A missing ``geo_gate`` record is **not** treated as ineligible: Pro caps a
    geographically ineligible role at exactly 20, so a job that reached 21+ is
    one Pro already judged eligible.

    The flag is write-once and there is no rollback. Setting
    ``EXPLORATION_RATE`` back to 0 stops new jobs being sampled but does not
    withdraw the ones already flagged; those keep surfacing until decided.
    Withdrawing a live sample needs an ad-hoc Firestore write over the user's
    ``jobs`` collection — there is no CLI for it.
    """
    if not DISCARD_AT_OR_BELOW < match.overall_score < QUEUE_DEFAULT_MIN_SCORE:
        return False
    if geo_gate is not None and geo_gate.get("verdict") == "ineligible":
        return False
    return exploration_sample(job.id, exploration_rate())


# --------------------------------------------------------- geo gate, in shadow
#
# ``tools.matching.geo`` decides for free what Rule 6 of the scoring prompt
# buys a Pro call to decide (69.4% of every Pro call on the main user came back
# capped at exactly 20). The historical replay proved the gate never wrongly
# rejects but could not measure what it would save, because tombstones carried
# no ``jd_parsed`` to replay against; they do now, so the upside is measurable
# off them going forward. Live recording stays regardless: it is the only
# source of the Pro comparison, which a replay cannot reconstruct.
#
# The gate runs on every scored job and its verdict is written next to what Pro
# said, and nothing else. It skips no call, changes no score and changes no
# discard decision.

# Language Pro reaches for when it rejects a job on geography, matched against
# ``red_flags_hit`` + ``reasoning``.
#
# A disambiguator, not a signal: it is loose enough to fire on ~1.5% of jobs
# Pro kept, so on its own it says almost nothing. Its only use is the ambiguous
# 0-20 band, which is neither the out-of-family sentinel nor the geo cap and
# where Pro's own prose is the only evidence. ``pro_capped`` is the primary
# signal.
#
# Computed here or not at all: ``discard_tombstone`` keeps no
# ``red_flags_hit``, and the discard path is where most geo rejections go, so
# this is the last moment the full ``JobMatch`` exists.
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

    ``None`` — record nothing — whenever there is no honest comparison: no
    ``profile`` (the caller opted out), no ``jd_parsed`` (no inputs), or
    ``overall_score == 0``, the ``pipeline.OUT_OF_FAMILY`` sentinel for a job
    that never reached Pro. That last guard also drops a genuine Pro zero,
    which under-counts true positives and can never manufacture a false
    positive.

    Records raw inputs, not a conclusion — no ``agree`` boolean — so the metric
    can be redefined over data already collected.

    Never raises. It runs inside ``persist_result`` after a Pro call has been
    paid for, so an exception here would throw away work worth real money to
    record a statistic.
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

    ``None`` in, ``None`` out, so callers can pipe ``prefilter``'s decision
    straight through: a family miss carries no decision and so produces no
    record.

    The shape diverges from :func:`shadow_geo_gate`'s deliberately. The
    ``pro_*`` keys are **absent, not null**, because no Pro call was made and a
    null ``pro_score`` beside thousands of real ones invites averaging it in.
    And ``enforced: True`` is the only thing distinguishing these tombstones
    from ``OUT_OF_FAMILY`` ones — both score 0 — so ``cli.geo_resurrect``
    selects on exactly this field.
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
    is a Pro call that could be skipped, ``abstain`` is the coverage the gate
    leaves on the table. ``eligible`` would skip nothing, so it has no counter.
    """
    if record is not None and record["verdict"] in ("ineligible", "abstain"):
        counts[f"geo_{record['verdict']}"] += 1


#: Zeroed geo tallies, spread into every counts dict a scorer can return,
#: including the early-return ones. A counts contract whose key set depends on
#: the branch that produced it is a KeyError waiting to happen.
EMPTY_GEO_COUNTS = {"geo_ineligible": 0, "geo_abstain": 0, "geo_skipped": 0}


# ------------------------------------------------------- score attribution
#
# A score is only comparable to another score if you know what produced it:
# which model ran and which prompt version it ran under. The model ids are
# pinned now, but a pin is only honest if something wrote down which pin was in
# force, and the prompts still move.
#
# The rule that makes this record worth having: stamp the leg that actually ran
# in this call, never the constant naming the leg that usually runs. The batch
# and online paths currently hold the same Flash id, so a wrong stamp is
# invisible until one of them moves and then retroactively mis-attributes every
# job scored under the mix-up. The models stay threaded in from the caller,
# this module never reaches for one itself, and ``tests/unit/test_scored_with``
# pins each path to a sentinel so the tests discriminate by which constant was
# read.


def scored_with(*, parse_model: str | None, match_model: str | None) -> dict:
    """Provenance for one scoring outcome: what ran, under which prompts, when.

    ``parse_model`` / ``match_model`` are the ids the caller actually called,
    or ``None`` for a leg that did not run in this call. Both nulls are
    load-bearing:

    - ``match_model is None`` means no scoring model ran — the outcome came
      from the free pre-filter, not Pro. A consumer filtering for "jobs a model
      scored" tests this key, not the presence of a score.
    - ``parse_model is None`` means this call did not pay for a parse (the job
      arrived already parsed, or hit ``jd_cache``), and which model produced
      that parse is not knowable from here. Never guess it from today's
      constant.

    Each prompt version is stamped only beside the model it versions, for the
    same reason.

    The absence of the whole record proves much less: it means nothing ran
    (``batch_runs._persist_prefiltered``), *or* the job was scored before this
    field existed, *or* a writer forgot to thread it through. Only the first is
    separately identifiable, by score, not by this key.
    ``cli.purge_discarded`` is deliberately not a fourth case — it carries the
    record over, so a Pro-scored job demoted by a threshold change does not
    land in ``discarded_jobs`` looking like the first.

    ``scored_at`` is a tz-aware UTC instant; naive ``utcnow()`` timestamps
    compare wrong against every other timestamp written alongside it.
    """
    return {
        "parse_model": parse_model,
        "match_model": match_model,
        "parse_prompt_version": PARSE_PROMPT_VERSION if parse_model else None,
        "match_prompt_version": MATCH_PROMPT_VERSION if match_model else None,
        "scored_at": datetime.now(UTC).isoformat(),
    }


def restore_payload(job: Job) -> dict:
    """Everything needed to rebuild this ``Job`` from its tombstone, later.

    ``discarded_jobs`` is discovery's dedupe mechanism, not a log: a tombstoned
    posting is never re-persisted or re-scored while it stays live on a board.
    So a wrong enforced ``ineligible`` verdict suppresses that posting
    permanently, not once, and this payload is what makes it reversible —
    ``cli.geo_resurrect`` re-runs the current gate over the stored parse after
    a ``geo.GATE_VERSION`` bump, offline, for free, and without depending on
    the posting still being live.

    Dumps the whole ``Job`` rather than a field list, so restoring is
    ``Job.model_validate(restore)`` with nothing to keep in sync. That includes
    ``jd_raw``, which ``models.job.Job`` requires and which is the tombstone's
    one heavy field — hence written only on enforced tombstones.
    """
    return job.model_dump(mode="json")


def discard_tombstone(
    job: Job,
    match: JobMatch,
    *,
    scored_run_id: str | None = None,
    geo_gate: dict | None = None,
    restore: dict | None = None,
    provenance: dict | None = None,
) -> dict:
    """Minimal `discarded_jobs` record.

    Exists so discovery's seen-check still recognizes the posting and never
    re-persists (and re-pays Flash/Pro to re-score) it while it stays live on
    the board.

    ``scored_run_id``, ``geo_gate`` and ``provenance`` are all passed in rather
    than derived here, because only the caller knows them: a backfill
    (``cli.purge_discarded``) must carry over the run that actually paid to
    score the job, not stamp its own free run, and likewise reads the
    provenance off the doc it is demoting. Each is absent rather than null when
    there is nothing to say, so "we didn't look" stays distinguishable from "we
    looked and found nothing".

    ``restore`` is :func:`restore_payload` and belongs only on tombstones the
    geo gate issued under enforcement. Every other tombstone is a Pro decision,
    which reversing would need the Pro call re-run, so the payload would be
    pure weight there.
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
        # ~71% of a cycle's Pro spend ends up here rather than in `jobs`, so
        # without this the tombstones are the one place the money went that
        # cannot be traced. Same field name as on job docs, so one query
        # answers "what did this run buy?".
        "scored_run_id": scored_run_id,
        # The negatives' only features: ~71% of everything ever scored lands
        # here, and without the parse a tombstone says that a job was rejected
        # but nothing about what was rejected. Always present, ``None`` when
        # the Flash parse never happened (a geo-enforced skip, a backfill), so
        # "no parse" stays distinguishable from "field not written yet".
        #
        # ``jd_raw`` stays out: it is the one heavy field, it is refetchable
        # from ``url``, and ``restore`` carries it on the only tombstones that
        # need rebuilding.
        "jd_parsed": (job.jd_parsed.model_dump(mode="json") if job.jd_parsed else None),
    }
    if geo_gate is not None:
        # Earns its place in an otherwise minimal record: the geo rejections
        # the gate exists to skip land here, not on job docs, so a tombstone
        # without it cannot be measured. Absent rather than null when there
        # was nothing to record.
        stone["geo_gate"] = geo_gate
    if restore is not None:
        stone["restore"] = restore
    if provenance is not None:
        stone["scored_with"] = provenance
    return stone


async def load_profile_and_pending(
    db: firestore.AsyncClient, user_id: str, limit: int | None = None
) -> tuple[MasterProfile, list[tuple]]:
    """The user's profile plus their pending, unscored ``(doc_ref, Job)`` pairs.

    Raises ``ValueError`` when the user has no profile to match against.
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

    The Flash parse is paid work that until this write lives only in process
    memory, so a scoring failure or a dead process re-pays it on the next run.
    Swallows its own errors: scoring can proceed without the write.
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
    provenance: dict | None = None,
) -> str:
    """Persist one scoring outcome; returns ``"discarded"`` or ``"scored"``.

    Deletes the job doc and writes a ``discarded_jobs`` tombstone when the
    match is at or below :data:`DISCARD_AT_OR_BELOW`; otherwise writes
    ``match`` + the parsed JD onto the job doc.

    ``profile`` turns on shadow recording of the geo gate, adding a ``geo_gate``
    map to whichever document this call writes. It changes nothing else — same
    outcome, same score, same discard decision — and is optional so a caller
    with no profile to hand keeps working.

    ``geo_gate`` supplies that map verbatim instead, and is how an enforced skip
    travels (:func:`enforced_geo_gate`). It cannot go through the shadow path,
    which correctly returns ``None`` for ``overall_score <= 0``; passing it here
    also keeps the record and the ``restore`` payload in one statement, so a
    tombstone can never carry one without the other.

    ``provenance`` is :func:`scored_with`. It is passed in because this function
    does not know which models ran — the batch and online scorers call different
    constants — and building it here off an import would mis-attribute every
    batch-scored job indistinguishably from a correct record. Absent, not null,
    when omitted.

    All three scorers go through this one function, which is why the recording
    hangs off it rather than off ``match_job``.
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
                    provenance=provenance,
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
        # Spend attribution: `discovered_at` says when the posting showed up,
        # not when or by which run it was paid to be scored — a backlog scored
        # months later is the normal case.
        "scored_at": datetime.now(UTC).isoformat(),
        "scored_run_id": current_run_id(),
    }
    if geo_gate is not None:
        fields["geo_gate"] = geo_gate
    if provenance is not None:
        fields["scored_with"] = provenance
    if should_explore(job, match, geo_gate):
        # Written only when the job is sampled, never as ``False``, so at the
        # default rate of 0 the job doc is byte-for-byte what it was before
        # this field existed.
        fields["exploration"] = True
    await ref.update(fields)
    return "scored"


async def count_unscored(db, user_id: str) -> int:
    """How many pending jobs still have no ``match`` — the real backlog.

    The one definition of "waiting to be scored" in this codebase, and
    deliberately the same filter :func:`load_profile_and_pending` uses.
    Anything reporting a backlog to the user must mean exactly this.

    Unbudgeted and unlimited on purpose: this is the backlog, not a grant. What
    a click will cover is ``min(this, the grant)``; the grant is
    ``tools.spend.estimate``'s job.

    Costs one streamed query over the user's pending jobs. Never call it from
    the estimate path, which must not query ``jobs`` at all.
    """
    query = (
        db.collection("users")
        .document(user_id)
        .collection("jobs")
        .where(filter=FieldFilter("user_decision", "==", "pending"))
    )
    total = 0
    async for snap in query.stream():
        if "match" not in (snap.to_dict() or {}):
            total += 1
    return total


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

    **Spends money.** Persists ``match`` and the parsed JD onto each job doc,
    unless the job scores at/below ``DISCARD_AT_OR_BELOW``, in which case the
    doc is replaced by a ``discarded_jobs`` tombstone. Returns ``{"scored",
    "discarded", "failed", "pending"}`` plus the ``geo_*`` shadow tallies and
    :func:`tools.matching.budget.summary`'s ``budget_*`` fields. Raises
    ``ValueError`` when the user has no profile.

    How many jobs the run may score is decided before anything is loaded, by
    one budget reservation whose grant becomes the query's limit; unused slots
    are refunded when it ends. ``cycle_id`` defaults to the ambient run, which
    opens a new cycle window; pass ``None`` to draw down the window already
    open instead, in which case an exhausted window yields zero slots until a
    discovery cycle opens the next one.

    ``ignore_budget`` skips the cap entirely and is reachable only from
    ``cli.run_matching --ignore-budget``. Such a run is still bounded by
    ``limit`` or :data:`SCORE_LIMIT_CEILING`.
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

    # Counted as jobs finish rather than from the returned counts: a run
    # cancelled out from under us after paying for hundreds of Pro calls must
    # not have those slots refunded just because no counts dict came back. A
    # hard SIGKILL skips the finally entirely, which fails safe.
    progress = {"attempted": 0}
    try:
        counts = await _score_pending(
            db, user_id, limit, concurrency, on_result, progress
        )
        # ``progress["attempted"]``, not ``counts["pending"]``: the same
        # number the refund below subtracts, so what the summary says was
        # drawn and what the reservation keeps can never disagree.
        return {**counts, **budget.summary(reservation, drawn=progress["attempted"])}
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

    # One Vertex context cache for the static scoring block, shared by every
    # job in this run: the block dominates input tokens and cached input bills
    # at a tenth of the standard rate. A mid-run expiry or a None from
    # create_match_cache just means the run prices like an uncached one.
    cache_name: str | None = None
    if len(pending) >= 2:
        cache_name = await create_match_cache(
            profile, ttl_seconds=cache_ttl_seconds(len(pending), concurrency)
        )

    # Read once per run, not per job, so "was this run enforcing?" is
    # answerable from the log line below.
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
            # The parse model *this call* paid for, or None when it did not:
            # an already-parsed job or a jd_cache hit came from an earlier run
            # under a model this scorer cannot name. See :func:`scored_with`.
            parse_model: str | None = None
            try:
                # Parse here, not inside match_job, so the result is durable
                # before the Pro call can fail. Cheapest source first: the
                # cross-user jd_cache, then Flash.
                if job.jd_parsed is None:
                    job.jd_parsed = await jd_cache.lookup(db, job.jd_raw)
                    if job.jd_parsed is None:
                        job.jd_parsed = await parse_jd(job)
                        parse_model = FLASH_MODEL
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
                        # A call-site concern, not prefilter's: the batch
                        # paths tombstone in bulk and report counts, so moving
                        # this into prefilter would start two new log streams.
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
                        ref,
                        job,
                        skipped,
                        profile=profile,
                        geo_gate=enforced,
                        # No ``match_model``: the pre-filter is a free local
                        # rule, and this field must never claim Pro ran on a
                        # job Pro never saw. The parse leg is still attributed
                        # when this call paid for it, since the tombstone keeps
                        # that parse as its only features.
                        provenance=scored_with(
                            parse_model=parse_model, match_model=None
                        )
                        if parse_model
                        else None,
                    )
                    counts[outcome] += 1
                    if on_result:
                        on_result(job, skipped, None)
                    return
                match = await match_job(job, profile, cached_content=cache_name)
                outcome = await persist_result(
                    ref,
                    job,
                    match,
                    profile=profile,
                    provenance=scored_with(
                        parse_model=parse_model, match_model=PRO_MODEL
                    ),
                )
                counts[outcome] += 1
                # Recomputed rather than returned by persist_result: the gate
                # is pure and costs microseconds, and widening that return
                # would break callers who index a counts dict with it.
                count_geo_gate(counts, shadow_geo_gate(job, match, profile))
                if on_result:
                    on_result(job, match, None)
            except Exception as e:
                counts["failed"] += 1
                log.exception("match.failed", job_id=job.id, company=job.company)
                if on_result:
                    on_result(job, None, str(e))
            finally:
                # Holding the semaphore draws the slot, whatever happens next,
                # including cancellation partway through a billed Pro call.
                # Jobs still queued behind the semaphore never reach this, so
                # their slots are correctly refunded.
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
