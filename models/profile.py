# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Canonical career-history schema (the "master profile")."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Bullet(BaseModel):
    """A single accomplishment bullet under a role."""

    text: str
    tags: list[str] = Field(
        default_factory=list,
        description="Skills, themes, technologies referenced. Used for matching.",
    )
    impact: str | None = Field(
        None,
        description="Quantified outcome if present, e.g. 'reduced p99 latency by 40%'",
    )


class Experience(BaseModel):
    company: str
    role: str
    start: date
    end: date | None = None  # None means current
    location: str | None = None
    bullets: list[Bullet]


class Education(BaseModel):
    institution: str
    degree: str
    field: str
    start_year: int
    end_year: int | None = None


class Residence(BaseModel):
    """Where the candidate is based. Drives the matching geo-eligibility rule.

    For onsite/hybrid roles, state and city tighten the match to the candidate's
    metro; when they are null the rule degrades to country-level matching.
    """

    country: str
    state: str | None = None  # state / province / region
    city: str | None = None


class JobPreferences(BaseModel):
    target_role_families: list[str] = Field(
        description=(
            "Functional families you're targeting. Jobs outside these are auto-skipped "
            "before scoring to save cost. Use lowercase slugs. "
            "E.g. ['engineering', 'product', 'developer-relations']. "
            "Common families at tech companies: engineering, product, design, data, "
            "marketing, sales, customer-success, operations, finance, people, legal."
        )
    )
    target_titles: list[str] = Field(
        description=(
            "Specific roles you'd actually take, across all your target families. "
            "E.g. ['Staff Software Engineer', 'Engineering Manager', 'Senior Product Manager']"
        )
    )
    target_seniorities: list[str] = Field(
        description=(
            "Levels you'd accept. Free-form to support both IC and management tracks. "
            "Suggested tech-company values — IC track: 'junior', 'mid', 'senior', 'staff', "
            "'principal'; management track: 'manager', 'senior-manager', 'director', 'vp'. "
            "These levels apply across functions at most tech companies, not just engineering."
        )
    )
    min_comp_total: int | None = None  # USD annual
    remote_policy: list[Literal["remote", "hybrid", "onsite"]] = ["remote"]
    locations: list[str] = []  # for hybrid/onsite
    must_haves: list[str] = Field(
        default_factory=list,
        description="Hard requirements, e.g. ['no on-call', 'IC track', 'reports to VP or above']",
    )
    deal_breakers: list[str] = Field(
        default_factory=list,
        description="Auto-reject signals, e.g. ['startup <20 people', 'requires 5 days in office']",
    )


class MasterProfile(BaseModel):
    user_id: str
    full_name: str
    email: str
    phone: str | None = None
    location: str
    # Structured residence used by the matching geo-eligibility rule. Optional for
    # backward compatibility; falls back to `location` (country-level) when absent.
    residence: Residence | None = None
    links: dict[str, str] = Field(
        default_factory=dict,
        description="e.g. {'github': '...', 'linkedin': '...', 'portfolio': '...'}",
    )
    objective_template: str = Field(
        description=(
            "Template with {role} and {company} placeholders. Keep it role-family-neutral "
            "or maintain one per family (see note below). "
            "E.g. '{seniority} professional with {years} years in {domain}, seeking a {role} "
            "role at {company} where I can apply my experience in...'. "
            "If you target multiple families (e.g. engineering AND product), consider "
            "storing a dict of templates keyed by family instead — see Phase 6."
        )
    )
    experience: list[Experience]
    education: list[Education]
    skills: dict[str, list[str]] = Field(
        description=(
            "User-defined categories — choose ones that span all your target families. "
            "Examples by field: engineering → {'languages': [...], 'frameworks': [...], "
            "'platforms': [...]}; product → {'methods': ['discovery', 'a/b testing'], "
            "'tools': ['amplitude', 'figma'], 'domains': ['fintech', 'b2b-saas']}; "
            "if targeting both, use a union: {'technical': [...], 'product': [...], "
            "'leadership': [...], 'domains': [...]}"
        )
    )
    preferences: JobPreferences
