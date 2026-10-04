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

# Thinking bills as output tokens at the full output rate, and ran 1.5x-4x the
# answer size on both calls below when left unconfigured. Both tasks need real
# judgment, so these trim the default rather than disabling thinking.
#
# The two knobs differ because the model generations do: 2.5 Flash 400s on
# thinking_level and takes the older thinking_budget instead (it failed every
# parse_jd call in a live backlog run), while Pro takes thinking_level. Pick the
# knob the pinned model generation accepts — and keep the id pinned, because an
# alias repointed Google-side would 400 this config with no commit here. 512
# tokens caps thinking near the thinking_level=LOW intent.
_PARSE_JD_THINKING = types.ThinkingConfig(thinking_budget=512)
_MATCH_THINKING = types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM)

# Ceiling on what one Pro scoring call may generate. Thinking counts toward
# max_output_tokens, so this must cover answer + thinking; the all-time
# telemetry worst is ~3.0K combined. 4096 leaves headroom while capping a
# runaway generation at ~$0.05 instead of ~$0.79 at the model's default.
# Hitting the cap truncates the JSON, which fails schema validation and
# surfaces as match.failed rather than a silent wrong score.
_MATCH_MAX_OUTPUT_TOKENS = 4096

#: Version of :data:`PARSE_JD_PROMPT`, stamped onto every parse this repo pays
#: for (``score.scored_with``). Bump it in the same commit as any change to
#: that prompt: a stale version asserts that two parses are comparable when
#: they are not. Integer, monotonic, no other semantics.
#:
#: Enforced, not left to memory: ``tests/unit/test_scored_with.py`` pins a
#: digest of the prompt text per version, so editing the prompt without
#: bumping this fails the suite, and so does bumping it without recording the
#: new digest.
PARSE_PROMPT_VERSION = 1

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

#: Version of the whole scoring prompt: :data:`MATCH_CONTEXT_TEMPLATE` plus
#: :data:`MATCH_JOB_TEMPLATE`. One number covers both, because the model sees
#: one concatenated prompt. Bump it whenever either template changes,
#: including the scoring rules text inside the context block — the edit most
#: likely to move every score while looking cosmetic.
#:
#: It does *not* cover the model id (recorded separately as ``match_model``) or
#: any per-user data interpolated into the templates (profile, rejection and
#: approval patterns).
#:
#: Digest-pinned per version in ``tests/unit/test_scored_with.py`` over both
#: templates concatenated — see :data:`PARSE_PROMPT_VERSION`.
MATCH_PROMPT_VERSION = 1


# The scoring prompt is split into a per-user static block and a per-job block
# so the static block — which dominates input tokens — can be uploaded once per
# run as Vertex cached content and billed at a tenth of the standard rate. The
# model sees the same information either way; the job must come after the rules
# because a cache has to be a strict prefix.
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
    """The cache display name for a user. One live cache per user, at most —
    which is what lets :func:`reap_match_caches` clean up after a run that
    never deleted its own.
    """
    return f"hermes-match-{user_id}"


# Caches are listed project-wide (the Vertex list API takes no display-name
# filter), so the scan is bounded: with TTLs clamped to an hour only a handful
# should ever be alive, and a surprise is not worth an unbounded walk.
_CACHE_SCAN_LIMIT = 200


