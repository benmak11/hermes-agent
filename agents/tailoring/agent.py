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
"""Tailoring: an LlmAgent (Gemini Flash) that tailors application materials."""

from _shared import flash
from google.adk.agents import Agent


def create_tailoring_agent() -> Agent:
    """Builds the Tailoring agent."""
    return Agent(
        name="Tailoring",
        model=flash(),
        description=(
            "Tailors the candidate's resume and cover letter to a specific job "
            "posting."
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
