# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Title pre-filter: only confidently out-of-family titles are dropped.

A wrong drop here silently loses a job the user might have wanted, so these
tests pin the precision-first contract: ambiguous titles must pass through to
the Flash parse, and explicit target_titles always win over the keyword map.
"""

from datetime import UTC, datetime

import pytest

from models.job import Job
from models.profile import JobPreferences
from tools.discovery.title_filter import classify_title, prefilter_jobs


def _job(title: str) -> Job:
    return Job(
        id=title.lower().replace(" ", "-"),
        user_id="u1",
        source="greenhouse",
        source_id="1",
        company="acme",
        title=title,
        url="https://boards.greenhouse.io/acme/jobs/1",
        jd_raw="...",
        discovered_at=datetime.now(UTC),
    )


def _prefs(families: list[str], titles: list[str] | None = None) -> JobPreferences:
    return JobPreferences(
        target_role_families=families,
        target_titles=titles or [],
        target_seniorities=["senior", "staff"],
    )


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Senior Software Engineer", "engineering"),
        ("Staff Backend Developer", "engineering"),
        ("Engineering Manager", "engineering"),
        # Word boundary: "Salesforce" must not read as sales.
        ("Salesforce Developer", "engineering"),
        ("Growth Engineer", None),  # growth = ambiguous, engineer or marketer
        ("Product Manager", "product"),
        ("Head of Product", "product"),
        ("Product Designer", "design"),  # design, not product
        ("Product Marketing Manager", "marketing"),  # marketing, not product
        ("Data Scientist", "data"),
        ("Account Executive", "sales"),
        ("Account Manager", "sales"),  # sales, not finance
        ("Enterprise Partnerships Lead", "sales"),
        ("Customer Success Manager", "customer-success"),
        ("Technical Recruiter", "people"),
        ("People Operations Partner", "people"),  # people, not operations
        ("Staff Accountant", "finance"),
        ("Payroll Specialist", "finance"),
        ("General Counsel", "legal"),
        ("Content Strategist", "marketing"),
        ("Supply Chain Analyst", "operations"),
        ("Executive Assistant", "operations"),
        ("Chief of Staff", "operations"),
    ],
)
def test_confident_classifications(title: str, family: str | None):
    assert classify_title(title) == family


@pytest.mark.parametrize(
    "title",
    [
        # Straddle two families — Flash must stay the arbiter.
        "Data Engineer",
        "Machine Learning Engineer",
        "Software Engineer, Machine Learning",
        "Analytics Engineer",
        "Solutions Architect",
        "Sales Engineer",
        "Technical Support Engineer",
        "Technical Program Manager",
        "Developer Advocate",
        "UX Researcher",
        # No signal at all.
        "Business Analyst",
        "Wizard of Light Bulb Moments",
    ],
)
def test_ambiguous_titles_are_not_classified(title: str):
    assert classify_title(title) is None


def test_drops_only_confident_out_of_family():
    jobs = [
        _job("Senior Software Engineer"),  # in-family
        _job("Account Executive"),  # confident sales -> dropped
        _job("General Counsel"),  # confident legal -> dropped
        _job("Data Engineer"),  # ambiguous -> kept for Flash
        _job("Chief Vibes Officer"),  # unclassifiable -> kept for Flash
    ]
    kept, dropped = prefilter_jobs(jobs, _prefs(["engineering"]))
    assert [j.title for j in kept] == [
        "Senior Software Engineer",
        "Data Engineer",
        "Chief Vibes Officer",
    ]
    assert dropped == {"sales": 1, "legal": 1}


def test_target_titles_override_the_keyword_map():
    # User explicitly wants a title the map would classify out-of-family.
    prefs = _prefs(["engineering"], titles=["Product Manager"])
    kept, dropped = prefilter_jobs([_job("Senior Product Manager")], prefs)
    assert len(kept) == 1
    assert not dropped


def test_multiple_target_families():
    jobs = [_job("Product Manager"), _job("Software Engineer"), _job("SDR")]
    kept, dropped = prefilter_jobs(jobs, _prefs(["engineering", "product"]))
    assert [j.title for j in kept] == ["Product Manager", "Software Engineer"]
    assert dropped == {"sales": 1}


def test_no_preferences_keeps_everything():
    jobs = [_job("Account Executive"), _job("Paralegal")]
    kept, dropped = prefilter_jobs(jobs, None)
    assert kept == jobs
    assert not dropped


def test_empty_target_families_keeps_everything():
    jobs = [_job("Account Executive")]
    kept, dropped = prefilter_jobs(jobs, _prefs([]))
    assert kept == jobs
    assert not dropped


def test_family_targets_are_case_insensitive():
    kept, dropped = prefilter_jobs([_job("Software Engineer")], _prefs(["Engineering"]))
    assert len(kept) == 1
    assert not dropped
