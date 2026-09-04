# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Matching pipeline: parse a JD (Flash) then score it against the profile (Pro).

Deterministic engine, run via cli/run_matching.py. A cheap pre-filter
(:func:`prefilter`) drops out-of-target roles — and, under ``GEO_GATE_ENFORCE``,
provably unreachable ones — before the expensive Pro scoring call. Model ids
come from :mod:`tools.llm_models`.
"""

from __future__ import annotations

import hashlib
import os
import time

from google.genai import types

from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile
from obs.llm_cost import record_llm_call
from obs.logging import get_logger
from tools.genai_client import vertex_client
from tools.llm_models import FLASH_MODEL, PRO_MODEL
from tools.matching import geo

log = get_logger("tools.matching")

# FLASH_MODEL / PRO_MODEL are imported above from tools.llm_models, the single
# home for these ids. Parsing is high-volume → Flash; scoring is the call worth
# paying for → Pro.

# Thinking bills as output tokens at the full output rate — telemetry showed it
# running 1.5x-4x the answer size on both calls below with no thinking_config
# set at all. Both tasks still need real judgment (role/seniority classification
# here; weighted scoring + geo-eligibility gating in match_job), so this trims
# the default rather than disabling thinking outright — see obs/llm_cost.py
# output post-deploy to confirm thinking_tokens actually dropped before going
# lower.
#
# gemini-flash-latest currently serves a 2.5-generation model: it 400s on
# thinking_level ("not supported by this model") and takes the older
# thinking_budget knob instead — verified live 2026-07-08 after every parse_jd
# call in a backlog run failed with that 400. 512 tokens caps thinking near the
# thinking_level=LOW intent. If the alias moves to a 3.x Flash, thinking_budget
# still works (3.x accepts either knob, just not both).
_PARSE_JD_THINKING = types.ThinkingConfig(thinking_budget=512)
_MATCH_THINKING = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM)

# Ceiling on what one Pro scoring call may generate — thinking counts toward
# max_output_tokens, so this must cover answer + thinking. All-time telemetry
# worst is ~3.0K combined (427 answer, 2,622 thinking); 4096 leaves headroom
# while capping a runaway generation at ~$0.05 instead of the model's ~64K
# default (~$0.79). Hitting the cap truncates the JSON, which fails schema
# validation and surfaces as match.failed rather than a silent wrong score.
_MATCH_MAX_OUTPUT_TOKENS = 4096

PARSE_JD_PROMPT = """Extract structured info from this job description.

For role_family, classify into exactly one of: engineering, product, design, data,
marketing, sales, customer-success, operations, finance, people, legal, other.
Cross-functional titles map to their primary function: 'Solutions Engineer' → engineering,
'Technical Product Manager' → product, 'Developer Advocate' → marketing (or engineering
if the role is mostly building), 'Sales Engineer' → sales. Use 'other' only when genuinely unclear.

For red_flags, look for signals that generalize across functions: vague or missing comp,
unrealistic scope for the level, 'wear many hats' / 'do more with less' (understaffing),
'fast-paced' used as a warning, 'family culture' (boundary issues), 'rockstar'/'ninja'
(eng), or 'must thrive in ambiguity' without senior comp. Adapt to the role's function.

For seniority, infer from required years of experience, scope, and title. Two tracks:
- IC track: 0-2 yrs → junior; 2-5 → mid; 5-8 → senior; 8-12 → staff; 12+ → principal
- Management track: 'Manager' → manager; 'Senior Manager'/'Group' → senior-manager;
  'Director'/'Head of' → director; 'VP'/'Vice President' → vp
Pick the track that matches the title. These levels apply across all functions at tech companies.

For location, extract the job's geography from the posting and the location line:
- job_country / job_state / job_city: the physical work location. For multi-site
  postings, pick the primary one. Leave any field null when the posting does not state it.
