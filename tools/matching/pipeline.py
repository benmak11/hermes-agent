# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Matching pipeline: parse a JD (Flash) then score it against the profile (Pro).

Deterministic engine (run via cli/run_matching.py), not an ADK agent. A cheap
family pre-filter drops out-of-target roles before the expensive Pro scoring
call. Models are kept in sync with agents/_shared.py.
"""

from __future__ import annotations

import time

from google import genai
from google.genai import types

from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile
from obs.llm_cost import record_llm_call
from obs.logging import get_logger

log = get_logger("tools.matching")

# Keep in sync with agents/_shared.py. Parsing is high-volume → Flash; scoring
# is the call worth paying for → Pro (gemini-3.1-pro-preview, since plain
# "gemini-3-pro" is not in the Vertex catalog for this project).
FLASH_MODEL = "gemini-flash-latest"
PRO_MODEL = "gemini-3.1-pro-preview"

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
        parsed_jd_json=job.jd_parsed.model_dump_json(indent=2) if job.jd_parsed else "{}",
        jd_text=job.jd_raw[:4000],  # truncate
    )


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
    """
    client = genai.Client(vertexai=True)
    try:
        cache = await client.aio.caches.create(
            model=PRO_MODEL,
            config=types.CreateCachedContentConfig(
                contents=[
                    build_match_context(profile, rejection_patterns, approval_patterns)
                ],
                ttl=f"{ttl_seconds}s",
                display_name=f"hermes-match-{profile.user_id}",
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
    client = genai.Client(vertexai=True)
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


async def parse_jd(job: Job) -> ParsedJD:
    """Cheap structured extraction with Flash — runs on every discovered job."""
    client = genai.Client(vertexai=True)
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
    """Parse (if needed), family pre-filter, then full Pro scoring.

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

    # Cheap pre-filter: skip jobs outside target families before the Pro call.
    targets = {f.lower() for f in profile.preferences.target_role_families}
    if job.jd_parsed.role_family not in targets:
        job_log.info(
            "matching.skip_out_of_family", role_family=job.jd_parsed.role_family
        )
        m = OUT_OF_FAMILY.model_copy()
        m.job_id = job.id
        return m

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
    client = genai.Client(vertexai=True)
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
                job_log.warning(
                    "matching.score.cache_fallback", error=str(e)[:200]
                )
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
