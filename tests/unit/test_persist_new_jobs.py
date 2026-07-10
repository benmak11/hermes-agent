# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Discovery persistence guard: empty-JD jobs never reach Firestore.

An empty ``jd_raw`` can't be parsed or scored (Vertex rejects empty input),
so persisting one creates a doc that re-fails every scoring run forever.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import tools.discovery.pipeline as discovery
from models.job import Job


def _job(job_id: str, jd_raw: str) -> Job:
    return Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id=job_id,
        company="acme",
        title="Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=jd_raw,
        discovered_at=datetime.now(UTC),
    )


class _FakeDoc:
    def __init__(self, db, path: tuple):
        self._db = db
        self._path = path

    def collection(self, name: str):
        return _FakeCollection(self._db, (*self._path, name))

    async def get(self):
        return SimpleNamespace(exists=self._path in self._db.existing)

    async def set(self, data: dict):
        self._db.sets[self._path] = data


class _FakeCollection:
    def __init__(self, db, path: tuple):
        self._db = db
        self._path = path

    def document(self, doc_id: str):
        return _FakeDoc(self._db, (*self._path, doc_id))


class _FakeDB:
    def __init__(self, existing: set[tuple] | None = None):
        self.existing = existing or set()
        self.sets: dict[tuple, dict] = {}

    def collection(self, name: str):
        return _FakeCollection(self, (name,))


def test_empty_jd_jobs_are_never_persisted(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    new = asyncio.run(
        discovery.persist_new_jobs(
            [
                _job("good", "We build rockets."),
                _job("empty", ""),
                _job("whitespace", "  \n\t "),
            ]
        )
    )

    assert new == 1
    assert [path[-1] for path in db.sets] == ["good"]


def test_seen_and_discarded_jobs_are_skipped(monkeypatch):
    db = _FakeDB(
        existing={
            ("users", "u1", "jobs", "seen"),
            ("users", "u1", "discarded_jobs", "tombstoned"),
        }
    )
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    new = asyncio.run(
        discovery.persist_new_jobs(
            [_job("seen", "JD."), _job("tombstoned", "JD."), _job("fresh", "JD.")]
        )
    )

    assert new == 1
    assert [path[-1] for path in db.sets] == ["fresh"]
