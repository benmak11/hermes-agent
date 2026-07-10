# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cross-user jd_cache: a posting parses once, ever.

Pins the cache module itself (content-hash keys, schema-drift self-healing,
chunked writes) and the integration contract in both scoring paths: cache
hits must skip Flash entirely, fresh parses must be written back.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tools.matching.batch as batch
import tools.matching.score as score
from models.job import Job, ParsedJD
from tools.matching import jd_cache


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


class _FakeSnap:
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return self._data


class _FakeBatchWriter:
    def __init__(self, sink: list):
        self._sink = sink

    def set(self, ref, doc: dict) -> None:
        self._sink.append((ref.id, doc))

    async def commit(self) -> None:
        pass


class _FakeDB:
    """Just enough Firestore: get_all by doc id + batched writes."""

    def __init__(self, docs: dict[str, dict] | None = None):
        self.docs = docs or {}
        self.writes: list[tuple[str, dict]] = []
        self.commits = 0

    def collection(self, name: str):
        assert name == jd_cache.COLLECTION
        return self

    def document(self, doc_id: str):
        return SimpleNamespace(id=doc_id)

    async def get_all(self, refs):
        for ref in refs:
            yield _FakeSnap(ref.id, self.docs.get(ref.id))

    def batch(self):
        self.commits += 1
        return _FakeBatchWriter(self.writes)


class _FakeRef:
    def __init__(self):
        self.updates: list[dict] = []

    async def update(self, fields: dict) -> None:
        self.updates.append(fields)


def test_jd_hash_is_stable_and_content_sensitive():
    assert jd_cache.jd_hash("abc") == jd_cache.jd_hash("abc")
    assert jd_cache.jd_hash("abc") != jd_cache.jd_hash("abc ")


def test_lookup_roundtrip():
    text = "We are hiring."
    db = _FakeDB()
    asyncio.run(jd_cache.store(db, text, _parsed(), model="gemini-2.5-flash"))
    assert db.writes and db.writes[0][0] == jd_cache.jd_hash(text)
    assert db.writes[0][1]["model"] == "gemini-2.5-flash"

    db.docs = dict(db.writes)
    hit = asyncio.run(jd_cache.lookup(db, text))
    assert hit == _parsed()
    assert asyncio.run(jd_cache.lookup(db, "different text")) is None


def test_lookup_skips_blank_texts_without_touching_firestore():
    class _ExplodingDB:
        def collection(self, name):  # pragma: no cover - must not be reached
            raise AssertionError("blank texts must not query Firestore")

    assert asyncio.run(jd_cache.lookup_many(_ExplodingDB(), ["", "   "])) == {}


def test_lookup_treats_schema_drift_as_miss():
    text = "We are hiring."
    db = _FakeDB({jd_cache.jd_hash(text): {"jd_parsed": {"role_family": 42}}})
    assert asyncio.run(jd_cache.lookup(db, text)) is None


def test_store_many_chunks_at_firestore_write_limit():
    parses = {f"jd {i}": _parsed() for i in range(jd_cache._WRITE_CHUNK + 1)}
    db = _FakeDB()
    asyncio.run(jd_cache.store_many(db, parses, model="m"))
    assert len(db.writes) == jd_cache._WRITE_CHUNK + 1
    assert db.commits == 2


def test_store_many_swallows_write_failures():
    class _FailingDB(_FakeDB):
        def batch(self):
            raise RuntimeError("firestore down")

    asyncio.run(jd_cache.store_many(_FailingDB(), {"jd": _parsed()}, model="m"))


def test_online_path_cache_hit_skips_flash(monkeypatch):
    job, ref = _job(), _FakeRef()
    profile = SimpleNamespace(
        preferences=SimpleNamespace(target_role_families=["engineering"])
    )
    parse_calls, store_calls = [], []

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_lookup(db, text):
        return _parsed()

    async def fake_parse(j):
        parse_calls.append(j.id)
        return _parsed()

    async def fake_store(db, text, parsed, *, model):
        store_calls.append(text)

    async def fake_match(*a, **kw):
        raise RuntimeError("pro call exploded")

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "parse_jd", fake_parse)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score.jd_cache, "lookup", fake_lookup)
    monkeypatch.setattr(score.jd_cache, "store", fake_store)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    asyncio.run(score.score_pending_jobs("u1"))

    assert parse_calls == []  # the whole point: no Flash call on a hit
    assert store_calls == []  # a hit is not re-stored
    assert ref.updates == [{"jd_parsed": _parsed().model_dump(mode="json")}]


def test_online_path_cache_miss_parses_then_stores(monkeypatch):
    job, ref = _job(), _FakeRef()
    profile = SimpleNamespace(
        preferences=SimpleNamespace(target_role_families=["engineering"])
    )
    store_calls = []

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_lookup(db, text):
        return None

    async def fake_parse(j):
        return _parsed()

    async def fake_store(db, text, parsed, *, model):
        store_calls.append((text, model))

    async def fake_match(*a, **kw):
        raise RuntimeError("pro call exploded")

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "parse_jd", fake_parse)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score.jd_cache, "lookup", fake_lookup)
    monkeypatch.setattr(score.jd_cache, "store", fake_store)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    asyncio.run(score.score_pending_jobs("u1"))

    assert store_calls == [(job.jd_raw, score.FLASH_MODEL)]


def test_batch_path_cache_hit_shrinks_the_flash_batch(monkeypatch):
    cached_job, fresh_job = _job("a", "Cached JD."), _job("b", "Fresh JD.")
    refs = {"a": _FakeRef(), "b": _FakeRef()}
    profile = SimpleNamespace(
        preferences=SimpleNamespace(target_role_families=["engineering"])
    )
    flash_batches, stored = [], []

    async def fake_load(db, user_id, limit=None):
        return profile, [(refs["a"], cached_job), (refs["b"], fresh_job)]

    async def fake_lookup_many(db, texts):
        assert sorted(texts) == ["Cached JD.", "Fresh JD."]
        return {"Cached JD.": _parsed()}

    async def fake_store_many(db, parses, *, model):
        stored.append((parses, model))

    async def fake_run_batch(*, model, lines, **kw):
        if model == batch.BATCH_FLASH_MODEL:
            flash_batches.append(lines)
            return [
                {
                    "request": {
                        "contents": [{"role": "user", "parts": [{"text": "Fresh JD."}]}]
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
    monkeypatch.setattr(batch.jd_cache, "lookup_many", fake_lookup_many)
    monkeypatch.setattr(batch.jd_cache, "store_many", fake_store_many)
    monkeypatch.setattr(batch.firestore, "AsyncClient", lambda: None)

    with pytest.raises(RuntimeError, match="pro batch exploded"):
        asyncio.run(batch.batch_score_pending_jobs("u1"))

    # Only the miss went to Flash; the fresh parse was written back.
    assert len(flash_batches) == 1 and len(flash_batches[0]) == 1
    assert stored == [({"Fresh JD.": _parsed()}, batch.BATCH_FLASH_MODEL)]
    # Both jobs got their parse persisted before the Pro stage failed.
    expected = {"jd_parsed": _parsed().model_dump(mode="json")}
    assert refs["a"].updates == [expected]
    assert refs["b"].updates == [expected]
