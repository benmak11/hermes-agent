# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Discovery: a ParallelAgent that scouts job sources concurrently.

Each scout uses google_search grounding and writes to a distinct output_key so
the parallel branches do not race on shared state.
"""

from _shared import flash
from google.adk.agents import Agent, ParallelAgent
from google.adk.tools import google_search


def create_discovery_agent() -> ParallelAgent:
    """Builds the Discovery ParallelAgent with its source scouts."""
    job_board_scout = Agent(
        name="job_board_scout",
        model=flash(),
        description="Searches public job boards for relevant openings.",
        instruction=(
            "Search public job boards (LinkedIn, Indeed, etc.) for openings "
            "that match the user's target role, skills, and location. Return a "
            "concise list of postings with title, company, location, and link."
        ),
        tools=[google_search],
        output_key="job_board_results",
    )

    company_careers_scout = Agent(
        name="company_careers_scout",
        model=flash(),
        description="Searches company career pages for relevant openings.",
        instruction=(
            "Search target companies' career pages for openings matching the "
            "user's target role, skills, and location. Return a concise list "
            "of postings with title, company, location, and link."
        ),
        tools=[google_search],
        output_key="company_careers_results",
    )

    return ParallelAgent(
        name="Discovery",
        description=(
            "Concurrently discovers job openings across job boards and company "
            "career pages."
        ),
        sub_agents=[job_board_scout, company_careers_scout],
    )


root_agent = create_discovery_agent()