- remote_policy: remote / hybrid / onsite (as above).
- remote_scope: for remote roles, where remote workers may be based, e.g. 'United States',
  'US-only', 'Europe', 'EMEA', 'Worldwide', 'LATAM'. null when the role is onsite/hybrid
  or the scope is unstated.
- us_remote_ok: true ONLY if the JD explicitly allows US-based remote workers (e.g.
  'Remote - US', 'US remote', 'remote anywhere in the US', 'US-based remote'). Otherwise false.
  Do not infer this from the company being US-headquartered; require an explicit statement.
"""

# The scoring prompt is split into a per-user static block and a per-job block
# so the static block (profile JSON + geography + decision patterns + scoring
# rules — it dominates input tokens and was resent on every call) can be
# uploaded once per scoring run as Vertex cached content and reused across all
# jobs in the run; cached input bills at a tenth of the standard rate (see
# obs/llm_cost.py). With or without a cache the model sees the same
# information; the only semantic change from the pre-split prompt is ordering
# (the job now comes after the rules, since a cache must be a strict prefix).
MATCH_CONTEXT_TEMPLATE = """You are a careful, skeptical career advisor scoring jobs against the candidate's profile.

# Candidate Profile
{profile_json}

# Candidate Geography
Residence: {residence}
Accepted work styles: {remote_policy}

# Recent Decisions
The candidate recently rejected jobs with these patterns:
{rejection_patterns}

The candidate recently approved jobs with these patterns:
{approval_patterns}

# Scoring Rules
1. role_fit: Is the role's title + family in the candidate's `target_titles` /
   `target_role_families`? A role outside all target families should already have been
   filtered upstream, so if you see one here, score role_fit ≤ 20. Within target families,
   penalize title/level mismatch (e.g. "Senior PM" when target is "Director, Product" → 70 max).
2. qualifications_match: What fraction of the JD's `required_skills` / required qualifications
   are evidenced in the candidate's skills or experience tags? Preferred skills count half.
   Judge by the role's own terms — for a PM role that means product/discovery/GTM skills,
   for an eng role that means technical skills. Do not over-weight technical skills for
   non-technical roles.
3. seniority_match: 100 if JD seniority is in `target_seniorities`. Off-by-one within the
   same track (e.g. senior vs staff, or manager vs director) → 60. Wrong track entirely
   (IC role when candidate wants management, or vice versa) → 30 unless target_seniorities
   includes both.
4. comp_alignment: 100 if comp_range.min_total >= min_comp_total. 50 if unknown.
   0 if comp_range.max_total < min_comp_total.
5. deal_breaker_penalty: Start at 100. Subtract 30 per deal-breaker hit. Floor at 0.
6. GEOGRAPHIC ELIGIBILITY (hard gate). Decide whether the candidate can actually
   hold this job from where they live (see "Candidate Geography" above and the
   parsed job_country/job_state/job_city/remote_scope/us_remote_ok fields):
   - Onsite or hybrid roles: the job's location must match the candidate's residence.
     Require the same COUNTRY; also require the same state when the candidate's state
     is known, and the same city/metro when in-person attendance is required and the
     candidate's city is known. A role that needs relocation or presence in another
     country is INELIGIBLE.
   - Remote roles: the role's remote_scope must INCLUDE the candidate's country
     (e.g. residence United States + remote_scope 'United States'/'US-only'/'Worldwide'
     → eligible; residence United States + remote_scope 'Europe'/'EMEA'/'LATAM'
     → INELIGIBLE). If remote_scope is unstated, treat it as ineligible unless
     us_remote_ok is true.
   - EXCEPTION: if us_remote_ok is true and the candidate is US-based, the role is
     ELIGIBLE regardless of where the company or office is located.
   - Also honor the candidate's accepted work styles: a purely onsite role when the
     candidate accepts only remote is ineligible, and vice versa.
   If the role is geographically INELIGIBLE: set deal_breaker_penalty = 0, add an
   explicit red flag like "Location ineligible: <job location> not reachable from
   <residence>", set recommendation = "skip", and CAP overall_score at 20 (override
   the weighted formula — a job the candidate cannot take is not a match no matter
   how strong the role fit).

