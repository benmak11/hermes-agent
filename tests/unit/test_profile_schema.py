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
"""Schema smoke test for MasterProfile (no LLM / GCP involved)."""

from datetime import date

from models.profile import (
    Bullet,
    Education,
    Experience,
    JobPreferences,
    MasterProfile,
)


def _minimal_profile() -> MasterProfile:
    return MasterProfile(
        user_id="me",
        full_name="Test User",
        email="test@example.com",
        location="Remote",
        objective_template="{seniority} professional seeking a {role} role at {company}.",
        experience=[
            Experience(
                company="Acme",
                role="Software Engineer",
                start=date(2020, 1, 1),
                bullets=[Bullet(text="Built a thing", tags=["python", "backend"])],
            )
        ],
        education=[
            Education(
                institution="State University",
                degree="BS",
                field="Computer Science",
                start_year=2012,
                end_year=2016,
            )
        ],
        skills={"technical": ["python", "typescript"]},
        preferences=JobPreferences(
            target_role_families=["engineering"],
            target_titles=["Staff Software Engineer"],
            target_seniorities=["staff"],
        ),
    )


def test_master_profile_roundtrip() -> None:
    profile = _minimal_profile()
    restored = MasterProfile.model_validate(profile.model_dump(mode="json"))

    assert restored.user_id == "me"
    assert restored.experience[0].bullets[0].tags == ["python", "backend"]
    # current role has no end date
    assert restored.experience[0].end is None


def test_job_preferences_defaults() -> None:
    profile = _minimal_profile()
    # Defaulted fields should be populated without being supplied.
    assert profile.preferences.remote_policy == ["remote"]
    assert profile.preferences.must_haves == []
    assert profile.preferences.min_comp_total is None
    assert profile.links == {}
