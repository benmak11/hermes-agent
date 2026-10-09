# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Board health detection: each fresh board fetch's outcome is recorded, and
folded into one shared ``board_health/{platform}:{slug}`` record per board.

A board that 404s, rate-limits or times out used to arrive in the discovery
summary as an empty board. These tests drive the real fetchers over patched
HTTP and the real pipeline over :class:`FakeStoreDB`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from firestore_fakes import FakeStoreDB

import tools.discovery.pipeline as discovery
from models.job import Job
from tools.ats import _http, board_cache, board_health

DAY1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

#: Captured once, so a second cycle's patch replaces the first rather than
#: chaining onto it.
_REAL_CLIENT_INIT = httpx.AsyncClient.__init__

_GH_JOB = {
    "id": 1,
    "title": "Engineer",
    "absolute_url": "https://example.com/jobs/1",
    "location": {"name": "Remote"},
    "content": "<p>Build things</p>",
}


@pytest.fixture(autouse=True)
def quiet_world(monkeypatch):
    """Instant retries, the board cache off, and no real HTTP client."""
    monkeypatch.setattr(_http, "_RETRY_INITIAL_WAIT", 0)
    monkeypatch.setattr(_http, "_RETRY_MAX_WAIT", 0)
    monkeypatch.delenv("BOARD_CACHE_TTL_SECONDS", raising=False)


