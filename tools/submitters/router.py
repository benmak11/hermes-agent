# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-job submission router: pick the right submitter for the job source."""

from __future__ import annotations

from pathlib import Path

from models.job import Job
from models.profile import MasterProfile
from obs.logging import get_logger

from .ashby import submit_ashby
from .greenhouse import ProgressFn, submit_greenhouse
from .lever import submit_lever

log = get_logger("tools.submitters")

SUBMITTERS = {
    "greenhouse": submit_greenhouse,
    "lever": submit_lever,
    "ashby": submit_ashby,
}


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

    Path A (deterministic Playwright) covers greenhouse, lever, and ashby.
    Path B (Computer Use) for the rest is deferred — those sources report an
    unsupported failure rather than auto-submitting through an unverified path.
    """
    submitter = SUBMITTERS.get(job.source)
    if submitter is not None:
        return await submitter(
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
    return {
        "success": False,
        "error": (
            f"Automated submission for source '{job.source}' is not supported "
            "yet (Computer Use path deferred). Apply manually for now."
        ),
    }
