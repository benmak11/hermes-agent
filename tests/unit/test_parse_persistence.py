# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""jd_parsed is persisted the moment it exists, not only with the match.

The Flash parse is paid work. Before this change it reached Firestore only
inside ``persist_result`` — so any Pro-stage failure (467 in the 12K backlog)
or a dead batch process re-paid the parse on the next run. These tests pin
the new contract for both scoring paths: parse first, persist immediately,
then let scoring fail however it likes.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tools.matching.batch as batch
import tools.matching.score as score
from models.job import Job, ParsedJD


def _job(job_id="j1", jd_raw="Build things at Acme.") -> Job:
    return Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id="123",
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=jd_raw,
        discovered_at=datetime.now(UTC),
    )


def _parsed() -> ParsedJD:
    return ParsedJD(role_family="engineering", seniority="staff", summary="Build.")


class _FakeRef:
    def __init__(self, fail: bool = False):
        self.updates: list[dict] = []
        self._fail = fail

    async def update(self, fields: dict) -> None:
        if self._fail:
            raise RuntimeError("firestore down")
        self.updates.append(fields)


def test_persist_jd_parsed_writes_the_dump():
    job = _job()
    job.jd_parsed = _parsed()
    ref = _FakeRef()
    asyncio.run(score.persist_jd_parsed(ref, job))
    assert ref.updates == [{"jd_parsed": job.jd_parsed.model_dump(mode="json")}]


def test_persist_jd_parsed_noop_without_parse():
    ref = _FakeRef()
    asyncio.run(score.persist_jd_parsed(ref, _job()))
    assert ref.updates == []


def test_persist_jd_parsed_swallows_write_failures():
    # Best-effort by contract: scoring can proceed without the write.
    job = _job()
    job.jd_parsed = _parsed()
    asyncio.run(score.persist_jd_parsed(_FakeRef(fail=True), job))


def test_online_path_persists_parse_before_scoring_failure(monkeypatch):
    job, ref = _job(), _FakeRef()
    profile = SimpleNamespace(
        preferences=SimpleNamespace(target_role_families=["engineering"])
    )

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_parse(j):
        return _parsed()

    async def fake_match(*a, **kw):
        raise RuntimeError("pro call exploded")

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "parse_jd", fake_parse)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert counts["failed"] == 1
    # The parse survived the scoring failure — next run skips the Flash call.
    assert ref.updates == [{"jd_parsed": _parsed().model_dump(mode="json")}]


def test_batch_path_persists_parses_before_score_stage_failure(monkeypatch):
    job, ref = _job(), _FakeRef()
    profile = SimpleNamespace(
        preferences=SimpleNamespace(target_role_families=["engineering"])
    )

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_run_batch(*, model, lines, **kw):
        if model == batch.BATCH_FLASH_MODEL:
            return [
                {
                    "request": {
                        "contents": [{"role": "user", "parts": [{"text": job.jd_raw}]}]
                    },
                    "response": {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": _parsed().model_dump_json()}],
                                }
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 100,
                            "candidatesTokenCount": 50,
                            "totalTokenCount": 150,
                        },
                    },
                }
            ]
        raise RuntimeError("pro batch exploded")

    monkeypatch.setattr(batch, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(batch, "_run_batch", fake_run_batch)
    monkeypatch.setattr(batch, "build_match_context", lambda p: "CTX")
    monkeypatch.setattr(batch, "build_match_job_block", lambda j: "BLOCK")
    monkeypatch.setattr(batch, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch.firestore, "AsyncClient", lambda: None)

    with pytest.raises(RuntimeError, match="pro batch exploded"):
        asyncio.run(batch.batch_score_pending_jobs("u1"))

    # Stage 1's spend reached Firestore even though stage 3 blew up.
    assert ref.updates == [{"jd_parsed": _parsed().model_dump(mode="json")}]
