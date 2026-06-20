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
            "Submits job applications by operating a web browser via Computer "
            "Use."
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
