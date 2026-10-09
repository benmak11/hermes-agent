# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.prune_backlog``: which jobs a prune may touch, the tombstone it
leaves, the order it writes in, and the undo that reverses it.

Runs on :class:`firestore_fakes.FakeStoreDB`, which honours ``where`` and
``select``, plus a write batch this module adds that records each commit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from firestore_fakes import FakeStoreDB

import cli.prune_backlog as pb
from models.job import Job
from tools.matching import prerank as pr
from tools.matching.score import PRUNED_BY_PRERANK

UID = "u1"
USER = {
    "preferences": {
        "target_role_families": ["engineering"],
        "target_titles": ["Staff Software Engineer"],
        "target_seniorities": ["staff"],
    },
    "residence": {"country": "US"},
}
GOOD = "Staff Software Engineer"  # prerank 90
BAD = "Account Executive"  # prerank -40
WORSE = "Office Manager"  # prerank -60
AT = "2026-10-09T12:00:00+00:00"


@pytest.fixture(autouse=True)
def curated(monkeypatch):
    monkeypatch.setattr(pr, "curated_slugs", lambda: frozenset({"acme"}))


class _Batch:
    def __init__(self, db: BatchDB):
        self._db = db
        self._ops: list[tuple] = []

    def set(self, ref, data, merge=False):
        self._ops.append(("set", ref, data))

    def delete(self, ref):
        self._ops.append(("delete", ref))

    async def commit(self):
        assert len(self._ops) <= 500, "Firestore refuses a batch over 500 writes"
        if self._db.fail_at == len(self._db.commits):
            raise RuntimeError("commit failed")
        for op in self._ops:
            if op[0] == "set":
                op[1]._write(op[2], False)
            else:
                await op[1].delete()
        self._db.commits.append([(op[0], op[1]._path) for op in self._ops])


class BatchDB(FakeStoreDB):
    """``FakeStoreDB`` plus ``batch()``. ``commits`` holds each committed
    batch's ``(op, path)`` list; ``fail_at`` makes that commit raise."""

    def __init__(self, data=None, fail_at: int | None = None):
        super().__init__(data)
        self.commits: list[list[tuple[str, str]]] = []
        self.fail_at = fail_at

    def batch(self):
        return _Batch(self)


def job(job_id: str, title: str, *, decision="pending", source="greenhouse", **extra):
    doc = Job(
        id=job_id,
        user_id=UID,
        source=source,
        source_id=job_id,
        company="other",
        title=title,
        url=f"https://example.com/{job_id}",
        jd_raw="jd",
        discovered_at=datetime(2026, 9, 1, tzinfo=UTC),
        user_decision=decision,
    ).model_dump(mode="json")
    return {**doc, **extra}


def scored_tomb(job_id: str, **extra) -> dict:
    return {"job_id": job_id, "title": BAD, "score": 0, **extra}


def _db(jobs: dict, *, tombs=None, apps=None, fail_at=None) -> BatchDB:
    return BatchDB(
        {
            "users": {UID: USER},
            f"users/{UID}/jobs": jobs,
            f"users/{UID}/discarded_jobs": dict(tombs or {}),
            f"users/{UID}/applications": dict(apps or {}),
        },
        fail_at=fail_at,
    )


def _jobs(db) -> dict:
    return db.data[f"users/{UID}/jobs"]


def _tombs(db) -> dict:
    return db.data[f"users/{UID}/discarded_jobs"]


def mixed() -> dict:
    """Two prunable jobs at -40 and -60, and one of every kind never pruned."""
    return {
        "bad": job("bad", BAD, source="lever"),
        "worse": job("worse", WORSE),
        "good": job("good", GOOD),
        "scored": job("scored", BAD, match={"overall_score": 10}),
        "starred": job("starred", BAD, decision="starred"),
        "rejected": job("rejected", BAD, decision="rejected"),
        "approved": job("approved", BAD, decision="approved"),
        "applied": job("applied", BAD),
        "applied-by-field": job("applied-by-field", BAD),
    }


APPS = {
    "app-applied": {"job_id": "applied", "status": "submitted"},
    "legacy-id": {"job_id": "applied-by-field", "status": "failed"},
}


