# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for deterministic resume bullet reranking (Phase 6)."""

from datetime import date

from models.job import ParsedJD
from models.profile import Bullet, Experience
from tools.tailoring.rerank import rerank_experience, score_bullet


def _jd() -> ParsedJD:
    return ParsedJD(
        summary="Backend role",
        required_skills=["python", "aws"],
        preferred_skills=["docker"],
    )


def test_score_bullet_weights() -> None:
    jd = _jd()
    # Two required-skill tag hits: 2 * 3.0 = 6.0
    assert score_bullet(Bullet(text="x", tags=["python", "aws"]), jd) == 6.0
    # One preferred (1.5) + quantified impact bonus (0.5) = 2.0
    assert (
        score_bullet(Bullet(text="x", tags=["docker"], impact="cut cost 50%"), jd) == 2.0
    )
    # No overlap, no impact = 0.0
    assert score_bullet(Bullet(text="x", tags=["banjo"]), jd) == 0.0


def test_rerank_orders_by_relevance_and_caps() -> None:
    jd = _jd()
    exp = Experience(
        company="C",
        role="R",
        start=date(2020, 1, 1),
        bullets=[
            Bullet(text="low", tags=["banjo"]),
            Bullet(text="high", tags=["python", "aws"]),
            Bullet(text="mid", tags=["docker"]),
        ],
    )
    out = rerank_experience([exp], jd, max_bullets_per_role=2)
    assert [b.text for b in out[0].bullets] == ["high", "mid"]  # ordered + capped


def test_rerank_keeps_floor_of_two() -> None:
    jd = _jd()
    exp = Experience(
        company="C",
        role="R",
        start=date(2020, 1, 1),
        bullets=[Bullet(text="a", tags=[]), Bullet(text="b", tags=[])],
    )
    # Cap of 1 is overridden by the floor of 2 for narrative continuity.
    out = rerank_experience([exp], jd, max_bullets_per_role=1)
    assert len(out[0].bullets) == 2


def test_rerank_does_not_mutate_bullet_text() -> None:
    jd = _jd()
    exp = Experience(
        company="C",
        role="R",
        start=date(2020, 1, 1),
        bullets=[Bullet(text="original wording", tags=["python"])],
    )
    out = rerank_experience([exp], jd)
    assert out[0].bullets[0].text == "original wording"