def _answers(monkeypatch, answers: dict[str, object]) -> None:
    """Route every board request by slug: a status code, JSON body, or exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        for slug, answer in answers.items():
            if slug in parts:
                if isinstance(answer, Exception):
                    raise answer
                if isinstance(answer, int):
                    return httpx.Response(answer)
                return httpx.Response(200, json=answer)
        raise AssertionError(f"unrouted request {request.url}")

    transport = httpx.MockTransport(handler)

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


def _discover(monkeypatch, boards, answers, *, db=None, now=DAY1):
    """One discovery cycle for ``u1`` over ``boards`` at ``now``."""
    _answers(monkeypatch, answers)
    monkeypatch.setattr(
        discovery,
        "all_active_companies",
        lambda exclusions=frozenset(): [(p, s, "known") for p, s in boards],
    )
    monkeypatch.setattr(board_health, "_now", lambda: now)
    db = db if db is not None else FakeStoreDB()
    summary = asyncio.run(discovery.run_discovery("u1", db=db))
    return summary, db


def _health(db) -> dict:
    return db.data.get(board_health.COLLECTION, {})


def _health_writes(db) -> list:
    return [w for w in db.writes if w[0].startswith(f"{board_health.COLLECTION}/")]


# ------------------------------------------------------------ the summary


def test_a_404_board_is_not_an_empty_board(monkeypatch):
    summary, _ = _discover(
        monkeypatch,
        [("greenhouse", "acme"), ("greenhouse", "globex")],
        {"acme": {"jobs": []}, "globex": 404},
    )
    assert summary["empty_boards"] == [
        {"platform": "greenhouse", "slug": "acme", "source": "known"}
    ]
    assert summary["boards_not_found"] == 1
    assert summary["boards_failing"] == 0


def test_summary_counts_every_outcome(monkeypatch):
    summary, _ = _discover(
        monkeypatch,
        [
            ("greenhouse", "acme"),
            ("greenhouse", "globex"),
            ("lever", "Initech"),
            ("ashby", "umbrella"),
            ("ashby", "hooli"),
        ],
        {
            "acme": {"jobs": [_GH_JOB]},
            "globex": 404,
            "Initech": 429,
            "umbrella": 503,
            "hooli": httpx.ReadTimeout("slow"),
        },
    )
    assert summary["board_outcomes"] == {
        "ok": 1,
        "not_found": 1,
        "rate_limited": 1,
        "server_error": 1,
        "timeout": 1,
    }
    assert (summary["boards_not_found"], summary["boards_failing"]) == (1, 3)
    assert summary["empty_boards"] == []
    assert len(summary["jobs"]) == 1


def test_the_complete_line_carries_the_counts(monkeypatch):
    with structlog.testing.capture_logs() as logs:
        _discover(
            monkeypatch,
            [("greenhouse", "acme"), ("lever", "globex")],
            {"acme": 404, "globex": 503},
        )
    (line,) = [e for e in logs if e["event"] == "discovery.complete"]
    assert line["boards_not_found"] == 1
    assert line["boards_failing"] == 1
    assert sorted(line["unhealthy_boards"]) == [
        "greenhouse/acme:not_found",
        "lever/globex:server_error",
    ]


def test_each_concurrent_board_gets_its_own_outcome(monkeypatch):
    """The slot is opened inside the per-board task; one opened before the
    gather would be shared, and every board would report the last answer."""
    boards = [("greenhouse", f"b{i}") for i in range(30)]
    answers: dict[str, object] = {
        s: (404 if i % 2 else {"jobs": []}) for i, (_, s) in enumerate(boards)
    }
    summary, db = _discover(monkeypatch, boards, answers)
    assert summary["board_outcomes"] == {"ok": 15, "not_found": 15}
    for i, (_, slug) in enumerate(boards):
        expected = "not_found" if i % 2 else "ok"
        assert _health(db)[f"greenhouse:{slug}"]["last_outcome"] == expected


def test_a_fetcher_exception_records_error(monkeypatch):
    async def boom(slug, user_id):
        raise ValueError("bad payload")

    monkeypatch.setitem(discovery.FETCHERS, "lever", boom)
    summary, db = _discover(monkeypatch, [("lever", "acme")], {})
    assert [f["slug"] for f in summary["failures"]] == ["acme"]
    assert summary["boards_failing"] == 1
    record = _health(db)["lever:acme"]
    assert (record["state"], record["last_outcome"]) == ("failing", "error")


# ---------------------------------------------------- what is not recorded


def test_a_cache_hit_records_nothing(monkeypatch):
    cached = [
        Job(
            id="j1",
            user_id="u1",
            source="greenhouse",
            source_id="1",
            company="acme",
            title="Engineer",
            url="https://example.com/jobs/1",
            jd_raw="Build things",
            discovered_at=DAY1,
        )
    ]

    async def load(platform, slug, user_id):
        return cached

    monkeypatch.setattr(board_cache, "load_jobs", load)
    summary, db = _discover(monkeypatch, [("greenhouse", "acme")], {})
    assert summary["boards_cached"] == 1
    assert summary["board_outcomes"] == {}
    assert _health(db) == {}
    assert _health_writes(db) == []


@pytest.mark.parametrize("platform", ["google_jobs", "meta_jobs"])
def test_search_query_sources_record_nothing(monkeypatch, platform):
    """Their "slugs" are search queries, not boards. Even a fetcher that goes
    through ``fetch_board_json`` must not produce a record."""

    async def fetcher(slug, user_id):
        await _http.fetch_board_json(platform, slug, "https://example.com/x/q")
        return []

    monkeypatch.setitem(discovery.FETCHERS, platform, fetcher)
    summary, db = _discover(monkeypatch, [(platform, "software engineer")], {"x": 404})
    assert summary["board_outcomes"] == {}
    assert summary["boards_not_found"] == 0
    assert db.data.get(board_health.COLLECTION) in (None, {})


def test_record_outcomes_ignores_untracked_platforms():
    db = FakeStoreDB()
    counts = asyncio.run(
        board_health.record_outcomes(
            db, [("google_jobs", "q", "not_found", 404)], now=DAY1
        )
    )
    assert counts["written"] == 0
    assert db.queries == [], "read the collection for nothing"


# --------------------------------------------------------- state machine


def test_first_sight_ok_is_written_once_then_never_again(monkeypatch):
    boards, answers = [("greenhouse", "acme")], {"acme": {"jobs": [_GH_JOB]}}
    _, db = _discover(monkeypatch, boards, answers)
    assert _health(db)["greenhouse:acme"] == {
        "platform": "greenhouse",
        "slug": "acme",
        "state": "ok",
        "last_outcome": "ok",
        "last_status": 200,
        "last_ok_at": DAY1.isoformat(),
        "failing_since": None,
        "not_found_days": 0,
        "last_not_found_day": None,
        "updated_at": DAY1.isoformat(),
    }
    assert len(_health_writes(db)) == 1

    _discover(monkeypatch, boards, answers, db=db, now=DAY1 + timedelta(days=3))
    assert len(_health_writes(db)) == 1, "an unchanged ok board was rewritten"


def test_slug_case_is_kept(monkeypatch):
    _, db = _discover(monkeypatch, [("lever", "AcmeCo")], {"AcmeCo": []})
    assert list(_health(db)) == ["lever:AcmeCo"]
    assert _health(db)["lever:AcmeCo"]["slug"] == "AcmeCo"


def test_ok_failing_ok_transitions(monkeypatch):
    boards = [("greenhouse", "acme")]
    _, db = _discover(monkeypatch, boards, {"acme": {"jobs": []}})
    later = DAY1 + timedelta(hours=6)
    _discover(monkeypatch, boards, {"acme": 404}, db=db, now=later)
    record = _health(db)["greenhouse:acme"]
    assert record["state"] == "failing"
    assert record["failing_since"] == later.isoformat()
    assert record["last_ok_at"] == DAY1.isoformat()
    assert (record["last_outcome"], record["last_status"]) == ("not_found", 404)
    assert record["not_found_days"] == 1

    back = DAY1 + timedelta(days=2)
    _discover(monkeypatch, boards, {"acme": {"jobs": []}}, db=db, now=back)
    record = _health(db)["greenhouse:acme"]
    assert record["state"] == "ok"
    assert record["last_ok_at"] == back.isoformat()
    assert record["failing_since"] is None
    assert record["not_found_days"] == 0
    assert record["last_not_found_day"] is None


def test_not_found_days_counts_distinct_utc_days(monkeypatch):
    boards, answers = [("ashby", "acme")], {"acme": 404}
    _, db = _discover(monkeypatch, boards, answers)
    _discover(monkeypatch, boards, answers, db=db, now=DAY1 + timedelta(hours=10))
    record = _health(db)["ashby:acme"]
    assert record["not_found_days"] == 1
    assert record["last_not_found_day"] == "2026-10-01"
    assert len(_health_writes(db)) == 1, "a same-day repeat 404 was rewritten"

    _discover(monkeypatch, boards, answers, db=db, now=DAY1 + timedelta(days=1))
    record = _health(db)["ashby:acme"]
    assert record["not_found_days"] == 2
    assert record["last_not_found_day"] == "2026-10-02"
    assert record["failing_since"] == DAY1.isoformat()


def test_the_utc_day_is_used_not_the_local_one():
    """23:30 at UTC-5 is the next UTC day."""
    from datetime import timezone

    db = FakeStoreDB()
    obs = [("greenhouse", "acme", "not_found", 404)]
    asyncio.run(board_health.record_outcomes(db, obs, now=DAY1))
    local = datetime(2026, 10, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    asyncio.run(board_health.record_outcomes(db, obs, now=local))
    record = _health(db)["greenhouse:acme"]
    assert record["not_found_days"] == 2
    assert record["updated_at"] == "2026-10-02T04:30:00+00:00"


def test_next_record_stamps_utc_whatever_zone_it_is_handed():
    from datetime import timezone

    local = datetime(2026, 10, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    record = board_health.next_record(
        None, "greenhouse", "acme", "not_found", 404, local
    )
    assert record["last_not_found_day"] == "2026-10-02"
    assert record["failing_since"] == "2026-10-02T04:30:00+00:00"


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (429, "rate_limited"),
        (503, "server_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("no route"), "error"),
    ],
)
def test_other_failures_never_count_not_found_days(monkeypatch, answer, outcome):
    boards = [("lever", "acme")]
    _, db = _discover(monkeypatch, boards, {"acme": 404})
    _discover(
        monkeypatch, boards, {"acme": answer}, db=db, now=DAY1 + timedelta(days=1)
    )
    record = _health(db)["lever:acme"]
    assert (record["state"], record["last_outcome"]) == ("failing", outcome)
    assert record["not_found_days"] == 1
    assert record["last_not_found_day"] == "2026-10-01"


def test_a_failing_board_seen_first_has_no_not_found_days_unless_404(monkeypatch):
    _, db = _discover(monkeypatch, [("lever", "acme")], {"acme": 429})
    record = _health(db)["lever:acme"]
    assert record["state"] == "failing"
    assert record["not_found_days"] == 0
    assert record["last_not_found_day"] is None
    assert record["last_ok_at"] is None


def test_repeated_identical_failures_are_not_rewritten(monkeypatch):
    boards, answers = [("greenhouse", "acme")], {"acme": 503}
    _, db = _discover(monkeypatch, boards, answers)
    _discover(monkeypatch, boards, answers, db=db, now=DAY1 + timedelta(days=4))
    assert len(_health_writes(db)) == 1


def test_writes_are_absolute_values():
    """Two users' cycles on the same day must land on the same document."""
    db = FakeStoreDB()
    obs = [("greenhouse", "acme", "not_found", 404)]
    asyncio.run(board_health.record_outcomes(db, obs, now=DAY1))
    asyncio.run(board_health.record_outcomes(db, obs, now=DAY1))
    (write,) = _health_writes(db)
    _, data, merge = write
    assert merge is False
    assert data["not_found_days"] == 1
    assert all(type(v) in (str, int, type(None)) for v in data.values())


