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
"""Tracking: an LlmAgent (Gemini Flash) that records application status."""

from _shared import flash
from google.adk.agents import Agent
from google.adk.tools import ToolContext


def record_application(
    company: str,
    role: str,
    status: str,
    tool_context: ToolContext,
) -> dict:
    """Records a job application in session state for tracking.

    Args:
        company: The company the application was submitted to.
        role: The role/title applied for.
        status: Current status (e.g. 'submitted', 'interviewing', 'rejected').

    Returns:
        A dict with 'status' and the updated list of tracked applications.
    """
    applications = tool_context.state.get("applications", [])
    applications.append({"company": company, "role": role, "status": status})
    tool_context.state["applications"] = applications
    return {"status": "success", "applications": applications}


def get_applications(tool_context: ToolContext) -> dict:
    """Returns all applications tracked so far in this session.

    Returns:
        A dict with 'status' and the list of tracked applications.
    """
    return {
        "status": "success",
        "applications": tool_context.state.get("applications", []),
    }


def create_tracking_agent() -> Agent:
    """Builds the Tracking agent."""
    return Agent(
        name="Tracking",
        model=flash(),
        description=(
            "Records submitted applications and reports their current status."
        ),
        instruction=(
            "You track the candidate's applications. Use record_application to "
            "log newly submitted applications and get_applications to report "
            "status. Keep the user informed about where each application stands."
        ),
        tools=[record_application, get_applications],
    )


root_agent = create_tracking_agent()
