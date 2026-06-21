# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Tailoring pipeline: turn an approved job into a ready-for-review Application.

Deterministic orchestration (run via cli/run_tailoring.py or the API approval
hook): rerank the candidate's bullets to the JD, rewrite the objective, render an
ATS-safe .docx, upload it to Cloud Storage, and assemble the Application record.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from models.application import Application, StatusEvent
from models.job import Job
from models.profile import MasterProfile

from .objective import generate_objective
from .render import render_resume_docx, upload_resume
from .rerank import rerank_experience


def application_id(job_id: str) -> str:
    """Stable per-job application id, so re-tailoring is idempotent."""
    return f"app-{job_id}"


async def tailor_application(
    job: Job,
    profile: MasterProfile,
    *,
    upload: bool = True,
) -> Application:
    """Produce a ready-for-review Application for an approved job.

    When ``upload`` is False the resume is rendered locally but not pushed to GCS
    (used by tests); ``resume_variant_uri`` is left as the local file path.
    """
    created_at = datetime.now(UTC)

    if job.jd_parsed is None:
        # Tailoring relies on the parsed JD (skills drive bullet reranking). A job
        # reaches "approved" only after matching, so this is normally populated.
        raise ValueError(f"Job {job.id} has no jd_parsed; run matching first.")

    reranked = rerank_experience(profile.experience, job.jd_parsed)
    objective = await generate_objective(profile, job)

    with tempfile.TemporaryDirectory() as tmp:
        local_path = render_resume_docx(
            profile, reranked, objective, Path(tmp) / "resume.docx"
        )
        if upload:
            resume_uri = upload_resume(local_path, profile.user_id, job.id)
        else:
            # Persist outside the temp dir so the caller can still read it.
            kept = Path(tempfile.gettempdir()) / f"resume-{job.id}.docx"
            kept.write_bytes(local_path.read_bytes())
            resume_uri = str(kept)

    def _snapshot(experience) -> list[dict]:
        return [
            {
                "company": exp.company,
                "role": exp.role,
                "bullets": [b.text for b in exp.bullets],
            }
            for exp in experience
        ]

    ready_at = datetime.now(UTC)
    return Application(
        id=application_id(job.id),
        user_id=profile.user_id,
        job_id=job.id,
        job_company=job.company,
        job_title=job.title,
        job_url=job.url,
        status="ready_for_review",
        resume_variant_uri=resume_uri,
        objective_text=objective,
        master_bullets=_snapshot(profile.experience),
        tailored_bullets=_snapshot(reranked),
        timeline=[
            StatusEvent(at=created_at, status="tailoring"),
            StatusEvent(at=ready_at, status="ready_for_review"),
        ],
    )