def test_reads_the_collection_once_per_cycle(monkeypatch):
    boards = [("greenhouse", "acme"), ("lever", "globex"), ("ashby", "initech")]
    _, db = _discover(
        monkeypatch, boards, {"acme": 404, "globex": [], "initech": {"jobs": []}}
    )
    reads = [q for q in db.queries if q[0] == board_health.COLLECTION]
    assert len(reads) == 1
    assert not [g for g in db.gets if g.startswith(board_health.COLLECTION)]


# ---------------------------------------------------------------- logging


def test_a_state_change_warns_but_first_sight_does_not(monkeypatch):
    boards = [("greenhouse", "acme")]
    with structlog.testing.capture_logs() as first:
        _, db = _discover(monkeypatch, boards, {"acme": {"jobs": []}})
    assert not [e for e in first if e["event"] == "board_health.changed"]
    (seen,) = [e for e in first if e["event"] == "board_health.first_seen"]
    assert seen["log_level"] == "info"

    with structlog.testing.capture_logs() as second:
        _discover(monkeypatch, boards, {"acme": 404}, db=db, now=DAY1)
    (changed,) = [e for e in second if e["event"] == "board_health.changed"]
    assert changed["log_level"] == "warning"
    assert changed["platform"] == "greenhouse"
    assert changed["slug"] == "acme"
    assert (changed["from_state"], changed["to_state"]) == ("ok", "failing")
    assert (changed["outcome"], changed["status"]) == ("not_found", 404)


