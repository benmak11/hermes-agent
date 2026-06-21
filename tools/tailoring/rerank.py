# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deterministic bullet reranking for resume tailoring.

Cheap, LLM-free relevance scoring by tag overlap with the parsed JD, so a given
posting surfaces the candidate's most relevant accomplishments first. Bullet
wording is never changed — only reordered and pruned.
"""

from __future__ import annotations

from models.job import ParsedJD
from models.profile import Bullet, Experience


def score_bullet(bullet: Bullet, jd: ParsedJD) -> float:
    """Relevance of one bullet to a JD: weighted overlap of tags vs JD skills."""
    required = {s.lower() for s in jd.required_skills}
    preferred = {s.lower() for s in jd.preferred_skills}
    tags = {t.lower() for t in bullet.tags}

    score = 0.0
    score += 3.0 * len(tags & required)
    score += 1.5 * len(tags & preferred)
    # Bullets with quantified impact get a small bonus — measurable wins read best.
    if bullet.impact:
        score += 0.5
    return score


def rerank_experience(
    experience: list[Experience],
    jd: ParsedJD,
    max_bullets_per_role: int = 4,
) -> list[Experience]:
    """Return a copy of experience with each role's bullets reordered and pruned.

    Keeps a floor of 2 bullets per role even when low-relevance, so the resume
    keeps narrative continuity rather than collapsing to a sparse list. Ties
    preserve the original (chronological/authored) order via a stable sort.
    """
    out: list[Experience] = []
    for exp in experience:
        scored = sorted(exp.bullets, key=lambda b: score_bullet(b, jd), reverse=True)
        keep = max(2, min(max_bullets_per_role, len(scored)))
        new_exp = exp.model_copy(update={"bullets": scored[:keep]})
        out.append(new_exp)
    return out