async def _prune(db, cutoff=-40.0, *, limit=None, write=True):
    return await pb.run_prune(
        db, UID, cutoff, limit=limit, write=write, enforce_geo=False
    )


# ------------------------------------------------------------------ selection


@pytest.mark.asyncio
async def test_plan_picks_pending_unscored_at_or_below_the_cutoff_only():
    db = _db(mixed(), apps=APPS)
    plan = await pb.plan_prune(db, UID, -40.0, limit=None, enforce_geo=False)
    assert plan is not None
    assert [c.job_id for c in plan.prune] == ["worse", "bad"]  # lowest first
    assert [c.score for c in plan.prune] == [-60.0, -40.0]
    assert plan.kept == 1  # "good"
    assert plan.applied == 2
    # One projected, equality-filtered query over jobs; jd_raw never read.
    assert (f"users/{UID}/jobs", (("user_decision", "pending"),), (), None) in (
        db.queries
    )
    selected = dict(db.selects)
    assert "jd_raw" not in selected[f"users/{UID}/jobs"]


@pytest.mark.asyncio
async def test_a_tie_at_the_cutoff_is_pruned_and_just_below_is_not():
    db = _db(mixed(), apps=APPS)
    tie = await pb.plan_prune(db, UID, -40.0, limit=None, enforce_geo=False)
    below = await pb.plan_prune(db, UID, -40.5, limit=None, enforce_geo=False)
    assert tie is not None and below is not None
    assert {c.job_id for c in tie.prune} == {"bad", "worse"}
    assert {c.job_id for c in below.prune} == {"worse"}


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_reports(capsys):
    db = _db(mixed(), apps=APPS)
    plan, out = await _prune(db, write=False)
    assert plan is not None and out is None
    assert db.commits == [] and db.writes == [] and db.deletes == []
    assert set(_jobs(db)) == set(mixed())
    text = capsys.readouterr().out
    assert "prune 2  keep 1" in text
    assert "skipped 2 pending job(s) with an application" in text
    assert "1  lever" in text and "1  greenhouse" in text
    # The riskiest cut (highest prerank) is listed first.
    assert text.index(BAD) < text.index(WORSE)
    assert "dry run: nothing written" in text


@pytest.mark.asyncio
async def test_the_sample_is_the_fifteen_highest_prerank_prunes(capsys):
    jobs = {f"w{i:02d}": job(f"w{i:02d}", WORSE) for i in range(20)}
    jobs["b"] = job("b", BAD)
    await _prune(_db(jobs), write=False)
    text = capsys.readouterr().out
    assert "the 15 highest-prerank jobs being pruned" in text
    assert text.count(BAD) == 1 and text.count(WORSE) == 14


@pytest.mark.asyncio
async def test_write_prunes_exactly_the_eligible_jobs():
    db = _db(mixed(), apps=APPS)
    _, out = await _prune(db)
    assert out is not None and (out.tombstoned, out.deleted) == (2, 2)
    assert set(_tombs(db)) == {"bad", "worse"}
    assert set(_jobs(db)) == set(mixed()) - {"bad", "worse"}


@pytest.mark.asyncio
async def test_limit_prunes_the_lowest_prerank_first():
    db = _db(mixed(), apps=APPS)
    plan, _ = await _prune(db, limit=1)
    assert plan is not None and plan.over_limit == 1
    assert set(_tombs(db)) == {"worse"}
    assert "bad" in _jobs(db)


@pytest.mark.asyncio
async def test_a_job_decided_after_the_plan_is_not_pruned():
    db = _db(mixed(), apps=APPS)
    plan = await pb.plan_prune(db, UID, -40.0, limit=None, enforce_geo=False)
    assert plan is not None
    _jobs(db)["bad"]["user_decision"] = "starred"
    _jobs(db)["worse"]["match"] = {"overall_score": 50}
    out = await pb.write_prune(db, UID, plan, -40.0, at=AT)
    assert (out.stale, out.deleted) == (2, 0)
    assert _tombs(db) == {} and db.commits == []


