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
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from firestore_fakes import FakeStoreDB

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

    result = asyncio.run(
        discovery.persist_new_jobs(
            [
                _job("good", "We build rockets."),
                _job("empty", ""),
                _job("whitespace", "  \n\t "),
            ]
        )
    )

    assert result.new == 1
    assert [path[-1] for path in db.sets] == ["good"]


def test_seen_and_discarded_jobs_are_skipped(monkeypatch):
    db = _FakeDB(
        existing={
            ("users", "u1", "jobs", "seen"),
            ("users", "u1", "discarded_jobs", "tombstoned"),
        }
    )
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    result = asyncio.run(
        discovery.persist_new_jobs(
            [_job("seen", "JD."), _job("tombstoned", "JD."), _job("fresh", "JD.")]
        )
    )

    assert result.new == 1
    assert [path[-1] for path in db.sets] == ["fresh"]


def test_seen_check_is_batched_not_per_job(monkeypatch):
    """Two round trips for the whole cycle, not two reads per job."""
    db = _FakeDB()
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    jobs = [_job(f"j{i}", "JD.") for i in range(50)]
    assert asyncio.run(discovery.persist_new_jobs(jobs)).new == 50

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
    assert asyncio.run(discovery.persist_new_jobs(jobs)).new == 1

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

    assert asyncio.run(discovery.persist_new_jobs([_job("both", "JD.")])).new == 0
    assert db.sets == {}


def test_get_all_batches_are_chunked(monkeypatch):
    """Chunked at ``_GET_ALL_CHUNK``, so one cycle can't build one huge request."""
    db = _FakeDB()
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)

    chunk = discovery._GET_ALL_CHUNK
    jobs = [_job(f"j{i}", "JD.") for i in range(chunk + 1)]
    assert asyncio.run(discovery.persist_new_jobs(jobs)).new == chunk + 1

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

    assert asyncio.run(discovery.persist_new_jobs([a, b])).new == 2
    # Four single-document batches (two passes x two users), never one of two.
    assert db.get_all_batches == [1, 1, 1, 1]
    assert {path[1] for path in db.sets} == {"u1", "u2"}


# ---------------------------------------------------------------------------
# PERSIST_CAP_PER_CYCLE: at most N new jobs per user per search
#
# The top ``cap - explore`` by prerank plus ``explore`` drawn at random from
# the rest. Jobs left out are written nowhere, so the next search offers them
# again for free.
# ---------------------------------------------------------------------------

SEED = 7
PREFS = {
    "target_role_families": ["data"],
    "target_titles": ["Data Engineer"],
    "target_seniorities": ["senior"],
}
NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_cap_env(monkeypatch):
    monkeypatch.delenv(discovery.PERSIST_CAP_ENV, raising=False)
    monkeypatch.delenv(discovery.PERSIST_EXPLORE_ENV, raising=False)
    monkeypatch.setattr(discovery, "_warned_env", set())


def _posting(job_id: str, title: str, age_min: int, user_id: str = "u1") -> Job:
    return Job(
        id=job_id,
        user_id=user_id,
        source="greenhouse",
        source_id=job_id,
        company="acme",
        title=title,
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw="JD.",
        discovered_at=NOW - timedelta(minutes=age_min),
    )


def _board(good: int = 8, other: int = 12, user_id: str = "u1") -> list[Job]:
    """``good`` prerank-matching jobs, all *older* than the ``other`` ones, so
    a ranking by recency alone would pick differently from one by prerank."""
    return [
        _posting(f"good{i}", "Senior Data Engineer", 1000 + i, user_id)
        for i in range(good)
    ] + [_posting(f"other{i}", "Office Coordinator", i, user_id) for i in range(other)]


def _store(user_doc: dict | None = None, **collections) -> FakeStoreDB:
    data = {f"users/u1/{name}": docs for name, docs in collections.items()}
    if user_doc is not None:
        data["users"] = {"u1": user_doc}
    return FakeStoreDB(data)


def _written(db: FakeStoreDB, user_id: str = "u1") -> set[str]:
    return set(db.data.get(f"users/{user_id}/jobs", {}))


