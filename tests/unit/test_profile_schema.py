# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
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
