# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``PRERANK_MODE``: which unscored jobs ``load_profile_and_pending`` hands the
scorers, and the ``users/{uid}/selections`` record each pick leaves."""

from __future__ import annotations

import asyncio
import random

import pytest
import structlog
from firestore_fakes import FakeStoreDB, _StoreDoc

from models.job import ParsedJD
from tools import selections
from tools.matching import prerank as pr
from tools.matching import selection
from tools.matching.score import load_profile_and_pending

GOOD = "Staff Software Engineer"
MID = "Software Engineer"
BAD = "Account Executive"

USER = {
    "user_id": "u1",
    "full_name": "Test Candidate",
    "email": "test@example.com",
    "location": "Somewhere",
    "objective_template": "{role} at {company}",
    "experience": [],
    "education": [],
    "skills": {},
    "preferences": {
        "target_role_families": ["engineering"],
        "target_titles": [GOOD],
        "target_seniorities": ["staff"],
    },
    "residence": {"country": "US"},
}

JOBS_PATH = "users/u1/jobs"
SELECTIONS_PATH = f"users/u1/{selections.COLLECTION}"

# The unchanged path's exact reads: the user doc, then one unprojected,
# unordered, unlimited query on pending.
TODAY_QUERIES = [(JOBS_PATH, (("user_decision", "pending"),), (), None)]


def _job(job_id: str, title: str, day: int, **extra) -> dict:
    return {
        "id": job_id,
        "user_id": "u1",
        "source": "greenhouse",
        "source_id": job_id,
        "company": "acme",
        "title": title,
        "url": f"https://example.com/{job_id}",
        "jd_raw": "jd",
        "discovered_at": f"2026-09-{day:02d}T00:00:00Z",
        "user_decision": "pending",
        **extra,
    }


def _jobs() -> dict:
    """Stream (insertion) order puts the weak jobs first, so ``off`` and the
    prerank disagree. a2 and a4 tie on prerank; a4 is newer."""
    return {
        "a1": _job("a1", BAD, 1),
        "a2": _job("a2", GOOD, 2),
        "a3": _job("a3", BAD, 3),
        "a4": _job("a4", GOOD, 9),
        "a5": _job("a5", MID, 5),
        "a6": _job("a6", BAD, 6),
        "s1": _job("s1", GOOD, 7, match={"overall_score": 80}),
        "x1": _job("x1", GOOD, 8, user_decision="approved"),
    }


UNSCORED = ["a1", "a2", "a3", "a4", "a5", "a6"]
TOP3 = ["a4", "a2", "a5"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(pr, "curated_slugs", lambda: frozenset())
    for name in ("PRERANK_MODE", "SELECTION_EXPLORE_RATE", "GEO_GATE_ENFORCE"):
        monkeypatch.delenv(name, raising=False)


def _db() -> FakeStoreDB:
    return FakeStoreDB({"users": {"u1": dict(USER)}, JOBS_PATH: _jobs()})


def _load(db, limit):
    _, pending = asyncio.run(load_profile_and_pending(db, "u1", limit))
    return [job.id for _, job in pending]


def _records(db) -> list[dict]:
    return list(db.data.get(SELECTIONS_PATH, {}).values())


# ------------------------------------------------------------------- off


@pytest.mark.parametrize("value", [None, "off", "OFF", "garbage"])
def test_off_reads_exactly_what_it_always_did(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("PRERANK_MODE", value)
    db = _db()

    ids = _load(db, 3)

    assert ids == ["a1", "a2", "a3"]
    assert db.queries == TODAY_QUERIES
    assert db.selects == []
    assert db.gets == ["users/u1"]
    assert db.writes == []


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_an_unlimited_load_is_unchanged_in_every_mode(monkeypatch, mode):
    """Batch ingest's content join: every pending job, the old way."""
    monkeypatch.setenv("PRERANK_MODE", mode)
    db = _db()

    ids = _load(db, None)

    assert ids == UNSCORED
    assert db.queries == TODAY_QUERIES
    assert db.selects == []
    assert db.gets == ["users/u1"]
    assert _records(db) == []


def test_off_returns_the_streamed_reference():
    db = _db()
    _, pending = asyncio.run(load_profile_and_pending(db, "u1", 2))
    assert [ref._path for ref, _ in pending] == [f"{JOBS_PATH}/a1", f"{JOBS_PATH}/a2"]


