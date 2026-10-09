# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-job submission router: pick the right submitter for the job source."""

from __future__ import annotations

import os
from pathlib import Path

from models.job import Job
from models.profile import MasterProfile
from obs.logging import get_logger

from .greenhouse import ProgressFn, submit_greenhouse

log = get_logger("tools.submitters")

#: Sources with an automated submitter. Everything else is apply-it-yourself.
AUTO_SUBMIT_SOURCES = frozenset({"greenhouse"})

#: The failure an unsupported source reports. User-facing: ``web/`` renders
#: timeline notes verbatim.
UNSUPPORTED_SOURCE_ERROR = (
    "Hermes can't send this application for you. Apply on the employer's "
    "site, then mark it as applied."
)


def auto_submit_enabled() -> bool:
    """``AUTO_SUBMIT_ENABLED``, read per call. Off unless explicitly switched on."""
    return os.getenv("AUTO_SUBMIT_ENABLED", "").strip().lower() in {"1", "true", "on"}


def auto_submit_available(source: str | None) -> bool:
    """May a user ask Hermes to submit a job from ``source``?

    The flag gates the user-facing route only. The router below does not read
    it, so an operator's worker ``dry_run`` rehearsal works with it off.
    """
    return auto_submit_enabled() and source in AUTO_SUBMIT_SOURCES


async def submit_application(
    job: Job,
    profile: MasterProfile,
    resume_path: Path,
    *,
    dry_run: bool = False,
    headless: bool = True,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Submit ``job`` via the appropriate path based on ``job.source``.

    Only :data:`AUTO_SUBMIT_SOURCES` have a submitter; any other source reports
    a plain-language failure instead of driving an unverified path. The route
    refuses those sources first, so that branch is a backstop.
    """
    if job.source in AUTO_SUBMIT_SOURCES:
        return await submit_greenhouse(
            job,
            profile,
            resume_path,
            dry_run=dry_run,
            headless=headless,
            on_progress=on_progress,
        )

    log.warning(
        "submit.unsupported_source",
        job_id=job.id,
        company=job.company,
        source=job.source,
    )
    return {"success": False, "error": UNSUPPORTED_SOURCE_ERROR}
