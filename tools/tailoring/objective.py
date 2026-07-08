# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""LLM objective rewriter: fills the candidate's objective template for one role."""

from __future__ import annotations

from google import genai
from google.genai import types

from models.job import Job
from models.profile import MasterProfile
from obs.llm_cost import record_llm_call

# Objective writing is low-volume and benefits from a little warmth/variation, so
# Flash at a higher temperature is the right cost/quality point.
OBJECTIVE_MODEL = "gemini-flash-latest"

OBJECTIVE_PROMPT = """Fill in this objective template for a specific role.

Template: {template}
Target Role: {title}
Target Company: {company}
JD Summary: {jd_summary}

Rules:
- Replace {{role}} and {{company}} placeholders naturally.
- Keep it to 2 sentences max, ~40 words total.
- Reference one specific thing from the JD that aligns with the candidate's strengths.
- Match the candidate's voice — don't add generic phrases like "passionate about" or "results-driven".
- No quotes around the output. Plain prose only.
"""


def _select_template(profile: MasterProfile, job: Job) -> str:
    """Pick the objective template, supporting an optional per-family dict.

    `objective_template` is normally a plain string. If a deployment widens it to
    a dict keyed by role family, select by the parsed family with a "default"
    fallback.
    """
    template = profile.objective_template
    if isinstance(template, dict):
        family = job.jd_parsed.role_family if job.jd_parsed else "default"
        return (
            template.get(family)
            or template.get("default")
            or next(iter(template.values()))
        )
    return template


async def generate_objective(profile: MasterProfile, job: Job) -> str:
    """Generate a tailored, ~2-sentence objective for this job."""
    template = _select_template(profile, job)
    jd_summary = (
        job.jd_parsed.summary if job.jd_parsed else job.jd_raw[:1000]
    )

    client = genai.Client(vertexai=True)
    response = await client.aio.models.generate_content(
        model=OBJECTIVE_MODEL,
        contents=[
            OBJECTIVE_PROMPT.format(
                template=template,
                title=job.title,
                company=job.company,
                jd_summary=jd_summary,
            )
        ],
        config=types.GenerateContentConfig(temperature=0.6),
    )
    record_llm_call(step="tailoring.objective", response=response, job_id=job.id)
    return response.text.strip()