def test_a_failing_board_staying_failing_does_not_warn(monkeypatch):
    boards = [("greenhouse", "acme")]
    _, db = _discover(monkeypatch, boards, {"acme": 429})
    with structlog.testing.capture_logs() as logs:
        _discover(monkeypatch, boards, {"acme": 404}, db=db, now=DAY1)
    assert not [e for e in logs if e["event"] == "board_health.changed"]


# ------------------------------------------------- never fails a search


class _BrokenHealthDB(FakeStoreDB):
    """Exclusions read fine; ``board_health`` fails to read or to write."""

    def __init__(self, *, fail_read=False, fail_write=False):
        super().__init__()
        self.fail_read = fail_read
        self.fail_write = fail_write

    def collection(self, name):
        coll = super().collection(name)
        if name != board_health.COLLECTION:
            return coll
        db = self

        class _Coll:
            def stream(self):
                if db.fail_read:
                    raise RuntimeError("firestore down")
                return coll.stream()

            def document(self, doc_id):
                doc = coll.document(doc_id)
                if db.fail_write:

                    async def _fail(data, merge=False):
                        raise RuntimeError("write refused")

                    doc.set = _fail  # type: ignore[method-assign]
                return doc

        return _Coll()


@pytest.mark.parametrize("which", ["fail_read", "fail_write"])
def test_a_health_failure_never_fails_the_search(monkeypatch, which):
    db = _BrokenHealthDB(**{which: True})
    with structlog.testing.capture_logs() as logs:
        summary, _ = _discover(
            monkeypatch,
            [("greenhouse", "acme"), ("greenhouse", "globex")],
            {"acme": {"jobs": [_GH_JOB]}, "globex": 404},
            db=db,
        )
    assert len(summary["jobs"]) == 1
    assert summary["boards_not_found"] == 1
    assert _health(db) == {}
    event = (
        "board_health.read_failed"
        if which == "fail_read"
        else ("board_health.write_failed")
    )
    (line,) = [e for e in logs if e["event"] == event]
    assert line["log_level"] == "warning"


def test_an_unexpected_health_error_is_still_contained(monkeypatch):
    async def explode(db, observations, now=None):
        raise RuntimeError("bug")

    monkeypatch.setattr(board_health, "record_outcomes", explode)
    summary, _ = _discover(
        monkeypatch, [("greenhouse", "acme")], {"acme": {"jobs": [_GH_JOB]}}
    )
    assert len(summary["jobs"]) == 1


# ------------------------------------------------------------ no user data


def test_records_hold_no_user_fields(monkeypatch):
    _, db = _discover(monkeypatch, [("greenhouse", "acme")], {"acme": 404})
    record = _health(db)["greenhouse:acme"]
    assert "u1" not in repr(record)
    assert set(record) == {
        "platform",
        "slug",
        "state",
        "last_outcome",
        "last_status",
        "last_ok_at",
        "failing_since",
        "not_found_days",
        "last_not_found_day",
        "updated_at",
    }
