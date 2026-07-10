# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Coordinator: the root LlmAgent that delegates to the five specialists.

This is the main app served by the FastAPI gateway. It assembles fresh
instances of each specialist via their factory functions so the sub-agents
attach to this coordinator without parent conflicts (each agent package also
exposes a standalone ``root_agent`` for isolated testing in the playground).
"""

from _shared import flash
from application.agent import create_application_agent
from discovery.agent import create_discovery_agent
from google.adk.agents import Agent
from google.adk.apps import App
from matching.agent import create_matching_agent
from tailoring.agent import create_tailoring_agent
from tracking.agent import create_tracking_agent


def create_coordinator() -> Agent:
    """Builds the Coordinator with all five specialist sub-agents."""
    return Agent(
        name="Coordinator",
        model=flash(),
        description=("Coordinates the end-to-end job discovery and application flow."),
        instruction=(
            "You are the coordinator for hermes, a job discovery and "
            "application assistant. Orchestrate the workflow by delegating to "
            "your specialists:\n"
            "- Discovery: find job openings across sources.\n"
            "- Matching: rank openings against the candidate's profile.\n"
            "- Tailoring: tailor resume and cover letter for a chosen posting.\n"
            "- Application: submit applications via the browser.\n"
            "- Tracking: record and report application status.\n\n"
            "Drive the flow in that order, but adapt to the user's request. "
            "Always confirm with the user before the Application agent submits "
            "anything."
        ),
        sub_agents=[
            create_discovery_agent(),
            create_matching_agent(),
            create_tailoring_agent(),
            create_application_agent(),
            create_tracking_agent(),
        ],
    )


root_agent = create_coordinator()

# App name must match this directory name ("coordinator") for eval/session
# resolution by the ADK agent loader.
app = App(name="coordinator", root_agent=root_agent)
