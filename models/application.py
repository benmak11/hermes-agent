# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Application model: tracks a tailored application from approval to submission."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ApplicationStatus = Literal[
    "queued",
    "tailoring",
    "ready_for_review",
    "submitting",
    "submitted",
    "failed",
    "responded",
    # The submitter filled everything it could, but required questions need a
    # human answer — the user finishes on the ATS form (unanswered_questions
    # lists exactly what's left). Retryable, e.g. after a profile update.
    "needs_input",
    # Terminal: the ATS reported the posting gone before we could submit.
    "posting_removed",
]


class Confirmation(BaseModel):
    submitted_at: datetime
    confirmation_id: str | None = None
    screenshot_uri: str | None = None


class StatusEvent(BaseModel):
    at: datetime
    status: str
    note: str | None = None


class Application(BaseModel):
    id: str
    user_id: str
    job_id: str
    # Denormalized for display on the review page (avoids a second job fetch).
    job_company: str | None = None
    job_title: str | None = None
    # Original posting URL — surfaced so the user can apply manually when
    # automated submission isn't possible (e.g. non-Greenhouse / custom forms).
    job_url: str | None = None
    status: ApplicationStatus
    # gs:// URI of the tailored resume docx produced by the Tailoring pipeline.
    resume_variant_uri: str | None = None
    # The generated, user-editable objective statement.
    objective_text: str | None = None
    cover_letter_uri: str | None = None
    # Snapshots per role so the review UI can diff master vs tailored without
    # re-running the (LLM) tailoring step. Each item: {company, role, bullets:[...]}.
    master_bullets: list[dict] = Field(default_factory=list)
    tailored_bullets: list[dict] = Field(default_factory=list)
    # Submission (Phase 7). last_submitted_at gates idempotency — a job is never
    # auto-resubmitted. screenshots holds {name, uri} evidence (pre-submit +
    # confirmation) uploaded to GCS.
    last_submitted_at: datetime | None = None
    # Human-readable labels of required form questions the submitter couldn't
    # answer from the profile (set with status=needs_input; the tracking UI
    # lists them next to the "complete your application" link).
    unanswered_questions: list[str] = Field(default_factory=list)
    screenshots: list[dict] = Field(default_factory=list)
    confirmation: Confirmation | None = None
    timeline: list[StatusEvent] = Field(default_factory=list)
