# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Matching pipeline: parse a JD (Flash) then score it against the profile (Pro).

Deterministic engine (run via cli/run_matching.py), not an ADK agent. A cheap
family pre-filter drops out-of-target roles before the expensive Pro scoring
call. Models are kept in sync with agents/_shared.py.
"""

from __future__ import annotations

from google import genai
from google.genai import types

from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile

# Keep in sync with agents/_shared.py. Parsing is high-volume → Flash; scoring
# is the call worth paying for → Pro (gemini-3.1-pro-preview, since plain
# "gemini-3-pro" is not in the Vertex catalog for this project).
FLASH_MODEL = "gemini-flash-latest"
PRO_MODEL = "gemini-3.1-pro-preview"

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
"""

MATCH_PROMPT_TEMPLATE = """You are a careful, skeptical career advisor scoring this job against the candidate's profile.

# Candidate Profile
{profile_json}

# Job
Company: {company}
Title: {title}
Location: {location}

## Parsed JD
{parsed_jd_json}

## Full JD
{jd_text}

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

overall_score = weighted average:
  0.30 * role_fit + 0.25 * qualifications_match + 0.20 * seniority_match +
  0.15 * comp_alignment + 0.10 * deal_breaker_penalty

recommendation thresholds:
  >= 85: strong_apply
  70-84: apply
  55-69: maybe
  < 55: skip

Be honest. Skeptical scoring is more useful than charitable scoring.
"""

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
    response = await client.aio.models.generate_content(
        model=FLASH_MODEL,
        contents=[job.jd_raw],
        config=types.GenerateContentConfig(
            system_instruction=PARSE_JD_PROMPT,
            response_mime_type="application/json",
            response_schema=ParsedJD,
            temperature=0.1,
        ),
    )
    return ParsedJD.model_validate_json(response.text)


async def match_job(
    job: Job,
    profile: MasterProfile,
    rejection_patterns: str = "",
    approval_patterns: str = "",
) -> JobMatch:
    """Parse (if needed), family pre-filter, then full Pro scoring."""
    if job.jd_parsed is None:
        job.jd_parsed = await parse_jd(job)

    # Cheap pre-filter: skip jobs outside target families before the Pro call.
    targets = {f.lower() for f in profile.preferences.target_role_families}
    if job.jd_parsed.role_family not in targets:
        m = OUT_OF_FAMILY.model_copy()
        m.job_id = job.id
        return m

    prompt = MATCH_PROMPT_TEMPLATE.format(
        profile_json=profile.model_dump_json(indent=2),
        company=job.company,
        title=job.title,
        location=job.location or "unspecified",
        parsed_jd_json=job.jd_parsed.model_dump_json(indent=2),
        jd_text=job.jd_raw[:4000],  # truncate
        rejection_patterns=rejection_patterns or "(none yet)",
        approval_patterns=approval_patterns or "(none yet)",
    )

    # Full scoring uses Pro — this is the call worth paying for.
    client = genai.Client(vertexai=True)
    response = await client.aio.models.generate_content(
        model=PRO_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=JobMatch,
            temperature=0.2,
        ),
    )
    match = JobMatch.model_validate_json(response.text)
    match.job_id = job.id  # ensure consistency
    return match
