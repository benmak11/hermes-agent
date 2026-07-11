# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the application submitter router (Phase 7).

Only the routing/guard logic is covered here — the per-ATS paths launch a
real browser and are validated separately (dry-run), not in CI.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

import tools.submitters.router as router_mod
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
@pytest.mark.parametrize("source", ["greenhouse", "lever", "ashby"])
async def test_router_dispatches_supported_sources(source: str, monkeypatch) -> None:
    seen: dict = {}

    async def fake_submit(job, profile, resume_path, **kwargs):
        seen["source"] = job.source
        return {"success": True, "dry_run": True}

    monkeypatch.setitem(router_mod.SUBMITTERS, source, fake_submit)
    res = await submit_application(
        _job(source), _profile(), Path("/tmp/x.docx"), dry_run=True
    )
    assert res["success"] is True
    assert seen["source"] == source


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["workday", "manual"])
async def test_router_unsupported_source_fails_gracefully(source: str) -> None:
    res = await submit_application(
        _job(source), _profile(), Path("/tmp/x.docx"), dry_run=True
    )
    assert res["success"] is False
    assert source in res["error"]
