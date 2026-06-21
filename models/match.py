# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Match-scoring schema produced by the Matching pipeline."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ScoreBreakdown(BaseModel):
    role_fit: float = Field(
        ge=0, le=100, description="Does the role match target_titles + target_role_families?"
    )
    qualifications_match: float = Field(
        ge=0,
        le=100,
        description="Overlap between JD required skills/qualifications and candidate's profile",
    )
    seniority_match: float = Field(
        ge=0, le=100, description="Seniority alignment (handles both IC and management tracks)"
    )
    comp_alignment: float = Field(
        ge=0, le=100, description="Comp meets/exceeds min_comp_total; 50 if unknown"
    )
    deal_breaker_penalty: float = Field(
        ge=0, le=100, description="100 = no deal-breakers hit, 0 = multiple hits"
    )


class JobMatch(BaseModel):
    job_id: str
    overall_score: float = Field(ge=0, le=100)
    breakdown: ScoreBreakdown
    matched_strengths: list[str] = Field(
        description="Specific bullets/skills from profile that fit"
    )
    gaps: list[str] = Field(description="Required skills missing from profile")
    red_flags_hit: list[str] = Field(description="Deal-breakers or red flags found in JD")
    reasoning: str = Field(description="2-3 sentence summary of the match")
    recommendation: Literal["strong_apply", "apply", "maybe", "skip"]
