# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Application: an LlmAgent that submits applications via Computer Use."""

from _shared import computer_use_model
from google.adk.agents import Agent
from google.adk.tools.computer_use.computer_use_toolset import ComputerUseToolset

from .computer import HermesBrowserComputer


def create_application_agent() -> Agent:
    """Builds the Application agent with the Computer Use toolset."""
    return Agent(
        name="Application",
        model=computer_use_model(),
        description=(
            "Submits job applications by operating a web browser via Computer Use."
        ),
        instruction=(
            "You submit job applications on behalf of the candidate by "
            "operating a web browser. Use the tailored materials "
            "({tailored_documents?}) to fill out application forms accurately. "
            "Navigate carefully, verify each field before submitting, and "
            "report the outcome of each submission."
        ),
        tools=[ComputerUseToolset(computer=HermesBrowserComputer())],
    )


root_agent = create_application_agent()
