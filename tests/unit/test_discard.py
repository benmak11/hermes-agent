# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Discard rule: zero/geo-ineligible scores never stay in the jobs collection."""

from datetime import UTC, datetime

from models.job import Job
from models.match import JobMatch, ScoreBreakdown
from tools.matching.score import DISCARD_AT_OR_BELOW, discard_tombstone, should_discard


def _match(score: float) -> JobMatch:
    return JobMatch(
        job_id="j1",
        overall_score=score,
        breakdown=ScoreBreakdown(
            role_fit=score,
            qualifications_match=score,
            seniority_match=score,
            comp_alignment=score,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="test",
        recommendation="skip",
    )


def test_discards_out_of_family_sentinel():
    assert should_discard(_match(0))


def test_discards_geo_ineligibility_cap():
    # The matching prompt caps geographically ineligible roles at 20.
    assert should_discard(_match(DISCARD_AT_OR_BELOW))


def test_keeps_anything_above_threshold():
    assert not should_discard(_match(DISCARD_AT_OR_BELOW + 1))
    assert not should_discard(_match(72))


def test_tombstone_is_minimal_but_traceable():
    job = Job(
        id="j1",
        user_id="u1",
        source="greenhouse",
        source_id="123",
        company="Acme",
        title="Staff PM",
        url="https://boards.greenhouse.io/acme/jobs/123",
        jd_raw="...",
        discovered_at=datetime.now(UTC),
    )
    stone = discard_tombstone(job, _match(0))
    assert stone["job_id"] == "j1"
    assert stone["score"] == 0
    assert stone["recommendation"] == "skip"
    assert "jd_raw" not in stone  # heavy fields stay out of the tombstone
    assert stone["discarded_at"]
