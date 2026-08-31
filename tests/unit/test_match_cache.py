# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Context caching on the match_job static block (Phase 3.2 cost work)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from google.genai import types

import tools.genai_client as genai_client
import tools.matching.pipeline as pipeline
import tools.matching.score as score
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
    monkeypatch.setattr(genai_client.genai, "Client", lambda **kw: fake)


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
    assert call["config"].max_output_tokens == pipeline._MATCH_MAX_OUTPUT_TOKENS


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
    assert call["config"].max_output_tokens == pipeline._MATCH_MAX_OUTPUT_TOKENS


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
        asyncio.run(pipeline.match_job(_job(), _profile(), cached_content="caches/abc"))
    assert len(models.calls) == 1  # no uncached retry on a non-cache error


def test_create_match_cache_returns_none_on_failure(monkeypatch):
    class _FailingCaches:
        async def create(self, **kw):
            raise Exception("Cached content is too small")

    fake = SimpleNamespace(aio=SimpleNamespace(caches=_FailingCaches()))
    monkeypatch.setattr(genai_client.genai, "Client", lambda **kw: fake)
    assert asyncio.run(pipeline.create_match_cache(_profile())) is None


# ------------------------------------------------------- leaked-cache cleanup


class _FakeCaches:
    """Records deletes; ``list`` yields whatever caches are alive."""

    def __init__(self, alive):
        self._alive = alive
        self.deleted: list[str] = []
        self.created: list[dict] = []

    async def list(self, config=None):
        async def _iter():
            for cache in self._alive:
                yield cache

        return _iter()

    async def delete(self, *, name):
        self.deleted.append(name)

    async def create(self, *, model, config):
        self.created.append({"model": model, "config": config})
        return SimpleNamespace(name="caches/new", usage_metadata=None)


def _cache(name, display_name):
    return SimpleNamespace(name=name, display_name=display_name)


def test_creating_a_cache_buries_the_previous_run_s(monkeypatch):
    """Every run deletes its own cache in a finally — but a killed process
    leaves one standing and *billed* until its TTL runs out. The display name
    is a per-user singleton, so anything alive here is a leak."""
    caches = _FakeCaches(
        [
            _cache("caches/leaked", "hermes-match-u1"),
            _cache("caches/other-user", "hermes-match-u2"),
            _cache("caches/unrelated", None),
        ]
    )
    monkeypatch.setattr(
        genai_client.genai,
        "Client",
        lambda **kw: SimpleNamespace(aio=SimpleNamespace(caches=caches)),
    )

    assert asyncio.run(pipeline.create_match_cache(_profile())) == "caches/new"

    assert caches.deleted == ["caches/leaked"]  # nobody else's, ever
    assert caches.created[0]["config"].display_name == "hermes-match-u1"


def test_a_failing_reap_never_blocks_the_run(monkeypatch):
    class _UnlistableCaches(_FakeCaches):
        async def list(self, config=None):
            raise Exception("caches.list is not available here")

    caches = _UnlistableCaches([])
    monkeypatch.setattr(
        genai_client.genai,
        "Client",
        lambda **kw: SimpleNamespace(aio=SimpleNamespace(caches=caches)),
    )

    assert asyncio.run(pipeline.create_match_cache(_profile())) == "caches/new"


def test_reap_stops_at_the_scan_limit(monkeypatch):
    """caches.list has no display-name filter, so the walk is project-wide;
    it must stay bounded no matter what is in the project."""
    alive = [_cache(f"caches/{i}", "hermes-match-u1") for i in range(500)]
    caches = _FakeCaches(alive)
    client = SimpleNamespace(aio=SimpleNamespace(caches=caches))

    deleted = asyncio.run(pipeline.reap_match_caches(client, "hermes-match-u1"))

    assert deleted == pipeline._CACHE_SCAN_LIMIT


def test_cache_ttl_tracks_the_run_length_between_its_bounds():
    """The TTL has to outlive the run: a cache that expires mid-run bills the
    remaining jobs' input at 10x, so both bounds err long. The floor is what
    binds for a default 300-slot cycle, giving it headroom over the estimate
    rather than exactly none; the ceiling bounds a leak, and is deliberately
    far enough out that raising SCORING_BUDGET_PER_CYCLE can't silently push
    runs past it."""
    assert score.cache_ttl_seconds(2, 5) == score._CACHE_TTL_FLOOR_SECONDS
    # The default cycle budget: estimated at 1800s, floored to 2x that.
    assert score.cache_ttl_seconds(300, 5) == score._CACHE_TTL_FLOOR_SECONDS
    assert score.cache_ttl_seconds(1000, 5) == 6000  # tracks the run in between
    # Only an --ignore-budget backlog run is long enough to hit the ceiling.
    assert score.cache_ttl_seconds(13_000, 5) == 78_000
    assert score.cache_ttl_seconds(20_000, 5) == score._CACHE_TTL_CEILING_SECONDS


def test_the_scorer_sizes_its_cache_from_the_backlog(monkeypatch):
    ttls = []

    class _StopHere(Exception):
        """Nothing past the cache creation matters to this test."""

    async def fake_create(profile, *a, ttl_seconds=3600, **kw):
        ttls.append(ttl_seconds)
        raise _StopHere

    async def fake_load(db, user_id, limit=None):
        job = _job()
        return _profile(), [(SimpleNamespace(), job)] * 1000

    monkeypatch.setattr(score, "create_match_cache", fake_create)
    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)

    with pytest.raises(_StopHere):
        asyncio.run(score._score_pending(None, "u1", None, 5, None, {"attempted": 0}))

    assert ttls == [score.cache_ttl_seconds(1000, 5)]