@pytest.mark.asyncio
async def test_a_job_applied_to_after_the_plan_is_not_pruned():
    """The plan's application check can be stale by write time; the write
    re-reads applications, so a job applied to in between keeps its doc."""
    db = _db(mixed(), apps=APPS)
    plan = await pb.plan_prune(db, UID, -40.0, limit=None, enforce_geo=False)
    assert plan is not None
    db.data[f"users/{UID}/applications"]["app-bad"] = {
        "job_id": "bad",
        "status": "queued",
    }
    out = await pb.write_prune(db, UID, plan, -40.0, at=AT)
    assert out.stale == 1
    assert "bad" in _jobs(db) and "bad" not in _tombs(db)
    assert set(_tombs(db)) == {"worse"}


@pytest.mark.asyncio
async def test_an_unrestorable_doc_is_never_pruned():
    jobs = {
        "bad": job("bad", BAD),
        "broken": {"title": BAD, "user_decision": "pending"},
    }
    db = _db(jobs)
    _, out = await _prune(db)
    assert out is not None and out.unrestorable == 1
    assert set(_tombs(db)) == {"bad"} and "broken" in _jobs(db)


# ---------------------------------------------------------- tombstone, order


@pytest.mark.asyncio
async def test_tombstone_carries_restore_and_the_prune_stamp():
    original = mixed()
    db = _db(mixed(), apps=APPS)
    plan = await pb.plan_prune(db, UID, -40.0, limit=None, enforce_geo=False)
    assert plan is not None
    await pb.write_prune(db, UID, plan, -40.0, at=AT)
    stone = _tombs(db)["bad"]
    assert stone["restore"] == original["bad"]
    assert stone["pruned"] == {
        "by": PRUNED_BY_PRERANK,
        "version": pr.PRERANK_VERSION,
        "cutoff": -40.0,
        "prerank": -40.0,
        "at": AT,
    }
    assert (stone["job_id"], stone["title"], stone["discarded_at"]) == ("bad", BAD, AT)
    # No model judged it, so nothing that reads as a Pro outcome.
    for key in ("score", "recommendation", "reasoning", "scored_with", "match"):
        assert key not in stone


@pytest.mark.asyncio
async def test_tombstones_commit_before_the_deletes():
    db = _db(mixed(), apps=APPS)
    await _prune(db)
    assert len(db.commits) == 2
    sets, deletes = db.commits
    assert {op for op, _ in sets} == {"set"}
    assert {p.rsplit("/", 2)[-2] for _, p in sets} == {"discarded_jobs"}
    assert {op for op, _ in deletes} == {"delete"}
    assert {p.rsplit("/", 2)[-2] for _, p in deletes} == {"jobs"}


@pytest.mark.asyncio
async def test_a_failure_between_tombstone_and_delete_leaves_both():
    db = _db(mixed(), apps=APPS, fail_at=1)
    with pytest.raises(RuntimeError):
        await _prune(db)
    assert {"bad", "worse"} <= set(_jobs(db))
    assert set(_tombs(db)) == {"bad", "worse"}


@pytest.mark.asyncio
async def test_more_than_five_hundred_jobs_commit_in_bounded_batches():
    jobs = {f"j{i:04d}": job(f"j{i:04d}", BAD) for i in range(1001)}
    db = _db(jobs)
    _, out = await _prune(db)
    assert out is not None and out.deleted == 1001
    assert _jobs(db) == {} and len(_tombs(db)) == 1001
    assert len(db.commits) == 6
    assert max(len(c) for c in db.commits) <= 500


# ----------------------------------------------------------------------- undo


async def _undo(db, *, write=True, limit=None, **filters):
    return await pb.run_undo(db, UID, limit=limit, write=write, **filters)


def _with_history() -> dict:
    return {
        "scored-tomb": scored_tomb("scored-tomb"),
        "geo-tomb": scored_tomb(
            "geo-tomb",
            geo_gate={"enforced": True},
            restore=job("geo-tomb", BAD),
        ),
    }


@pytest.mark.asyncio
async def test_undo_restores_only_pruned_tombstones():
    db = _db(mixed(), tombs=_with_history(), apps=APPS)
    await _prune(db)
    out = await _undo(db)
    assert (out.matched, out.restored) == (2, 2)
    assert set(_tombs(db)) == {"scored-tomb", "geo-tomb"}
    assert {"bad", "worse"} <= set(_jobs(db))
    assert "geo-tomb" not in _jobs(db) and "scored-tomb" not in _jobs(db)


