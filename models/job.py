# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Job and parsed-JD models for the discovery pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class CompRange(BaseModel):
    min_total: int | None = None
    max_total: int | None = None
    currency: str = "USD"


class ParsedJD(BaseModel):
    role_family: Literal[
        "engineering",
        "product",
        "design",
        "data",
        "marketing",
        "sales",
        "customer-success",
        "operations",
        "finance",
        "people",
        "legal",
        "other",
    ] = "other"
    required_skills: list[str] = []
    preferred_skills: list[str] = []
    seniority: Literal[
        # IC track
        "junior",
        "mid",
        "senior",
        "staff",
        "principal",
        # management track
        "manager",
        "senior-manager",
        "director",
        "vp",
        "unspecified",
    ] = "unspecified"
    comp_range: CompRange | None = None
    remote_policy: Literal["remote", "hybrid", "onsite", "unspecified"] = "unspecified"
    # Structured work location, extracted best-effort from the JD + the freeform
    # Job.location string. Used by the matching geo-eligibility rule. Any field
    # may be null when the posting does not state it.
    job_country: str | None = None
    job_state: str | None = None  # state / province / region
    job_city: str | None = None
    # For remote roles, where remote workers may be based per the JD, e.g.
    # "United States", "US-only", "Europe", "EMEA", "Worldwide". None when the
    # role is onsite/hybrid or the scope is unstated.
    remote_scope: str | None = None
    # True only when the JD explicitly invites US-based remote workers (e.g.
    # "Remote - US", "US remote"). Drives the US-remote exception in matching
    # even when the company/office sits abroad.
    us_remote_ok: bool = False
    red_flags: list[str] = []
    summary: str


class Job(BaseModel):
    id: str  # hash(source + source_id) for dedup
    user_id: str
    source: Literal[
        "greenhouse", "lever", "ashby", "workday", "google_jobs", "gmail_alert", "manual"
    ]
    source_id: str  # the ID from the source system
    company: str
    title: str
    url: str
    location: str | None = None
    jd_raw: str
    jd_parsed: ParsedJD | None = None  # populated by the Matching Agent (Phase 4)
    discovered_at: datetime
    # "first time seeing this company" badge in the UI is driven by this.
    discovered_via: Literal["known", "unvetted", "manual"] = "known"
    user_decision: Literal[
        "pending", "approved", "rejected", "starred", "applied"
    ] = "pending"
