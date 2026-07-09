# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Context caching on the match_job static block (Phase 3.2 cost work)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from google.genai import types

import tools.matching.pipeline as pipeline
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import JobPreferences, MasterProfile


def _profile() -> MasterProfile:
    return MasterProfile(
        user_id="u1",
        full_name="Terry Tester",
        email="t@example.com",
        location="Austin, TX, United States",
        objective_template="{role} at {company}",
        experience=[],
        education=[],
        skills={"technical": ["python"]},
        preferences=JobPreferences(
            target_role_families=["engineering"],
            target_titles=["Staff Software Engineer"],
            target_seniorities=["staff"],
        ),
    )


def _job() -> Job:
    return Job(
        id="j1",
        user_id="u1",
        source="greenhouse",
        source_id="123",
        company="Acme",
        title="Staff Software Engineer",
        url="https://boards.greenhouse.io/acme/jobs/123",
        jd_raw="Build things at Acme.",
        discovered_at=datetime.now(UTC),
        jd_parsed=ParsedJD(
            role_family="engineering", seniority="staff", summary="Build things."
        ),
    )


def _match_json() -> str:
    return JobMatch(
        job_id="j1",
        overall_score=80,
        breakdown=ScoreBreakdown(
            role_fit=80,
            qualifications_match=80,
            seniority_match=80,
            comp_alignment=80,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="test",
        recommendation="apply",
    ).model_dump_json()


def _response() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        model_version="gemini-3.1-pro-preview",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            candidates_token_count=50,
            total_token_count=150,
        ),
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text=_match_json())])
            )
        ],
    )


class _FakeModels:
    """Captures generate_content calls; raises queued errors first."""

    def __init__(self, errors: list[Exception] | None = None):
        self.calls: list[dict] = []
        self._errors = errors or []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._errors:
            raise self._errors.pop(0)
        return _response()


def _install_fake_client(monkeypatch, models: _FakeModels) -> None:
    fake = SimpleNamespace(aio=SimpleNamespace(models=models))
    monkeypatch.setattr(pipeline.genai, "Client", lambda **kw: fake)


def test_prompt_split_separates_static_from_per_job():
    context = pipeline.build_match_context(_profile())
    job_block = pipeline.build_match_job_block(_job())
    # Static block: profile, geography, rules — and nothing about the job.
    assert "Terry Tester" in context
    assert "# Scoring Rules" in context
    assert "Residence: Austin, TX, United States" in context
    assert "Acme" not in context
    # Per-job block: the job — and nothing from the profile.
    assert "Company: Acme" in job_block
    assert "Build things at Acme." in job_block
    assert "Terry Tester" not in job_block


def test_uncached_call_sends_full_prompt(monkeypatch):
    models = _FakeModels()
    _install_fake_client(monkeypatch, models)
    match = asyncio.run(pipeline.match_job(_job(), _profile()))
    assert match.job_id == "j1"
    assert len(models.calls) == 1
    (call,) = models.calls
    assert "Terry Tester" in call["contents"][0]  # static block inlined
    assert "Company: Acme" in call["contents"][0]
    assert call["config"].cached_content is None


def test_cached_call_sends_only_job_block(monkeypatch):
    models = _FakeModels()
    _install_fake_client(monkeypatch, models)
    match = asyncio.run(
        pipeline.match_job(_job(), _profile(), cached_content="caches/abc")
    )
    assert match.job_id == "j1"
    (call,) = models.calls
    assert call["config"].cached_content == "caches/abc"
    assert "Company: Acme" in call["contents"][0]
    assert "Terry Tester" not in call["contents"][0]  # static block NOT resent


def test_expired_cache_falls_back_to_uncached(monkeypatch):
    models = _FakeModels(errors=[Exception("CachedContent not found: caches/abc")])
    _install_fake_client(monkeypatch, models)
    match = asyncio.run(
        pipeline.match_job(_job(), _profile(), cached_content="caches/abc")
    )
    assert match.job_id == "j1"
    assert len(models.calls) == 2
    retry = models.calls[1]
    assert retry["config"].cached_content is None
    assert "Terry Tester" in retry["contents"][0]  # full prompt on the retry


def test_non_cache_error_does_not_double_spend(monkeypatch):
    models = _FakeModels(errors=[Exception("429 RESOURCE_EXHAUSTED")])
    _install_fake_client(monkeypatch, models)
    with pytest.raises(Exception, match="429"):
        asyncio.run(
            pipeline.match_job(_job(), _profile(), cached_content="caches/abc")
        )
    assert len(models.calls) == 1  # no uncached retry on a non-cache error


def test_create_match_cache_returns_none_on_failure(monkeypatch):
    class _FailingCaches:
        async def create(self, **kw):
            raise Exception("Cached content is too small")

    fake = SimpleNamespace(aio=SimpleNamespace(caches=_FailingCaches()))
    monkeypatch.setattr(pipeline.genai, "Client", lambda **kw: fake)
    assert asyncio.run(pipeline.create_match_cache(_profile())) is None
