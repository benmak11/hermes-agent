# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Matching: an LlmAgent (Gemini 3 Pro) that ranks postings against the profile."""

from _shared import pro
from google.adk.agents import Agent


def score_job_match(job_description: str, candidate_profile: str) -> dict:
    """Scores how well a candidate profile matches a job description.

    Args:
        job_description: The full text of the job posting.
        candidate_profile: A summary of the candidate's skills and experience.

    Returns:
        A dict with 'status', a 'score' between 0 and 100, and 'rationale'.
    """
    # Placeholder heuristic — replace with a real scoring model or embedding
    # similarity. Kept deterministic so the agent can reason over the result.
    jd_terms = {t.lower() for t in job_description.split()}
    profile_terms = {t.lower() for t in candidate_profile.split()}
    overlap = jd_terms & profile_terms
    score = min(100, int(len(overlap) / max(len(jd_terms), 1) * 100))
    return {
        "status": "success",
        "score": score,
        "rationale": f"{len(overlap)} overlapping terms between profile and posting.",
    }


def create_matching_agent() -> Agent:
    """Builds the Matching agent."""
    return Agent(
        name="Matching",
        model=pro(),
        description=(
            "Ranks discovered job postings against the candidate's profile and "
            "selects the strongest matches."
        ),
        instruction=(
            "You rank job postings for the candidate. Consider these discovered "
            "postings:\n"
            "Job boards: {job_board_results?}\n"
            "Company careers: {company_careers_results?}\n\n"
            "Use the score_job_match tool to evaluate fit, then return a ranked "
            "shortlist of the best opportunities with a short rationale for each."
        ),
        tools=[score_job_match],
        output_key="matched_jobs",
    )


root_agent = create_matching_agent()
