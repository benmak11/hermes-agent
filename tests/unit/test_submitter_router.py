# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the application submitter router.

Only the routing/guard logic is covered here — the Greenhouse path launches a
real browser and is validated separately (dry-run), not in CI.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import tools.submitters.router as router
from models.job import Job
from models.profile import JobPreferences, MasterProfile
from tools.submitters.router import submit_application


def _job(source: str) -> Job:
    return Job(
        id="t",
        user_id="me",
        source=source,
        source_id="1",
        company="Acme",
        title="Engineer",
        url="https://example.com/job",
        jd_raw="jd",
        discovered_at=datetime.now(UTC),
    )


def _profile() -> MasterProfile:
    return MasterProfile(
        user_id="me",
        full_name="Ada Lovelace",
        email="ada@example.com",
        location="United States",
        objective_template="t",
        experience=[],
        education=[],
        skills={},
        preferences=JobPreferences(
            target_role_families=["engineering"],
            target_titles=["Engineer"],
            target_seniorities=["senior"],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["lever", "ashby", "workable", "google_jobs", "meta_jobs"]
)
async def test_router_unsupported_source_fails_gracefully(source: str) -> None:
    res = await submit_application(
        _job(source), _profile(), Path("/tmp/x.docx"), dry_run=True
    )
    assert res["success"] is False
    # Plain language: it reaches the user's timeline verbatim.
    assert res["error"] == router.UNSUPPORTED_SOURCE_ERROR
    for jargon in ("Computer Use", "not supported yet", "source", source):
        assert jargon not in res["error"]


@pytest.mark.asyncio
async def test_only_greenhouse_reaches_the_auto_submitter(monkeypatch) -> None:
    """Workable is manual-apply, like Lever and Ashby: the browser submitter is
    never driven at it. The spy keeps a misroute from launching Playwright."""
    called: list[str] = []

    async def spy(job, *args, **kwargs):
        called.append(job.source)
        return {"success": True}

    monkeypatch.setattr(router, "submit_greenhouse", spy)

    workable = await submit_application(
        _job("workable"), _profile(), Path("/tmp/x.docx"), dry_run=True
    )
    assert workable["success"] is False
    assert "mark it as applied" in workable["error"]
    assert called == []

    await submit_application(
        _job("greenhouse"), _profile(), Path("/tmp/x.docx"), dry_run=True
    )
    assert called == ["greenhouse"]