async def reap_match_caches(client, display_name: str) -> int:
    """Delete any live cache carrying ``display_name``; returns how many.

    A killed process leaves its cache standing and billed until the TTL runs
    out, and the display name is a per-user singleton, so anything found here
    is a previous run's leak. Swallows all errors — cache hygiene must never
    stop a scoring run.

    If two runs for the same user overlap, the later buries the earlier one's
    live cache; that costs the first run its discount but not its results.
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
                # Truncated: the user's leaked cache may lie past here, so the
                # reap silently no-ops exactly when leaks start to matter. Warn
                # so it shows up in the logs rather than as a slow bill.
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

    Returns the cache resource name for ``match_job(..., cached_content=)``, or
    ``None`` when creation fails (e.g. a thin profile falls under the model's
    minimum cacheable size); callers then run uncached, with identical scoring
    behavior. Reaps any previous cache for this user first.
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
    """Human-readable residence for the prompt.

    Prefers the structured `residence` (city, state, country) and falls back to
    the freeform `location` string.
    """
    r = profile.residence
    if r is None:
        return profile.location
    parts = [p for p in (r.city, r.state, r.country) if p]
    return ", ".join(parts) if parts else profile.location


# ------------------------------------------------------------- the pre-filter
#
# Everything a scorer can decide without buying a Pro call lives in
# :func:`prefilter`, which all three scorers call immediately before deciding
# to spend. Its two rejections must never be confused: a role outside the
# target families, and — only under ``GEO_GATE_ENFORCE`` — a job the
# deterministic geo gate proves the candidate cannot hold.

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
#: The score is 0 and must never be 20: 20 is ``score.DISCARD_AT_OR_BELOW`` and
#: means, everywhere in this codebase, that Pro itself applied Rule 6's
#: geographic cap (``cli.geo_replay``, ``score.shadow_geo_gate`` and every
#: tombstone count derived from them read it that way). A gate-issued 20 would
#: forge Pro decisions that were never made.
#:
#: 0 is also ``OUT_OF_FAMILY``'s "never reached Pro" sentinel. The two are told
#: apart by ``geo_gate.enforced`` on the tombstone, never by score or by
#: ``reasoning``.
GEO_INELIGIBLE = JobMatch(
    job_id="",
    overall_score=0,
    breakdown=ScoreBreakdown(
        role_fit=0,
        qualifications_match=0,
        seniority_match=0,
        comp_alignment=0,
        # 0 rather than OUT_OF_FAMILY's 100, mirroring what Rule 6 instructs
        # Pro to write for a geographically ineligible role. The breakdown is
        # fiction either way, but should match what the paid path would write.
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

    Off unless explicitly switched on, and off is the shipped state. The gate
    measured 0 false positives over 1,127 records, but a false positive under
    enforcement loses the posting permanently rather than once: the tombstone
    is discovery's dedupe key, so it suppresses that posting on every future
    re-discovery (``score.restore_payload`` and ``cli.geo_resurrect`` are the
    undo).
    """
    return os.getenv("GEO_GATE_ENFORCE", "").strip().lower() in {"1", "true", "on"}


def geo_holdout_fraction() -> float:
    """``GEO_GATE_HOLDOUT`` as a fraction in [0, 1]; default 10%.

    Anything unparseable or out of range falls back to the default rather than
    to zero, because a hold-out of zero quietly destroys the measurement.
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

    Deterministic on the job id, never ``random()``: the batch pipeline decides
    at submit time and joins responses back at ingest, hours later in a
    different process, so an answer that changed in between would break the
    content join. SHA-256 rather than :func:`hash`, because Python salts
    ``hash(str)`` per process.

    Sampling the id rather than the decision also keeps the hold-out set stable
    across a ``GATE_VERSION`` bump, so the series stays readable.
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
      targets; the gate was not consulted.
    - ``(GEO_INELIGIBLE copy, decision)`` — ``enforce`` is on and the gate
      proved the job unreachable.
    - ``(None, ...)`` — go and score it.

    The non-``None`` decision is the discriminator between an enforced geo skip
    and a family miss. Never compare scores: both sentinels are 0, deliberately
    (see :data:`GEO_INELIGIBLE`). The returned match is a ``model_copy``, since
    callers stamp ``job_id`` onto it.

    Families are lowercased on the profile side only: ``role_family`` is a
    ``Literal`` the parse prompt already constrains to lowercase, while
    ``target_role_families`` is user-supplied.

    A job with no parse gets ``(None, None)``; every caller settles the
    unparsed case before asking. Do not "fix" that by parsing here — it would
    put a billed Flash call inside the function whose job is to avoid them.

    When ``enforce`` is false the gate is not consulted at all, so no new code
    path, not even an exception, can come out of it. Shadow recording is
    unaffected; it has its own ``geo.evaluate`` call in
    ``score.persist_result``.
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
        # The gate is pure and has no business raising, but a profile it
        # cannot read must cost a skipped optimization, never a skipped job.
        log.warning("matching.geo_gate_failed", job_id=job.id, error=str(e)[:200])
        return None, None
    if decision.verdict != "ineligible":
        return None, decision

    # US residents only. The gate was only ever measured against US-normalizing
    # profiles, and for a non-US resident the structure inverts: ``us_remote_ok``
    # is hard-gated on US and never fires, while ``country_mismatch`` fires
    # against nearly every US posting. That population is unmeasured and
    # structurally different. The check lives here rather than in geo.py, which
    # states what is provable; who we act on it for is policy.
    if decision.residence_country != "US":
        return None, decision

    if geo_holdout(job.id, geo_holdout_fraction()):
        # Scored by Pro anyway and recorded through the normal shadow path.
        # Permanent, not a rollout ramp: without it the enforced population
        # stops producing Pro comparisons, leaving a hole in the only metric
        # that justifies the gate, exactly where the gate acts.
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

    It does *not* pre-filter: callers must run :func:`prefilter` themselves
    immediately before deciding to spend, because they are the ones that need
    the gate's verdict to record onto a tombstone.

    ``cached_content`` is a Vertex cache resource name from
    :func:`create_match_cache`; when set, only the per-job block is sent and the
    static block is read from the cache at the discounted rate. It must have
    been built from the same profile and patterns, or the model scores against
    stale context.
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
