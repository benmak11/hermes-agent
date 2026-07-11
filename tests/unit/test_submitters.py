# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for submitter helpers (URL derivation, profile-driven answers).

The Playwright fill paths themselves are validated with live dry-runs, not CI.
"""

from datetime import UTC, date, datetime

from models.job import Job
from models.profile import Experience, JobPreferences, MasterProfile, Residence
from tools.submitters.ashby import application_url
from tools.submitters.common import profile_answers
from tools.submitters.lever import apply_url


def _job(url: str, source: str = "lever") -> Job:
    return Job(
        id="t",
        user_id="me",
        source=source,
        source_id="1",
        company="Acme",
        title="Engineer",
        url=url,
        jd_raw="jd",
        discovered_at=datetime.now(UTC),
    )


def _profile(**overrides) -> MasterProfile:
    base: dict = {
        "user_id": "me",
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "location": "United States",
        "objective_template": "t",
        "experience": [],
        "education": [],
        "skills": {},
        "preferences": JobPreferences(
            target_role_families=["engineering"],
            target_titles=["Engineer"],
            target_seniorities=["senior"],
        ),
    }
    base.update(overrides)
    return MasterProfile(**base)


def test_lever_apply_url_appends_apply():
    job = _job("https://jobs.lever.co/acme/123-abc")
    assert apply_url(job) == "https://jobs.lever.co/acme/123-abc/apply"


def test_lever_apply_url_idempotent_and_strips_query():
    job = _job("https://jobs.lever.co/acme/123-abc/apply/?lever-origin=applied")
    assert apply_url(job) == "https://jobs.lever.co/acme/123-abc/apply"


def test_ashby_application_url_appends_application():
    job = _job("https://jobs.ashbyhq.com/acme/456-def", source="ashby")
    assert application_url(job) == "https://jobs.ashbyhq.com/acme/456-def/application"


def test_profile_answers_covers_links_and_location():
    profile = _profile(
        links={
            "LinkedIn": "https://linkedin.com/in/ada",
            "github": "https://github.com/ada",
            "portfolio": "https://ada.dev",
        },
        residence=Residence(country="United States", city="Austin"),
    )
    pairs = profile_answers(profile)
    assert len(pairs) == 4

    by_match = {}
    for pattern, value in pairs:
        for probe in ("LinkedIn Profile", "GitHub", "Portfolio/Website", "Location"):
            if pattern.search(probe):
                by_match[probe] = value
    assert by_match["LinkedIn Profile"] == "https://linkedin.com/in/ada"
    assert by_match["GitHub"] == "https://github.com/ada"
    assert by_match["Portfolio/Website"] == "https://ada.dev"
    assert by_match["Location"] == "United States"  # residence country wins


def test_profile_answers_skips_missing_links():
    pairs = profile_answers(_profile(location="Canada"))
    assert [v for _, v in pairs] == ["Canada"]  # only the location pattern


def test_current_org_derivation_matches_lever_fill():
    # The lever submitter fills `org` from the open-ended experience entry.
    profile = _profile(
        experience=[
            Experience(
                company="OldCo",
                role="Engineer",
                start=date(2018, 1, 1),
                end=date(2020, 1, 1),
                bullets=[],
            ),
            Experience(
                company="Acme",
                role="Staff Engineer",
                start=date(2020, 2, 1),
                end=None,
                bullets=[],
            ),
        ]
    )
    current = next((e.company for e in profile.experience if e.end is None), None)
    assert current == "Acme"