# ---------------------------------------------------------------- modes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "off"),
        ("", "off"),
        ("shadow", "shadow"),
        (" On ", "on"),
        ("enabled", "off"),
        ("1", "off"),
    ],
)
def test_prerank_mode(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("PRERANK_MODE", value)
    assert selection.prerank_mode() == expected


def test_an_unknown_mode_warns_once(monkeypatch):
    monkeypatch.setattr(selection, "_warned_modes", set())
    monkeypatch.setenv("PRERANK_MODE", "sideways")
    with structlog.testing.capture_logs() as logs:
        assert selection.prerank_mode() == "off"
        assert selection.prerank_mode() == "off"
    warned = [e for e in logs if e["event"] == "selection.mode_env_invalid"]
    assert len(warned) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 1 / 3),
        ("", 1 / 3),
        ("abc", 1 / 3),
        ("nan", 1 / 3),
        ("inf", 1 / 3),
        ("0", 0.0),
        ("0.25", 0.25),
        ("-0.5", 0.0),
        ("2", 1.0),
    ],
)
def test_explore_rate(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("SELECTION_EXPLORE_RATE", value)
    assert selection.explore_rate() == pytest.approx(expected)


# --------------------------------------------------------------- shadow


def test_shadow_picks_what_off_picks_and_logs_the_prerank_beside_it(monkeypatch):
    off_ids = _load(_db(), 3)
    monkeypatch.setenv("PRERANK_MODE", "shadow")
    db = _db()

    ids = _load(db, 3)

    assert ids == off_ids
    [record] = _records(db)
    assert record["mode"] == "shadow"
    assert record["explore_rate"] is None
    assert record["pool_size"] == len(UNSCORED)
    assert record["prerank_version"] == pr.PRERANK_VERSION
    assert [i["job_id"] for i in record["items"]] == off_ids
    assert {i["arm"] for i in record["items"]} == {"baseline"}
    assert [i["job_id"] for i in record["shadow_top"]] == TOP3
    scores = [i["prerank"] for i in record["shadow_top"]]
    assert scores == sorted(scores, reverse=True)


def test_shadow_streams_a_projection_with_no_order_by(monkeypatch):
    monkeypatch.setenv("PRERANK_MODE", "shadow")
    db = _db()

    _load(db, 3)

    assert db.selects == [(JOBS_PATH, tuple(selection.POOL_FIELDS))]
    assert [orders for _, _, orders, _ in db.queries] == [()]
    # The chosen docs are re-read in full, one get each.
    assert db.gets == ["users/u1"] + [f"{JOBS_PATH}/{i}" for i in ["a1", "a2", "a3"]]


# ------------------------------------------------------------------- on


def test_on_without_exploration_takes_the_top_by_sort_key(monkeypatch):
    monkeypatch.setenv("PRERANK_MODE", "on")
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "0")
    db = _db()

    ids = _load(db, 3)

    assert ids == TOP3
    [record] = _records(db)
    items = record["items"]
    # a2 and a4 tie on score: the newer discovered_at wins.
    assert items[0]["prerank"] == items[1]["prerank"]
    assert [i["arm"] for i in items] == ["rank"] * 3
    assert [i["rank_in_pool"] for i in items] == [1, 2, 3]
    assert record["explore_rate"] == 0.0
    assert "shadow_top" not in record
    assert set(items[0]) == {
        "job_id",
        "arm",
        "prerank",
        "features",
        "signals",
        "rank_in_pool",
    }
    assert items[0]["signals"] == {"location": "none"}


def test_on_returns_full_jobs_with_their_refs(monkeypatch):
    monkeypatch.setenv("PRERANK_MODE", "on")
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "0")
    _, pending = asyncio.run(load_profile_and_pending(_db(), "u1", 2))
    assert [ref._path for ref, _ in pending] == [f"{JOBS_PATH}/a4", f"{JOBS_PATH}/a2"]
    assert pending[0][1].jd_raw == "jd"


def _select(db, limit, mode, rng):
    pairs = asyncio.run(
        selection.select_pending(db, "u1", USER, limit, mode=mode, rng=rng)
    )
    return [job.id for _, job in pairs]


def test_full_exploration_is_seeded_and_uniform(monkeypatch):
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "1")

    first = _select(db := _db(), 3, "on", random.Random(11))
    again = _select(_db(), 3, "on", random.Random(11))

    assert first == again
    assert first != TOP3
    assert len(set(first)) == 3 and set(first) <= set(UNSCORED)
    [record] = _records(db)
    assert [i["arm"] for i in record["items"]] == ["explore"] * 3
    assert record["explore_rate"] == 1.0


