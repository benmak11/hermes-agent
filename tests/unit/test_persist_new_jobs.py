# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Discovery persistence: the seen-check, and the empty-JD guard.

An empty ``jd_raw`` can't be parsed or scored (Vertex rejects empty input),
so persisting one creates a doc that re-fails every scoring run forever.

The seen-check itself is batched through ``get_all`` rather than two ``get()``
calls per job. Its ordering is load-bearing — the ``discarded_jobs`` tombstone
is discovery's dedupe mechanism and must win over a live job doc — so the
precedence is pinned here independently of how the reads are issued.
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

    @property
    def id(self) -> str:
        return self._path[-1]

    def collection(self, name: str):
        return _FakeCollection(self._db, (*self._path, name))

    async def get(self):
        self._db.single_gets.append(self._path)
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
        #: Per-document ``.get()`` calls — the thing batching removes.
        self.single_gets: list[tuple] = []
        #: One entry per ``get_all`` round trip, holding its batch size.
        self.get_all_batches: list[int] = []

    def collection(self, name: str):
        return _FakeCollection(self, (name,))

    async def get_all(self, refs):
        """Mirror the real contract: a snapshot per ref, in no useful order.

        Reversing is not decoration. ``get_all`` explicitly does not promise to
        answer in the order it was asked, so a caller that matched snapshots to
        requests by position would pass against an ordered fake and silently
        mis-file every result in production.
        """
        self.get_all_batches.append(len(refs))
        for ref in reversed(list(refs)):
            yield SimpleNamespace(
                id=ref.id, exists=ref._path in self.existing, reference=ref
            )


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


def test_seen_check_is_batched_not_per_job(monkeypatch):
    """Two round trips for the whole cycle, not two reads per job."""
    db = _FakeDB()
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    jobs = [_job(f"j{i}", "JD.") for i in range(50)]
    assert asyncio.run(discovery.persist_new_jobs(jobs)) == 50

    # Not one per-document read anywhere: the old path issued 100.
    assert db.single_gets == []
    # One batch for the tombstones, one for the job docs.
    assert db.get_all_batches == [50, 50]


def test_tombstoned_jobs_cost_one_read_not_two(monkeypatch):
    """A tombstoned job is never looked up in ``jobs`` at all.

    The old path always issued both reads and then chose between them. Checking
    tombstones first means the 10:1 majority of a cycle (10,473 tombstoned vs
    1,033 kept, in the 12K backlog) is settled by a single read each.
    """
    db = _FakeDB(existing={("users", "u1", "discarded_jobs", "dead")})
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    jobs = [_job("dead", "JD."), _job("alive", "JD.")]
    assert asyncio.run(discovery.persist_new_jobs(jobs)) == 1

    # Both jobs are asked about in the tombstone batch; only the survivor
    # reaches the jobs batch.
    assert db.get_all_batches == [2, 1]
    assert [path[-1] for path in db.sets] == ["alive"]


def test_tombstone_wins_over_a_live_job_doc(monkeypatch):
    """Both present → discarded, exactly as the per-job version resolved it.

    This is discovery's dedupe mechanism: matching tombstones a zero-scored job
    but the posting stays live on the board for weeks, so the job keeps being
    re-fetched. If a leftover ``jobs`` doc could outrank the tombstone, the job
    would be re-persisted and re-scored on every cycle.
    """
    db = _FakeDB(
        existing={
            ("users", "u1", "discarded_jobs", "both"),
            ("users", "u1", "jobs", "both"),
        }
    )
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    assert asyncio.run(discovery.persist_new_jobs([_job("both", "JD.")])) == 0
    assert db.sets == {}


def test_get_all_batches_are_chunked(monkeypatch):
    """Chunked at ``_GET_ALL_CHUNK``, so one cycle can't build one huge request."""
    db = _FakeDB()
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    chunk = discovery._GET_ALL_CHUNK
    jobs = [_job(f"j{i}", "JD.") for i in range(chunk + 1)]
    assert asyncio.run(discovery.persist_new_jobs(jobs)) == chunk + 1

    # Tombstone pass then jobs pass, each split the same way.
    assert db.get_all_batches == [chunk, 1, chunk, 1]


def test_jobs_for_different_users_are_not_batched_together(monkeypatch):
    """A chunk must never mix users — ids are only unique within a user."""
    db = _FakeDB()
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    a = _job("shared-id", "JD.")
    b = _job("shared-id", "JD.")
    b.user_id = "u2"
    b.id = "other-id"

    assert asyncio.run(discovery.persist_new_jobs([a, b])) == 2
    # Four single-document batches (two passes x two users), never one of two.
    assert db.get_all_batches == [1, 1, 1, 1]
    assert {path[1] for path in db.sets} == {"u1", "u2"}
