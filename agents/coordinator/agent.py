# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
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
        description=(
            "Coordinates the end-to-end job discovery and application flow."
        ),
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
