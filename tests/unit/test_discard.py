# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Discard rule: zero/geo-ineligible scores never stay in the jobs collection."""

from datetime import UTC, datetime

from models.job import Job, ParsedJD
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


def _job() -> Job:
    return Job(
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


# --- the negatives keep their features -------------------------------------
#
# ~71% of everything ever scored is tombstoned. Without the parse, that 71% is
# a pile of rejections with no features attached, and no ranking model can
# learn what a bad match looks like from it.


def _parsed() -> ParsedJD:
    return ParsedJD(
        role_family="engineering",
        seniority="staff",
        summary="Build things.",
        required_skills=["python"],
    )


def test_tombstone_carries_the_parsed_jd():
    job = _job()
    job.jd_parsed = _parsed()

    stone = discard_tombstone(job, _match(0))

    assert stone["jd_parsed"] == job.jd_parsed.model_dump(mode="json")
    assert stone["jd_parsed"]["role_family"] == "engineering"


def test_tombstone_carries_a_null_parse_when_there_was_none():
    """Present-and-null, not absent. A geo-enforced skip never pays for the
    Flash parse, and counting these later has to tell "nothing was parsed"
    apart from "this tombstone predates the field"."""
    job = _job()
    assert job.jd_parsed is None

    stone = discard_tombstone(job, _match(0))

    assert "jd_parsed" in stone
    assert stone["jd_parsed"] is None


def test_tombstone_still_refuses_jd_raw_even_with_a_parse():
    """The parse is cheap to keep; the raw JD is not, and it is refetchable
    from ``url``. Adding the parse must not drag the heavy field in with it."""
    job = _job()
    job.jd_parsed = _parsed()

    stone = discard_tombstone(job, _match(0))

    assert "jd_raw" not in stone
    assert "jd_raw" not in stone["jd_parsed"]