@pytest.mark.asyncio
async def test_prune_then_undo_restores_identical_job_docs():
    db = _db(mixed(), tombs=_with_history(), apps=APPS)
    await _prune(db)
    await _undo(db)
    assert _jobs(db) == mixed()
    assert _tombs(db) == _with_history()


@pytest.mark.asyncio
async def test_undo_writes_the_job_before_dropping_the_tombstone():
    db = _db(mixed(), apps=APPS)
    await _prune(db)
    db.commits.clear()
    await _undo(db)
    sets, deletes = db.commits
    assert {(op, p.rsplit("/", 2)[-2]) for op, p in sets} == {("set", "jobs")}
    assert {(op, p.rsplit("/", 2)[-2]) for op, p in deletes} == {
        ("delete", "discarded_jobs")
    }


@pytest.mark.asyncio
async def test_a_failed_undo_leaves_the_job_in_both_places():
    db = _db(mixed(), apps=APPS)
    await _prune(db)
    db.fail_at = len(db.commits) + 1
    with pytest.raises(RuntimeError):
        await _undo(db)
    assert {"bad", "worse"} <= set(_jobs(db)) and set(_tombs(db)) == {"bad", "worse"}


@pytest.mark.asyncio
async def test_undo_dry_run_writes_nothing(capsys):
    db = _db(mixed(), apps=APPS)
    await _prune(db)
    db.commits.clear()
    out = await _undo(db, write=False)
    assert out.restored == 2 and db.commits == []
    assert set(_tombs(db)) == {"bad", "worse"}
    assert "would restore 2 of 2" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_undo_leaves_a_live_job_alone_and_drops_the_stale_tombstone():
    """A prune interrupted between its batches; the job has since been scored."""
    db = _db(mixed(), apps=APPS, fail_at=1)
    with pytest.raises(RuntimeError):
        await _prune(db)
    _jobs(db)["bad"]["match"] = {"overall_score": 75}
    db.fail_at = None
    out = await _undo(db)
    assert out.already_live == 2 and out.restored == 0
    assert _jobs(db)["bad"]["match"] == {"overall_score": 75}
    assert _tombs(db) == {}


@pytest.mark.asyncio
async def test_undo_filters_by_version_cutoff_and_since():
    db = _db({"bad": job("bad", BAD), "worse": job("worse", WORSE)})
    plan = await pb.plan_prune(db, UID, -50.0, limit=None, enforce_geo=False)
    assert plan is not None
    await pb.write_prune(db, UID, plan, -50.0, at="2026-10-01T00:00:00+00:00")
    plan = await pb.plan_prune(db, UID, -40.0, limit=None, enforce_geo=False)
    assert plan is not None
    await pb.write_prune(db, UID, plan, -40.0, at="2026-10-08T00:00:00+00:00")
    assert _tombs(db)["worse"]["pruned"]["cutoff"] == -50.0

    assert await pb.pruned_ids(db, UID, cutoff=-40.0) == ["bad"]
    assert await pb.pruned_ids(db, UID, version=pr.PRERANK_VERSION + 1) == []
    since = datetime(2026, 10, 5, tzinfo=UTC)
    assert await pb.pruned_ids(db, UID, since=since) == ["bad"]
    # "Z" and "+00:00" name the same instant.
    exact = pb._since_arg("2026-10-08T00:00:00Z")
    assert await pb.pruned_ids(db, UID, since=exact) == ["bad"]

    out = await _undo(db, cutoff=-50.0)
    assert out.restored == 1
    assert set(_jobs(db)) == {"worse"} and set(_tombs(db)) == {"bad"}


@pytest.mark.asyncio
async def test_undo_reads_only_the_pruned_stamp_when_listing():
    db = _db(mixed(), tombs=_with_history(), apps=APPS)
    await _prune(db)
    await pb.pruned_ids(db, UID)
    assert (f"users/{UID}/discarded_jobs", ("pruned",)) in db.selects


def test_matches_filter_never_accepts_a_scored_tombstone():
    assert not pb.matches_filter(scored_tomb("x"))
    assert not pb.matches_filter({"pruned": {"by": "someone-else"}})
    assert pb.matches_filter({"pruned": {"by": PRUNED_BY_PRERANK}})