def test_a_limit_beyond_the_pool_takes_the_pool(monkeypatch):
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "0")
    assert _select(_db(), 50, "on", random.Random(0)) == [
        "a4",
        "a2",
        "a5",
        # The three weak jobs tie: newest first.
        "a6",
        "a3",
        "a1",
    ]


def test_an_empty_pool_logs_an_empty_pick(monkeypatch):
    db = FakeStoreDB({"users": {"u1": dict(USER)}})
    assert _select(db, 3, "on", random.Random(0)) == []
    [record] = _records(db)
    assert record["pool_size"] == 0 and record["items"] == []


def test_the_scorer_geo_flag_reaches_the_prerank(monkeypatch):
    seen = []
    real = pr.prerank

    def spy(*args, **kw):
        seen.append(kw["enforce_geo"])
        return real(*args, **kw)

    monkeypatch.setattr(selection, "prerank", spy)
    monkeypatch.setenv("GEO_GATE_ENFORCE", "1")
    _select(_db(), 1, "on", random.Random(0))
    assert seen and set(seen) == {True}


def test_a_doc_that_changed_after_the_projection_is_skipped(monkeypatch):
    """Vanished, scored, or decided between projection and fetch: skipped,
    so the pick is shorter than the limit."""
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "0")
    db = _db()
    real = selection.choose

    def racing(*args, **kw):
        picks = real(*args, **kw)
        jobs = db.data[JOBS_PATH]
        del jobs["a4"]
        jobs["a2"]["match"] = {"overall_score": 50}
        jobs["a5"]["user_decision"] = "rejected"
        return picks

    monkeypatch.setattr(selection, "choose", racing)

    assert _select(db, 4, "on", random.Random(0)) == ["a6"]
    [record] = _records(db)
    assert [i["job_id"] for i in record["items"]] == ["a6"]
    assert record["pool_size"] == len(UNSCORED)


# ------------------------------------------------------------------ log


def test_a_failed_log_write_never_fails_the_pick(monkeypatch):
    monkeypatch.setenv("PRERANK_MODE", "on")
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "0")
    real_set = _StoreDoc.set

    async def failing(self, data, merge=False):
        if self._coll == SELECTIONS_PATH:
            raise RuntimeError("firestore down")
        await real_set(self, data, merge)

    monkeypatch.setattr(_StoreDoc, "set", failing)
    db = _db()

    with structlog.testing.capture_logs() as logs:
        assert _load(db, 3) == TOP3

    assert _records(db) == []
    assert any(e["event"] == "selection.write_failed" for e in logs)


def test_the_record_carries_the_bound_run_id(monkeypatch):
    monkeypatch.setenv("PRERANK_MODE", "shadow")
    db = _db()
    with structlog.contextvars.bound_contextvars(run_id="run-test"):
        _load(db, 1)
    [record] = _records(db)
    assert record["run_id"] == "run-test"
    assert record["selected_at"]


def test_the_record_run_id_is_none_outside_a_run(monkeypatch):
    monkeypatch.setenv("PRERANK_MODE", "shadow")
    db = _db()
    _load(db, 1)
    assert _records(db)[0]["run_id"] is None


# ------------------------------------------------------- the scoring seam


def test_one_scoring_pass_writes_one_selection(monkeypatch):
    """``score_or_start_run`` tries a batch run first, then scores online.
    Under selection that must be one pick and one record, not two."""
    from tools.matching import batch_runs, score

    monkeypatch.setenv("PRERANK_MODE", "on")
    monkeypatch.setenv("SELECTION_EXPLORE_RATE", "0")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "3")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "3")
    db = _db()
    for module in (score, batch_runs):
        monkeypatch.setattr(module.firestore, "AsyncClient", lambda: db)
    matched = []

    async def no_cache(profile, ttl_seconds):
        return None

    async def fake_match(job, profile, cached_content=None):
        matched.append(job.id)
        raise RuntimeError("no LLM in unit tests")

    async def cached_parse(db, jd_raw):
        return ParsedJD(role_family="engineering", summary="Build.")

    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.jd_cache, "lookup", cached_parse)
    monkeypatch.setattr(score, "match_job", fake_match)

    counts = asyncio.run(batch_runs.score_or_start_run("u1"))

    assert sorted(matched) == sorted(TOP3)
    assert counts["failed"] == 3
    [record] = _records(db)
    assert [i["job_id"] for i in record["items"]] == TOP3
    assert db.selects == [(JOBS_PATH, tuple(selection.POOL_FIELDS))]
