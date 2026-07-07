# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the Workable/SmartRecruiters/Recruitee fetcher helpers."""

from tools.ats.smartrecruiters import _jd_text
from tools.ats.smartrecruiters import _location as sr_location
from tools.ats.workable import _location as workable_location
from tools.discovery.dork import extract_slugs


def test_workable_location_variants() -> None:
    assert workable_location({"city": "Austin", "state": "TX", "country": "USA"}) == (
        "Austin, TX, USA"
    )
    assert workable_location({"telecommuting": True}) == "Remote"
    assert workable_location({"telecommuting": True, "city": "Athens"}) == (
        "Remote — Athens"
    )
    assert workable_location({}) is None


def test_smartrecruiters_location_and_jd() -> None:
    raw = {"location": {"city": "Clayton", "region": "VIC", "country": "au"}}
    assert sr_location(raw) == "Clayton, VIC, AU"
    assert sr_location({}) is None

    detail = {
        "jobAd": {
            "sections": {
                "jobDescription": {"title": "Job", "text": "<p>Build things</p>"},
                "qualifications": {"title": "Quals", "text": "<p>Python</p>"},
                "videos": {"urls": []},  # non-text section is skipped
            }
        }
    }
    jd = _jd_text(detail)
    assert "Build things" in jd and "Python" in jd
    assert _jd_text({}) == ""


def test_dork_slug_extraction_new_platforms() -> None:
    assert extract_slugs(
        ["https://apply.workable.com/blueground/j/38ABFA8E0D/"], "workable"
    ) == {"blueground"}
    # /j/{shortcode} job links must not yield "j" as a slug
    assert extract_slugs(["https://apply.workable.com/j/38ABFA8E0D"], "workable") == set()
    assert extract_slugs(
        ["https://jobs.smartrecruiters.com/BoschGroup/744000136114394-engineer"],
        "smartrecruiters",
    ) == {"BoschGroup"}
    assert extract_slugs(
        ["https://sendcloud.recruitee.com/o/senior-marketer"], "recruitee"
    ) == {"sendcloud"}
    assert extract_slugs(["https://www.recruitee.com/pricing"], "recruitee") == set()