def _persist(monkeypatch, db, jobs, seed=SEED):
    monkeypatch.setattr(discovery.firestore, "AsyncClient", lambda: db)
    with structlog.testing.capture_logs() as logs:
        result = asyncio.run(discovery.persist_new_jobs(jobs, rng=random.Random(seed)))
    persisted = [e for e in logs if e["event"] == "discovery.persisted"]
    assert len(persisted) == 1
    return result, persisted[0], logs


@pytest.mark.parametrize("value", [None, "", "0", " 0 "])
def test_no_cap_writes_every_fresh_job_as_before(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(discovery.PERSIST_CAP_ENV, value)
    db = _store({"preferences": PREFS})
    jobs = _board(good=8, other=30)

    result, line, _ = _persist(monkeypatch, db, jobs)

    assert result == discovery.PersistResult(new=38)
    assert db.data["users/u1/jobs"] == {j.id: j.model_dump(mode="json") for j in jobs}
    # No cap, no extra read of the user document.
    assert db.gets == []
    assert (line["capped"], line["admitted_explore"]) == (0, 0)


def test_cap_admits_top_by_prerank_plus_random_picks(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    db = _store({"preferences": PREFS, "residence": {"country": "US"}})
    jobs = _board(good=8, other=12)

    result, line, _ = _persist(monkeypatch, db, jobs)

    # The rest, in rank order: all score alike, so newest first.
    rest = [f"other{i}" for i in range(12)]
    picks = random.Random(SEED).sample(rest, 2)
    # Precondition: the seed draws something other than the next two by rank,
    # or this test could not tell explore picks from a plain top 10.
    assert set(picks) != set(rest[:2])
    assert _written(db) == {f"good{i}" for i in range(8)} | set(picks)
    assert result == discovery.PersistResult(new=10, capped=10, admitted_explore=2)
    assert (line["new_jobs"], line["capped"], line["admitted_explore"]) == (10, 10, 2)
    # One read of the user document, for the preferences.
    assert db.gets == ["users/u1"]


def test_explore_admits_are_clamped_to_the_cap(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "3")
    monkeypatch.setenv(discovery.PERSIST_EXPLORE_ENV, "50")
    db = _store({"preferences": PREFS})

    result, _, _ = _persist(monkeypatch, db, _board(good=8, other=12))

    assert result == discovery.PersistResult(new=3, capped=17, admitted_explore=3)
    assert len(_written(db)) == 3


def test_zero_explore_admits_is_a_pure_top_k(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    monkeypatch.setenv(discovery.PERSIST_EXPLORE_ENV, "0")
    db = _store({"preferences": PREFS})

    result, _, _ = _persist(monkeypatch, db, _board(good=8, other=12))

    assert _written(db) == {f"good{i}" for i in range(8)} | {"other0", "other1"}
    assert result == discovery.PersistResult(new=10, capped=10, admitted_explore=0)


def test_fewer_fresh_jobs_than_the_cap_are_all_written(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    db = _store({"preferences": PREFS})
    jobs = _board(good=2, other=3)

    result, _, _ = _persist(monkeypatch, db, jobs)

    assert _written(db) == {j.id for j in jobs}
    assert result == discovery.PersistResult(new=5)
    assert db.gets == []


def test_unadmitted_jobs_are_written_nowhere_and_offered_again(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    db = _store({"preferences": PREFS})
    jobs = _board(good=8, other=12)

    _persist(monkeypatch, db, jobs)
    first = _written(db)
    left_out = {j.id for j in jobs} - first
    assert len(left_out) == 10
    # Nothing anywhere marks them seen: no job doc, no tombstone, no write.
    for docs in db.data.values():
        assert not left_out & set(docs)
    assert all(not path.endswith(tuple(left_out)) for path, _, _ in db.writes)

    # The next search finds them fresh again, and only them.
    result, line, _ = _persist(monkeypatch, db, jobs)
    assert line["seen_before"] == 10
    assert result == discovery.PersistResult(new=10)
    assert _written(db) == {j.id for j in jobs}


def test_seen_and_tombstoned_jobs_never_count_against_the_cap(monkeypatch):
    """The cap bites on *new* jobs. Seen and tombstoned jobs here outrank the
    fresh ones, so a cap applied before the seen-check would cut the fresh."""
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    seen = [_posting(f"seen{i}", "Senior Data Engineer", i) for i in range(15)]
    dead = [_posting(f"dead{i}", "Senior Data Engineer", i) for i in range(15)]
    fresh = [_posting(f"new{i}", "Office Coordinator", 100 + i) for i in range(6)]
    db = _store(
        {"preferences": PREFS},
        jobs={j.id: {"title": j.title} for j in seen},
        discarded_jobs={j.id: {"title": j.title} for j in dead},
    )

    result, line, _ = _persist(monkeypatch, db, seen + dead + fresh)

    assert result == discovery.PersistResult(new=6)
    assert {p for p, _, _ in db.writes} == {f"users/u1/jobs/new{i}" for i in range(6)}
    assert (line["seen_before"], line["previously_discarded"]) == (15, 15)


@pytest.mark.parametrize(
    "user_doc",
    [None, {}, {"preferences": None}, {"preferences": {"target_titles": 3}}],
)
def test_missing_preferences_rank_by_recency(monkeypatch, user_doc):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "4")
    monkeypatch.setenv(discovery.PERSIST_EXPLORE_ENV, "0")
    db = _store(user_doc)

    result, _, _ = _persist(monkeypatch, db, _board(good=8, other=12))

    # Every score is 0, so the newest four win, whatever their titles.
    assert _written(db) == {"other0", "other1", "other2", "other3"}
    assert result.new == 4


def test_a_failed_user_read_ranks_without_preferences(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "4")
    monkeypatch.setenv(discovery.PERSIST_EXPLORE_ENV, "0")
    db = _store({"preferences": PREFS})

    async def broken(*a, **kw):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(type(db.collection("users").document("u1")), "get", broken)

    result, _, logs = _persist(monkeypatch, db, _board(good=8, other=12))

    assert result.new == 4
    assert _written(db) == {"other0", "other1", "other2", "other3"}
    assert any(e["event"] == "discovery.persist_user_read_failed" for e in logs)


def test_the_cap_applies_per_user(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    db = _store({"preferences": PREFS})
    other_user = _board(good=8, other=12, user_id="u2")
    for job in other_user:
        job.id = f"u2-{job.id}"

    result, _, _ = _persist(monkeypatch, db, _board() + other_user)

    assert len(_written(db, "u1")) == 10
    assert len(_written(db, "u2")) == 10
    assert result == discovery.PersistResult(new=20, capped=20, admitted_explore=4)
    # One read per capped user.
    assert sorted(db.gets) == ["users/u1", "users/u2"]


@pytest.mark.parametrize("value", ["ten", "1.5", "-3"])
def test_a_bad_cap_means_no_cap_and_warns_once(monkeypatch, value):
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, value)
    db = _store({"preferences": PREFS})

    result, _, logs = _persist(monkeypatch, db, _board(good=8, other=12))
    with structlog.testing.capture_logs() as again:
        assert discovery.persist_cap() == 0

    assert result == discovery.PersistResult(new=20)
    warned = [e for e in logs + again if e["event"] == "discovery.persist_env_invalid"]
    assert len(warned) == 1
    assert warned[0]["name"] == discovery.PERSIST_CAP_ENV


def test_a_bad_explore_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv(discovery.PERSIST_EXPLORE_ENV, "lots")
    with structlog.testing.capture_logs() as logs:
        assert discovery.explore_admits(10) == discovery.DEFAULT_EXPLORE_ADMITS
        assert discovery.explore_admits(1) == 1
    assert [e["name"] for e in logs] == [discovery.PERSIST_EXPLORE_ENV]


def test_ranking_never_runs_the_parsed_term(monkeypatch):
    """Prerank's ``parsed`` term drives the real prefilter. Fresh postings have
    no parse, and the fields handed to prerank leave ``jd_parsed`` out anyway."""
    from tools.matching import prerank

    def refuse(*a, **kw):
        raise AssertionError("the parsed term ran at discovery")

    monkeypatch.setattr(prerank, "_parsed", refuse)
    monkeypatch.setenv(discovery.PERSIST_CAP_ENV, "10")
    db = _store({"preferences": PREFS})
    jobs = _board(good=8, other=12)

    result, _, _ = _persist(monkeypatch, db, jobs)

    assert result.new == 10
    assert all(j.jd_parsed is None for j in jobs)
    assert "jd_parsed" not in discovery._prerank_fields(jobs[0])
