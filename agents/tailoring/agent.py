# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Tailoring: an LlmAgent (Gemini Flash) that tailors application materials."""

from _shared import flash
from google.adk.agents import Agent


def create_tailoring_agent() -> Agent:
    """Builds the Tailoring agent."""
    return Agent(
        name="Tailoring",
        model=flash(),
        description=(
            "Tailors the candidate's resume and cover letter to a specific job posting."
        ),
        instruction=(
            "Given the shortlisted matches ({matched_jobs?}) and the "
            "candidate's base materials, produce a tailored resume summary and "
            "cover letter for the selected posting. Emphasize the most relevant "
            "experience and mirror the posting's keywords."
        ),
        output_key="tailored_documents",
    )


root_agent = create_tailoring_agent()