overall_score = weighted average (UNLESS overridden by the geographic gate above):
  0.30 * role_fit + 0.25 * qualifications_match + 0.20 * seniority_match +
  0.15 * comp_alignment + 0.10 * deal_breaker_penalty

recommendation thresholds:
  >= 85: strong_apply
  70-84: apply
  55-69: maybe
  < 55: skip

Be honest. Skeptical scoring is more useful than charitable scoring.

Score the job that follows against this profile.
"""

MATCH_JOB_TEMPLATE = """# Job
Company: {company}
Title: {title}
Location: {location}

## Parsed JD
{parsed_jd_json}

## Full JD
{jd_text}
"""


def build_match_context(
    profile: MasterProfile,
    rejection_patterns: str = "",
    approval_patterns: str = "",
) -> str:
    """The static (per-user, per-run) block of the scoring prompt."""
    return MATCH_CONTEXT_TEMPLATE.format(
        profile_json=profile.model_dump_json(indent=2),
        residence=_residence_str(profile),
        remote_policy=", ".join(profile.preferences.remote_policy) or "unspecified",
        rejection_patterns=rejection_patterns or "(none yet)",
        approval_patterns=approval_patterns or "(none yet)",
    )


def build_match_job_block(job: Job) -> str:
    """The per-job block of the scoring prompt."""
    return MATCH_JOB_TEMPLATE.format(
        company=job.company,
        title=job.title,
        location=job.location or "unspecified",
        parsed_jd_json=job.jd_parsed.model_dump_json(indent=2)
        if job.jd_parsed
        else "{}",
        jd_text=job.jd_raw[:4000],  # truncate
    )


def match_cache_display_name(user_id: str) -> str:
    """A cache is a per-user singleton — one live one per user, at most.

    That is what makes :func:`reap_match_caches` able to clean up after a run
    that never got to delete its own.
    """
    return f"hermes-match-{user_id}"


# Caches are listed project-wide (the Vertex list API takes no display-name
# filter), so the scan is bounded: with TTLs clamped to an hour there should
# only ever be a handful alive, and a surprise is not worth an unbounded walk.
_CACHE_SCAN_LIMIT = 200


async def reap_match_caches(client, display_name: str) -> int:
    """Delete any live cache still carrying ``display_name``; returns how many.

    Every run deletes its own cache in a ``finally``, but a killed process
    (Cloud Run scale-down, SIGKILL) leaves one standing and *billed* until its
    TTL runs out — with the old 24h TTL, for a day. Since the display name is
    a per-user singleton, anything found here is a previous run's corpse, so
    each run buries the last one's. Best-effort: never let cache hygiene stop
    a scoring run.

    Two runs for the same user overlapping would have the later one bury the
    earlier one's *live* cache; that costs the first run its discount (
    ``match_job`` falls back to the uncached prompt on a cache error) but not
    its results, and the queue's named tasks already dedupe cycles per user.
    """
    deleted = 0
    try:
        scanned = 0
        async for cache in await client.aio.caches.list():
            scanned += 1
            if cache.display_name == display_name and cache.name:
                await client.aio.caches.delete(name=cache.name)
                deleted += 1
            if scanned >= _CACHE_SCAN_LIMIT:
                # Truncated: the user's own leaked cache may be past here, so
                # this reap silently becomes a no-op exactly when there are
                # enough live caches for leaks to matter. Warn so that shows
                # up in the logs instead of as a slow bill.
                log.warning(
                    "matching.cache.reap_truncated",
                    display_name=display_name,
                    scanned=scanned,
                )
                break
        if deleted:
            log.info(
                "matching.cache.reaped", display_name=display_name, deleted=deleted
            )
    except Exception as e:
        log.warning(
            "matching.cache.reap_failed", display_name=display_name, error=str(e)[:200]
        )
    return deleted


async def create_match_cache(
    profile: MasterProfile,
    rejection_patterns: str = "",
    approval_patterns: str = "",
    *,
    ttl_seconds: int = 3600,
) -> str | None:
    """Upload the static scoring block as Vertex cached content.

    Returns the cache resource name to pass as ``match_job(...,
    cached_content=)``, or ``None`` when creation fails — e.g. the block is
    under the model's minimum cacheable size for a thin profile. Callers just
    run uncached in that case; scoring behavior is identical either way.

    Any previous cache for this user is reaped first (see
    :func:`reap_match_caches`) — the display name is a per-user singleton, so
    the only thing that can be standing here is a leak.
    """
    client = vertex_client()
    display_name = match_cache_display_name(profile.user_id)
    await reap_match_caches(client, display_name)
    try:
        cache = await client.aio.caches.create(
            model=PRO_MODEL,
            config=types.CreateCachedContentConfig(
                contents=[
                    build_match_context(profile, rejection_patterns, approval_patterns)
                ],
                ttl=f"{ttl_seconds}s",
                display_name=display_name,
            ),
        )
    except Exception as e:
        log.warning("matching.cache.create_failed", error=str(e)[:300])
        return None
    tokens = cache.usage_metadata.total_token_count if cache.usage_metadata else None
    log.info(
        "matching.cache.created",
        cache=cache.name,
        cached_tokens=tokens,
        ttl_seconds=ttl_seconds,
    )
    return cache.name


async def delete_match_cache(cache_name: str) -> None:
    """Best-effort delete; a cache that outlives this also ages out on TTL."""
    client = vertex_client()
    try:
        await client.aio.caches.delete(name=cache_name)
        log.info("matching.cache.deleted", cache=cache_name)
    except Exception as e:
        log.warning(
            "matching.cache.delete_failed", cache=cache_name, error=str(e)[:200]
        )


def _residence_str(profile: MasterProfile) -> str:
    """Human-readable residence for the prompt, with country-level fallback.

    Prefers the structured `residence` (city, state, country); falls back to the
    freeform `location` string when residence is not set.
    """
    r = profile.residence
    if r is None:
        return profile.location
    parts = [p for p in (r.city, r.state, r.country) if p]
    return ", ".join(parts) if parts else profile.location


# ------------------------------------------------------------- the pre-filter
#
# Everything a scorer can decide *without* buying a Pro call lives in
# :func:`prefilter`, which all three scorers call immediately before deciding
# to spend. Two rejections come out of it, and they must never be confused:
# a role outside the target families, and — only under ``GEO_GATE_ENFORCE`` — a
# job the deterministic geo gate proves the candidate cannot hold.

# Sentinel score for jobs filtered out before full scoring.
OUT_OF_FAMILY = JobMatch(
    job_id="",
    overall_score=0,
    breakdown=ScoreBreakdown(
        role_fit=0,
        qualifications_match=0,
        seniority_match=0,
        comp_alignment=0,
        deal_breaker_penalty=100,
    ),
    matched_strengths=[],
    gaps=[],
    red_flags_hit=[],
    reasoning="Role family outside target_role_families — skipped before scoring.",
    recommendation="skip",
)

#: Sentinel for a job the geo gate rejected *instead of* calling Pro.
#:
#: **The score is 0, and it must never be 20.** 20 is
#: ``score.DISCARD_AT_OR_BELOW`` and, across this codebase, means exactly one
#: thing: *Pro* looked at the job and applied Rule 6's geographic cap. Three
#: separate pieces of machinery read it that way — ``cli.geo_replay``'s
#: ``GEO_CAP_SCORE``, ``score.shadow_geo_gate``'s ``pro_capped``, and every
#: historical tombstone count derived from either. A gate-issued 20 would forge
#: Pro decisions that were never made and silently corrupt the one measurement
#: this whole phase is justified by.
#:
#: 0 is already ``OUT_OF_FAMILY``'s "never reached Pro" sentinel, which is
#: precisely what this is too. The two are told apart by ``geo_gate.enforced``
#: on the tombstone — never by score, and never by ``reasoning``.
GEO_INELIGIBLE = JobMatch(
    job_id="",
    overall_score=0,
    breakdown=ScoreBreakdown(
        role_fit=0,
        qualifications_match=0,
        seniority_match=0,
        comp_alignment=0,
        # 0 rather than OUT_OF_FAMILY's 100, mirroring what Rule 6 instructs Pro
        # to write for a geographically ineligible role. The breakdown is
        # fiction either way — nothing scored this job — but where a value can
        # match what the paid path would have produced, it should.
        deal_breaker_penalty=0,
    ),
    matched_strengths=[],
    gaps=[],
    red_flags_hit=[],
    reasoning=(
        "Geographically ineligible from the candidate's residence — skipped "
        "before scoring by the deterministic geo gate (tools.matching.geo)."
    ),
    recommendation="skip",
)

#: Fraction of gate-rejected jobs scored by Pro anyway, for measurement.
DEFAULT_GEO_HOLDOUT = 0.10


def geo_enforce_enabled() -> bool:
    """True when the geo gate may *skip* Pro calls, not merely record them.

    Off unless explicitly switched on, same shape as ``QUEUE_MODE``
    (``tools.queues.enabled``). Off is the shipped state: the gate's
    false-positive rate is measured (0 over 1,127
    records) but a false positive under enforcement is not one lost job — the
    tombstone is discovery's dedupe key, so it suppresses that posting on every
    future re-discovery too. That is what :func:`score.restore_payload` and
    ``cli.geo_resurrect`` exist to make reversible, and what this flag exists to
    keep switched off until someone decides to turn it on.
    """
    return os.getenv("GEO_GATE_ENFORCE", "").strip().lower() in {"1", "true", "on"}


def geo_holdout_fraction() -> float:
    """``GEO_GATE_HOLDOUT`` as a fraction in [0, 1]; default 10%.

    Anything unparseable or out of range falls back to the default rather than
    disabling the hold-out, because a hold-out of zero is the one setting that
    quietly destroys the measurement.
    """
    raw = os.getenv("GEO_GATE_HOLDOUT", "").strip()
    if not raw:
        return DEFAULT_GEO_HOLDOUT
    try:
        value = float(raw)
    except ValueError:
        log.warning("matching.geo_holdout_env_invalid", value=raw[:40])
        return DEFAULT_GEO_HOLDOUT
    if not 0.0 <= value <= 1.0:
        log.warning("matching.geo_holdout_env_invalid", value=raw[:40])
        return DEFAULT_GEO_HOLDOUT
    return value


def geo_holdout(job_id: str, fraction: float) -> bool:
    """Is this job in the hold-out — scored by Pro despite an ineligible verdict?

    **Deterministic on the job id, never ``random()``.** The resumable batch
    pipeline decides at submit time and joins the responses back at ingest,
    hours later and in a different process (``batch_runs.resume``, typically a
    worker cron tick). A job that answered "skip" at submit and "score" at
    ingest — or the reverse — would break the content join: the ingest would
    either look for a Pro response that was never requested, or tombstone a job
    whose paid response is sitting in the output it is holding. Hashing the id
    makes the answer a property of the job rather than of the process asking.

    SHA-256 rather than :func:`hash`, because Python salts ``hash(str)`` per
    process by default — the exact failure this is written to avoid.

    Sampling the id and not the *decision* also means the hold-out set is stable
    across a ``GATE_VERSION`` bump, so the same jobs keep producing the Pro
    comparison and the series stays readable.
    """
    if fraction <= 0.0:
        return False
    if fraction >= 1.0:
        return True
    digest = hashlib.sha256(job_id.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64 < fraction


def prefilter(
    job: Job, profile: MasterProfile, *, enforce: bool
) -> tuple[JobMatch | None, geo.GeoDecision | None]:
    """What can be decided about this job for free — the one pre-Pro seam.

    Returns ``(match, decision)``:

    - ``(OUT_OF_FAMILY copy, None)`` — role family outside the profile's
      targets. No gate was consulted, hence no decision.
    - ``(GEO_INELIGIBLE copy, decision)`` — ``enforce`` is on and the gate
      proved the job unreachable. **The non-``None`` decision is the
      discriminator**: a caller tells an enforced geo skip from a family miss by
      whether a decision came back with the sentinel, never by comparing scores
      (both are 0, deliberately — see :data:`GEO_INELIGIBLE`).
    - ``(None, ...)`` — go and score it.

    This decision used to be written out three times (in :func:`match_job`, in
    ``batch._batch_score``, in ``batch_runs._submit_score_stage``) because the
    batch paths never call :func:`match_job` — they build their own Pro requests
    from a list of jobs. That is also why Phase 1C's geo shadow recording had to
    be hung off ``score.persist_result`` rather than off the pre-filter itself:
    there was no single place the pre-filter *was*.

    The families are lowercased on the profile side only. ``role_family`` is a
    ``Literal`` the parse prompt already constrains to lowercase, whereas
    ``target_role_families`` is user-supplied and arrives however it was typed.

    A job with no parse gets ``(None, None)``, and that ``None`` deliberately
    does not have to be told apart from "go score it" at a call site: every
    caller settles the unparsed case *before* asking. Do not "fix" this by
    parsing here — that would put a billed Flash call inside the function whose
    job is to avoid billed calls.

    **When ``enforce`` is false the gate is not consulted at all**, so this
    reduces to the family test that shipped before Phase 1D, plus one boolean.
    That is the merge-safety argument, and it is why the gate call sits behind
    the flag rather than being evaluated and discarded: with the flag off there
    is no new code path for anything — not even an exception — to come out of.
    The shadow recording is unaffected; it has always had its own
    ``geo.evaluate`` call inside ``score.persist_result``.

    Returns a ``model_copy``, never a module-level singleton: callers stamp
    ``job_id`` onto what they get back.
    """
    parsed = job.jd_parsed
    if parsed is None:
        return None, None
    targets = {f.lower() for f in profile.preferences.target_role_families}
    if parsed.role_family not in targets:
        match = OUT_OF_FAMILY.model_copy()
        match.job_id = job.id
        return match, None
    if not enforce:
        return None, None

    try:
        decision = geo.evaluate(parsed, profile)
    except Exception as e:
        # The gate is pure and has no business raising, but a profile it cannot
        # read (an old doc, a stub) must cost a skipped optimization and never a
        # skipped job. Abstaining here is exactly the status quo.
        log.warning("matching.geo_gate_failed", job_id=job.id, error=str(e)[:200])
        return None, None
    if decision.verdict != "ineligible":
        return None, decision

    # **US residents only, and this check belongs here rather than in geo.py.**
    # ``geo.evaluate`` reads exactly one profile field (``residence.country``),
    # and both profiles the gate was measured against normalize to "US" — so the
    # effective sample is one profile, not two. For a non-US resident the
    # structure inverts rather than merely shifting: ``us_remote_ok``, the
    # safety valve carrying 73.4% of the kept corpus, is hard-gated on US inside
    # the gate and so never fires, while ``country_mismatch`` fires against
    # nearly every US posting. That population is unmeasured *and* structurally
    # different, and there are zero non-US users today, so declining to enforce
    # for them costs nothing. geo.py stays a pure statement of what is provable;
    # who we are willing to act on it for is a policy, and policy lives here.
    if decision.residence_country != "US":
        return None, decision

    if geo_holdout(job.id, geo_holdout_fraction()):
        # Scored by Pro anyway, and recorded through the normal shadow path.
        # Permanent, not a rollout ramp: once enforcing, the enforced population
        # stops producing Pro comparisons forever, which would leave the only
        # metric that justifies the gate with a hole exactly where the gate acts.
        log.info(
            "matching.geo_holdout",
            job_id=job.id,
            rule=decision.rule,
            job_country=decision.job_country,
        )
        return None, decision

    log.info(
        "matching.geo_skipped",
        job_id=job.id,
        rule=decision.rule,
        job_country=decision.job_country,
        residence_country=decision.residence_country,
    )
    match = GEO_INELIGIBLE.model_copy()
    match.job_id = job.id
    return match, decision


async def parse_jd(job: Job) -> ParsedJD:
    """Cheap structured extraction with Flash — runs on every discovered job."""
    client = vertex_client()
    try:
        response = await client.aio.models.generate_content(
            model=FLASH_MODEL,
            contents=[job.jd_raw],
            config=types.GenerateContentConfig(
                system_instruction=PARSE_JD_PROMPT,
                response_mime_type="application/json",
                response_schema=ParsedJD,
                temperature=0.1,
                thinking_config=_PARSE_JD_THINKING,
            ),
        )
        record_llm_call(step="matching.parse_jd", response=response, job_id=job.id)
        return ParsedJD.model_validate_json(response.text)
    except Exception:
        log.exception("matching.parse_jd.failed", job_id=job.id, company=job.company)
        raise


async def match_job(
    job: Job,
    profile: MasterProfile,
    rejection_patterns: str = "",
    approval_patterns: str = "",
    cached_content: str | None = None,
) -> JobMatch:
    """Parse (if needed), then full Pro scoring. **This call always spends.**

    It does *not* pre-filter. :func:`prefilter` used to run here, which made the
    online path the only one where the free rejections happened inside the paid
    function — and left the caller unable to see *why* a job was rejected, which
    the geo gate needs (it has to record a verdict onto the tombstone). So the
    pre-filter moved out to the callers, where the two batch scorers had always
    had it, and all three now call it immediately before deciding to spend.

    ``cached_content`` is a Vertex cache resource name from
    :func:`create_match_cache`; when set, only the per-job block is sent and
    the static block is read from the cache at the discounted rate. The cache
    must have been built from the same profile/patterns, or the model will
    score against stale context.
    """
    started = time.monotonic()
    job_log = log.bind(job_id=job.id, company=job.company)
    if job.jd_parsed is None:
        job.jd_parsed = await parse_jd(job)

    job_block = build_match_job_block(job)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=JobMatch,
        temperature=0.2,
        thinking_config=_MATCH_THINKING,
        max_output_tokens=_MATCH_MAX_OUTPUT_TOKENS,
    )

    def _uncached_args() -> tuple[list[str], types.GenerateContentConfig]:
        context = build_match_context(profile, rejection_patterns, approval_patterns)
        return [f"{context}\n\n{job_block}"], config

    # Full scoring uses Pro — this is the call worth paying for.
    client = vertex_client()
    try:
        if cached_content:
            try:
                response = await client.aio.models.generate_content(
                    model=PRO_MODEL,
                    contents=[job_block],
                    config=config.model_copy(update={"cached_content": cached_content}),
                )
            except Exception as e:
                # A cache can expire/evict mid-run (long backlog > TTL). Only
                # cache-shaped errors fall back to the uncached prompt —
                # anything else (429s, invalid schema, ...) would fail again
                # uncached, so re-raise rather than double-spend on it.
                if "cach" not in str(e).lower():
                    raise
                job_log.warning("matching.score.cache_fallback", error=str(e)[:200])
                contents, cfg = _uncached_args()
                response = await client.aio.models.generate_content(
                    model=PRO_MODEL, contents=contents, config=cfg
                )
        else:
            contents, cfg = _uncached_args()
            response = await client.aio.models.generate_content(
                model=PRO_MODEL, contents=contents, config=cfg
            )
        record_llm_call(step="matching.score", response=response, job_id=job.id)
        match = JobMatch.model_validate_json(response.text)
    except Exception:
        job_log.exception("matching.score.failed")
        raise
    match.job_id = job.id  # ensure consistency
    job_log.info(
        "matching.scored",
        score=match.overall_score,
        recommendation=match.recommendation,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return match
