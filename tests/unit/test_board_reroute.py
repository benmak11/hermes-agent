# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Board health beyond ok/failing: probing a 404ing slug on the other
platforms, then ``moved`` / ``quarantined`` / ``dead``, compose-time skip and
reroute, and the ``BOARD_REROUTE_AUTO`` shadow.

Board fetches go through the real fetchers over an ``httpx.MockTransport``;
``probe_board`` is replaced by a table lookup. No real HTTP.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from firestore_fakes import FakeStoreDB

import tools.discovery.pipeline as discovery
from tools.ats import _http, board_health
from tools.ats.board_health import names_match, normalize_name
from tools.ats.probe import BoardProbe

DAY1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

_REAL_CLIENT_INIT = httpx.AsyncClient.__init__

_HOSTS = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
}

_GH_JOB = {
    "id": 1,
    "title": "Engineer",
    "absolute_url": "https://example.com/jobs/1",
    "location": {"name": "Remote"},
    "content": "<p>Build things</p>",
}
_LEVER_JOB = {
    "id": "l1",
    "text": "Engineer",
    "hostedUrl": "https://example.com/l1",
    "categories": {"location": "Remote"},
    "descriptionPlain": "Build things",
}
_WORKABLE_BOARD = {
    "name": "Acme",
    "jobs": [
        {
            "shortcode": "AB12CD34EF",
            "title": "Engineer",
            "url": "https://apply.workable.com/j/AB12CD34EF",
            "city": "",
            "state": "",
            "country": "United States",
            "telecommuting": True,
            "description": "<p>Build things</p>",
        }
    ],
}

NOT_FOUND = BoardProbe("not_found", 404, None, None)


def jobs(n: int, name: str | None) -> BoardProbe:
    return BoardProbe("ok", 200, n, name)


@pytest.fixture(autouse=True)
def quiet_world(monkeypatch):
    monkeypatch.setattr(_http, "_RETRY_INITIAL_WAIT", 0)
    monkeypatch.setattr(_http, "_RETRY_MAX_WAIT", 0)
    monkeypatch.delenv("BOARD_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv(board_health.REROUTE_FLAG, raising=False)
    monkeypatch.setattr(discovery, "load_blocklist", lambda: set())


class World:
    """Board answers, probe answers and YAML names for a run of cycles."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.answers: dict[tuple[str, str], object] = {}
        self.probes: dict[tuple[str, str], object] = {}
        self.names: dict[tuple[str, str], str] = {}
        self.fetched: list[tuple[str, str]] = []
        self.probed: list[tuple[str, str]] = []
        self.db = FakeStoreDB()
        transport = httpx.MockTransport(self._handle)

        def patched(client, *args, **kwargs):
            kwargs["transport"] = transport
            _REAL_CLIENT_INIT(client, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

        async def probe(platform, slug):
            self.probed.append((platform, slug))
            answer = self.probes.get((platform, slug), NOT_FOUND)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(board_health, "probe_board", probe)
        monkeypatch.setattr(discovery, "entry_names", lambda: dict(self.names))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        platform = _HOSTS[request.url.host]
        parts = [p for p in request.url.path.split("/") if p]
        slug = parts[-2] if platform == "greenhouse" else parts[-1]
        self.fetched.append((platform, slug))
        answer = self.answers.get((platform, slug), 404)
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, json=answer)

    def cycle(self, boards, now, *, exclusions=frozenset()):
        self.fetched.clear()
        self.probed.clear()
        self.mp.setattr(
            discovery,
            "all_active_companies",
            lambda exclusions=frozenset(): [(p, s, "known") for p, s in boards],
        )
        self.mp.setattr(board_health, "_now", lambda: now)
        return asyncio.run(discovery.run_discovery("u1", db=self.db))

    def record(self, key: str) -> dict:
        return self.db.data[board_health.COLLECTION][key]

    def seed(self, key: str, record: dict) -> None:
        self.db.data.setdefault(board_health.COLLECTION, {})[key] = record


def _health_writes(db) -> list:
    return [w for w in db.writes if w[0].startswith(f"{board_health.COLLECTION}/")]


# --------------------------------------------------------------- names


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Acme", "Acme Inc."),
        ("Acme, Inc.", "ACME"),
        ("Acme Corp", "acme corporation"),
        ("Acme GmbH", "Acme"),
        ("Acme Co. Ltd", "Acme"),
        ("  Acme   Labs ", "acme labs"),
        ("Acme-Labs", "Acme Labs"),
        ("Acme\u2019s", "Acmes"),
        ("Globex Pty Ltd", "Globex"),
    ],
)
def test_names_that_match_after_normalising(a, b):
    assert names_match(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Super Technologies", "Super"),
        ("MaintainX (An Autodesk Company)", "MaintainX"),
        ("Acme Labs", "Acme"),
        ("Acme", "Globex"),
        ("Acme", None),
        (None, None),
        ("", ""),
        ("Inc", "Acme"),
    ],
)
def test_names_that_do_not_match(a, b):
    assert not names_match(a, b)


def test_normalize_keeps_every_non_suffix_word():
    assert normalize_name("MaintainX (An Autodesk Company)") == (
        "maintainx an autodesk company"
    )
    assert normalize_name("Initech, LLC") == "initech"
    assert normalize_name("Co") == "co", "a lone suffix is the whole name"
    assert normalize_name("!!!") is None


# --------------------------------------------------- pure transitions


def _failing(days: int, first: str, last: str, **over) -> dict:
    rec = {
        "platform": "greenhouse",
        "slug": "acme",
        "state": "failing",
        "last_outcome": "not_found",
        "last_status": 404,
        "last_ok_at": None,
        "failing_since": f"{first}T09:00:00+00:00",
        "not_found_days": days,
        "last_not_found_day": last,
        "first_not_found_day": first,
    }
    rec.update(over)
    return rec


def test_the_first_404_day_is_the_span_anchor_not_failing_since():
    """A board that rate-limited for a week before its first 404 spans from
    the 404, not from when it started failing."""
    prev = board_health.next_record(
        None, "greenhouse", "acme", "rate_limited", 429, DAY1
    )
    assert "first_not_found_day" not in prev
    later = DAY1 + timedelta(days=7)
    rec = board_health.next_record(prev, "greenhouse", "acme", "not_found", 404, later)
    assert rec["failing_since"] == DAY1.isoformat()
    assert rec["first_not_found_day"] == "2026-10-08"


def test_a_record_without_a_first_day_anchors_at_its_last_404_day():
    """Older records: the span starts late, so the board can only die late."""
    prev = _failing(2, "2026-10-01", "2026-10-02")
    del prev["first_not_found_day"]
    rec = board_health.next_record(
        prev, "greenhouse", "acme", "not_found", 404, DAY1 + timedelta(days=5)
    )
    assert rec["first_not_found_day"] == "2026-10-02"


@pytest.mark.parametrize(
    ("days", "probed", "today", "due"),
    [
        (1, None, DAY1, False),
        (2, None, DAY1, True),
        (2, "2026-10-01T03:00:00+00:00", DAY1, False),
        (3, "2026-09-30T03:00:00+00:00", DAY1, True),
    ],
)
def test_a_failing_board_is_probed_from_two_404_days(days, probed, today, due):
    rec = _failing(days, "2026-09-25", "2026-10-01", probed_at=probed)
    assert board_health.needs_probe(rec, today, observed=True, active=True) is due


def test_no_new_404_day_since_the_probe_means_no_probe():
    rec = _failing(2, "2026-09-29", "2026-09-30", probed_at="2026-09-30T10:00:00+00:00")
    assert not board_health.needs_probe(rec, DAY1, observed=True, active=True)


def test_an_inactive_board_is_never_probed():
    rec = _failing(4, "2026-09-25", "2026-10-01")
    assert not board_health.needs_probe(rec, DAY1, observed=False, active=False)


def test_the_probe_targets_the_other_platforms():
    rec = _failing(2, "2026-09-30", "2026-10-01")
    assert board_health.probe_targets(rec) == [
        ("lever", "acme"),
        ("ashby", "acme"),
        ("workable", "acme"),
    ]
    moved = {**rec, "state": "moved"}
    assert board_health.probe_targets(moved) == [("greenhouse", "acme")]


@pytest.mark.parametrize(
    ("days", "first", "state"),
    [
        (2, "2026-09-01", "failing"),  # long span, too few days
        (3, "2026-09-18", "failing"),  # 13-day span
        (3, "2026-09-17", "dead"),  # 14-day span
        (5, "2026-09-01", "dead"),
    ],
)
def test_no_candidate_is_dead_only_at_three_days_spanning_fourteen(days, first, state):
    rec = _failing(days, first, "2026-10-01")
    probes = {("lever", "acme"): NOT_FOUND, ("ashby", "acme"): NOT_FOUND}
    out = board_health.resolve(rec, "Acme", probes, DAY1)
    assert out["state"] == state
    assert out["probed_at"] == DAY1.isoformat()
    if state == "dead":
        assert out["next_check_at"] == (DAY1 + timedelta(days=14)).isoformat()
    else:
        assert "next_check_at" not in out


def test_an_empty_board_elsewhere_is_not_a_candidate():
    rec = _failing(2, "2026-09-30", "2026-10-01")
    probes = {("lever", "acme"): jobs(0, "Acme"), ("ashby", "acme"): NOT_FOUND}
    out = board_health.resolve(rec, "Acme", probes, DAY1)
    assert out["state"] == "failing"
    assert "candidates" not in out


def test_one_candidate_with_a_matching_name_is_moved():
    rec = _failing(2, "2026-09-30", "2026-10-01")
    probes = {("lever", "acme"): jobs(4, "Acme Inc"), ("ashby", "acme"): NOT_FOUND}
    out = board_health.resolve(rec, "Acme", probes, DAY1)
    assert out["state"] == "moved"
    assert out["resolves_to"] == {"platform": "lever", "slug": "acme"}
    assert out["reverify_at"] == (DAY1 + timedelta(days=30)).isoformat()
    assert out["candidates"] == [
        {
            "platform": "lever",
            "slug": "acme",
            "name": "Acme Inc",
            "job_count": 4,
            "probed_at": DAY1.isoformat(),
        }
    ]
    assert out["not_found_days"] == 2, "the 404 history is kept"


@pytest.mark.parametrize(
    ("entry_name", "probes"),
    [
        # names differ
        ("Acme", {("lever", "acme"): jobs(4, "Acme Labs")}),
        # parenthetical is a different name
        ("Acme", {("lever", "acme"): jobs(4, "Acme (A Globex Company)")}),
        # no recorded name
        (None, {("lever", "acme"): jobs(4, "Acme")}),
        # the candidate has no name
        ("Acme", {("lever", "acme"): jobs(4, None)}),
        # several candidates, even with matching names
        (
            "Acme",
            {("lever", "acme"): jobs(4, "Acme"), ("ashby", "acme"): jobs(2, "Acme")},
        ),
    ],
)
def test_anything_unproven_is_quarantined(entry_name, probes):
    rec = _failing(2, "2026-09-30", "2026-10-01")
    probes = {("lever", "acme"): NOT_FOUND, ("ashby", "acme"): NOT_FOUND, **probes}
    out = board_health.resolve(rec, entry_name, probes, DAY1)
    assert out["state"] == "quarantined"
    assert "resolves_to" not in out
    expected = [
        {"platform": p, "slug": s, "job_count": b.job_count, "name": b.name}
        for (p, s), b in probes.items()
        if b.outcome == "ok"
    ]
    assert [
        {k: c[k] for k in ("platform", "slug", "job_count", "name")}
        for c in out["candidates"]
    ] == expected


def test_an_inconclusive_round_only_stamps_the_probe():
    rec = _failing(2, "2026-09-30", "2026-10-01", probed_at=None)
    probes = {
        ("lever", "acme"): jobs(4, "Acme"),
        ("ashby", "acme"): BoardProbe("timeout", None, None, None),
    }
    out = board_health.resolve(rec, "Acme", probes, DAY1)
    assert out == {**rec, "probed_at": DAY1.isoformat()}


def _moved(**over) -> dict:
    rec = _failing(
        2,
        "2026-09-30",
        "2026-10-01",
        state="moved",
        resolves_to={"platform": "lever", "slug": "acme"},
        reverify_at=(DAY1 + timedelta(days=30)).isoformat(),
        probed_at=DAY1.isoformat(),
        candidates=[
            {
                "platform": "lever",
                "slug": "acme",
                "name": "Acme",
                "job_count": 4,
                "probed_at": DAY1.isoformat(),
            }
        ],
    )
    rec.update(over)
    return rec


def test_a_moved_board_is_reverified_at_thirty_days():
    rec = _moved()
    day29 = DAY1 + timedelta(days=29)
    day30 = DAY1 + timedelta(days=30)
    assert not board_health.needs_probe(rec, day29, observed=False, active=True)
    assert board_health.needs_probe(rec, day30, observed=False, active=True)

    still_gone = board_health.resolve(
        rec, "Acme", {("greenhouse", "acme"): NOT_FOUND}, day30
    )
    assert still_gone["state"] == "moved"
    assert still_gone["reverify_at"] == (day30 + timedelta(days=30)).isoformat()

    back = board_health.resolve(
        rec, "Acme", {("greenhouse", "acme"): jobs(3, "Acme")}, day30
    )
    assert back["state"] == "ok"
    assert back["last_ok_at"] == day30.isoformat()
    for gone in ("resolves_to", "reverify_at", "candidates", "probed_at"):
        assert gone not in back
    assert back["not_found_days"] == 0


def test_a_rerouted_target_failing_sends_the_board_back_to_failing():
    rec = _moved()
    assert board_health.next_rerouted(rec, "ok", 200, DAY1) == rec
    out = board_health.next_rerouted(rec, "not_found", 404, DAY1)
    assert out["state"] == "failing"
    assert (out["last_outcome"], out["last_status"]) == ("not_found", 404)
    for gone in ("resolves_to", "reverify_at", "candidates"):
        assert gone not in out
    assert out["failing_since"] == rec["failing_since"]


def test_a_moved_board_fetched_failing_keeps_its_reroute_and_counts():
    rec = _moved()
    out = board_health.next_record(
        rec, "greenhouse", "acme", "not_found", 404, DAY1 + timedelta(days=3)
    )
    assert out == rec


def test_a_dead_board_recheck_that_fails_pushes_the_next_check():
    rec = _failing(
        3, "2026-09-01", "2026-09-20", state="dead", next_check_at=DAY1.isoformat()
    )
    out = board_health.next_record(rec, "greenhouse", "acme", "not_found", 404, DAY1)
    assert out["state"] == "dead"
    assert out["next_check_at"] == (DAY1 + timedelta(days=14)).isoformat()
    assert out["not_found_days"] == 4


@pytest.mark.parametrize("state", ["dead", "quarantined", "moved", "failing"])
def test_any_ok_fetch_returns_a_board_to_ok(state):
    rec = _moved(state=state)
    out = board_health.next_record(rec, "greenhouse", "acme", "ok", 200, DAY1)
    assert out["state"] == "ok"
    assert set(out) == {
        "platform",
        "slug",
        "state",
        "last_outcome",
        "last_status",
        "last_ok_at",
        "failing_since",
        "not_found_days",
        "last_not_found_day",
    }


# ------------------------------------------------------ through discovery

ACME = ("greenhouse", "acme")
OTHER = ("greenhouse", "globex")


def test_probing_starts_on_the_second_404_day_and_finds_a_move(monkeypatch):
    w = World(monkeypatch)
    w.names[("greenhouse", "acme")] = "Acme"
    w.answers[OTHER] = {"jobs": [_GH_JOB]}
    w.probes[("lever", "acme")] = jobs(4, "Acme, Inc.")

    w.cycle([ACME, OTHER], DAY1)
    assert w.probed == [], "probed after one 404 day"
    assert w.record("greenhouse:acme")["state"] == "failing"

    with structlog.testing.capture_logs() as logs:
        summary = w.cycle([ACME, OTHER], DAY1 + timedelta(days=1))
    assert sorted(w.probed) == [
        ("ashby", "acme"),
        ("lever", "acme"),
        ("workable", "acme"),
    ]
    rec = w.record("greenhouse:acme")
    assert rec["state"] == "moved"
    assert rec["resolves_to"] == {"platform": "lever", "slug": "acme"}
    assert len(summary["jobs"]) == 1
    (changed,) = [e for e in logs if e["event"] == "board_health.changed"]
    assert changed["log_level"] == "warning"
    assert (changed["from_state"], changed["to_state"]) == ("failing", "moved")
    assert changed["resolves_to"] == {"platform": "lever", "slug": "acme"}


def test_at_most_one_probe_round_per_board_per_day(monkeypatch):
    w = World(monkeypatch)
    w.cycle([ACME], DAY1)
    w.cycle([ACME], DAY1 + timedelta(days=1))
    assert len(w.probed) == 3
    w.cycle([ACME], DAY1 + timedelta(days=1, hours=5))
    assert w.probed == []
    w.cycle([ACME], DAY1 + timedelta(days=2))
    assert len(w.probed) == 3, "a new 404 day earns a new round"


def test_a_failed_probe_leaves_the_record_and_completes_the_search(monkeypatch):
    w = World(monkeypatch)
    w.answers[OTHER] = {"jobs": [_GH_JOB]}
    w.cycle([ACME, OTHER], DAY1)
    w.probes[("lever", "acme")] = RuntimeError("probe bug")
    day2 = DAY1 + timedelta(days=1)
    with structlog.testing.capture_logs() as logs:
        summary = w.cycle([ACME, OTHER], day2)
    before = w.record("greenhouse:acme")
    assert len(summary["jobs"]) == 1
    assert before["state"] == "failing"
    assert "probed_at" not in before, "a failed round wrote a probe stamp"
    (failed,) = [e for e in logs if e["event"] == "board_health.probe_failed"]
    assert failed["log_level"] == "warning"
    assert (failed["platform"], failed["slug"]) == ACME
    writes = len(_health_writes(w.db))
    w.probes.pop(("lever", "acme"))
    w.cycle([ACME, OTHER], day2 + timedelta(hours=1))
    assert len(_health_writes(w.db)) > writes, "the next cycle retries"


def test_no_candidate_dies_after_fourteen_days_then_is_skipped_and_rechecked(
    monkeypatch,
):
    w = World(monkeypatch)
    w.answers[OTHER] = {"jobs": []}
    for offset in (0, 1, 13):
        w.cycle([ACME, OTHER], DAY1 + timedelta(days=offset))
        assert w.record("greenhouse:acme")["state"] == "failing"

    with structlog.testing.capture_logs() as logs:
        w.cycle([ACME, OTHER], DAY1 + timedelta(days=14))
    rec = w.record("greenhouse:acme")
    assert rec["state"] == "dead"
    assert rec["not_found_days"] == 4
    (changed,) = [e for e in logs if e["event"] == "board_health.changed"]
    assert (changed["to_state"], changed["log_level"]) == ("dead", "warning")
    due = DAY1 + timedelta(days=28)
    assert rec["next_check_at"] == due.isoformat()

    summary = w.cycle([ACME, OTHER], due - timedelta(minutes=1))
    assert ACME not in w.fetched
    assert summary["boards_skipped_dead"] == 1
    assert w.probed == []

    w.answers[ACME] = {"jobs": [_GH_JOB]}
    with structlog.testing.capture_logs() as logs:
        summary = w.cycle([ACME, OTHER], due)
    assert ACME in w.fetched
    assert summary["boards_skipped_dead"] == 0
    assert len(summary["jobs"]) == 1
    assert w.record("greenhouse:acme")["state"] == "ok"
    (changed,) = [e for e in logs if e["event"] == "board_health.changed"]
    assert (changed["from_state"], changed["to_state"]) == ("dead", "ok")


def test_a_dead_recheck_still_404_is_probed_and_stays_dead(monkeypatch):
    w = World(monkeypatch)
    w.seed(
        "greenhouse:acme",
        _failing(
            3, "2026-09-01", "2026-09-20", state="dead", next_check_at=DAY1.isoformat()
        ),
    )
    w.cycle([ACME], DAY1)
    assert ACME in w.fetched
    assert sorted(w.probed) == [
        ("ashby", "acme"),
        ("lever", "acme"),
        ("workable", "acme"),
    ]
    rec = w.record("greenhouse:acme")
    assert rec["state"] == "dead"
    assert rec["next_check_at"] == (DAY1 + timedelta(days=14)).isoformat()


def test_flag_off_fetches_the_original_and_logs_would_reroute(monkeypatch):
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    w.answers[("lever", "acme")] = [_LEVER_JOB]
    with structlog.testing.capture_logs() as logs:
        summary = w.cycle([ACME], DAY1 + timedelta(days=2))
    assert w.fetched == [ACME]
    (line,) = [e for e in logs if e["event"] == "board_health.would_reroute"]
    assert line["log_level"] == "info"
    assert (line["board"], line["target"]) == ("greenhouse:acme", "lever:acme")
    assert (summary["boards_would_reroute"], summary["boards_rerouted"]) == (1, 0)
    (complete,) = [e for e in logs if e["event"] == "discovery.complete"]
    assert complete["boards_would_reroute"] == 1
    assert w.record("greenhouse:acme")["state"] == "moved"


def test_a_board_that_moved_to_workable_is_found_and_rerouted(monkeypatch):
    """Workable is a probed platform: a 404ing board whose slug answers there
    under the same name moves, and the flag then fetches the Workable board."""
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    w.names[ACME] = "Acme"
    w.probes[("workable", "acme")] = jobs(1, "Acme")
    w.answers[("workable", "acme")] = _WORKABLE_BOARD
    w.cycle([ACME], DAY1)
    w.cycle([ACME], DAY1 + timedelta(days=1))
    assert ("workable", "acme") in w.probed
    rec = w.record("greenhouse:acme")
    assert rec["state"] == "moved"
    assert rec["resolves_to"] == {"platform": "workable", "slug": "acme"}

    summary = w.cycle([ACME], DAY1 + timedelta(days=2))
    assert w.fetched == [("workable", "acme")]
    assert [j.source for j in summary["jobs"]] == ["workable"]
    assert w.record("workable:acme")["state"] == "ok"


@pytest.mark.parametrize("value", ["1", "true", "on", "ON "])
def test_flag_on_fetches_the_target_instead(monkeypatch, value):
    monkeypatch.setenv(board_health.REROUTE_FLAG, value)
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    w.answers[("lever", "acme")] = [_LEVER_JOB]
    summary = w.cycle([ACME], DAY1 + timedelta(days=2))
    assert w.fetched == [("lever", "acme")]
    assert len(summary["jobs"]) == 1
    assert (summary["boards_rerouted"], summary["boards_would_reroute"]) == (1, 0)
    assert w.record("greenhouse:acme")["state"] == "moved"
    assert w.record("lever:acme")["state"] == "ok"


def test_flag_on_never_fetches_a_target_twice(monkeypatch):
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    w.seed("ashby:acme", _moved(platform="ashby"))
    w.answers[("lever", "acme")] = [_LEVER_JOB]
    summary = w.cycle([ACME, ("ashby", "acme"), ("lever", "ACME")], DAY1)
    assert w.fetched == [("lever", "ACME")]
    assert summary["boards_rerouted"] == 2


def test_flag_on_never_fetches_a_blocklisted_target(monkeypatch):
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    monkeypatch.setattr(discovery, "load_blocklist", lambda: {("lever", "Acme")})
    w.seed("greenhouse:acme", _moved())
    summary = w.cycle([ACME], DAY1)
    assert w.fetched == [ACME]
    assert (summary["boards_rerouted"], summary["boards_would_reroute"]) == (0, 1)


def test_a_rerouted_target_that_fails_sends_the_board_back(monkeypatch):
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    with structlog.testing.capture_logs() as logs:
        w.cycle([ACME], DAY1 + timedelta(days=2))
    assert w.fetched == [("lever", "acme")]
    rec = w.record("greenhouse:acme")
    assert rec["state"] == "failing"
    assert "resolves_to" not in rec
    changes = [e for e in logs if e["event"] == "board_health.changed"]
    assert [(e["slug"], e["from_state"], e["to_state"]) for e in changes] == [
        ("acme", "moved", "failing")
    ]
    assert changes[0]["log_level"] == "warning"

    w.cycle([ACME], DAY1 + timedelta(days=3))
    assert w.fetched == [ACME], "the original is fetched again"


def test_a_moved_board_whose_original_returns_goes_back_to_ok(monkeypatch):
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    w.answers[("lever", "acme")] = [_LEVER_JOB]
    w.cycle([ACME], DAY1 + timedelta(days=29))
    assert w.probed == []
    w.probes[ACME] = jobs(2, "Acme")
    w.cycle([ACME], DAY1 + timedelta(days=30))
    assert w.probed == [ACME]
    assert w.record("greenhouse:acme")["state"] == "ok"
    w.cycle([ACME], DAY1 + timedelta(days=31))
    assert w.fetched == [ACME]


def test_one_board_health_read_per_cycle_with_probes_and_reroutes(monkeypatch):
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    w.seed(
        "ashby:initech",
        _failing(1, "2026-09-30", "2026-09-30", platform="ashby", slug="initech"),
    )
    w.answers[("lever", "acme")] = [_LEVER_JOB]
    w.cycle([ACME, ("ashby", "initech")], DAY1)
    assert w.probed, "the probe round ran"
    reads = [q for q in w.db.queries if q[0] == board_health.COLLECTION]
    assert len(reads) == 1
    assert not [g for g in w.db.gets if g.startswith(board_health.COLLECTION)]


def test_a_quarantined_board_is_still_fetched_and_can_recover(monkeypatch):
    w = World(monkeypatch)
    w.seed(
        "greenhouse:acme",
        _failing(
            2,
            "2026-09-30",
            "2026-10-01",
            state="quarantined",
            probed_at=DAY1.isoformat(),
            candidates=[
                {"platform": "lever", "slug": "acme", "name": "X", "job_count": 1}
            ],
        ),
    )
    w.cycle([ACME], DAY1 + timedelta(hours=2))
    assert w.fetched == [ACME]
    assert w.probed == []
    assert w.record("greenhouse:acme")["state"] == "quarantined"

    w.answers[ACME] = {"jobs": []}
    with structlog.testing.capture_logs() as logs:
        w.cycle([ACME], DAY1 + timedelta(days=1))
    assert w.record("greenhouse:acme")["state"] == "ok"
    assert "candidates" not in w.record("greenhouse:acme")
    (changed,) = [e for e in logs if e["event"] == "board_health.changed"]
    assert (changed["from_state"], changed["to_state"], changed["log_level"]) == (
        "quarantined",
        "ok",
        "warning",
    )


def test_a_quarantine_names_its_candidates_in_the_warning(monkeypatch):
    w = World(monkeypatch)
    w.names[("greenhouse", "acme")] = "Acme"
    w.probes[("lever", "acme")] = jobs(4, "Acme Labs")
    w.cycle([ACME], DAY1)
    with structlog.testing.capture_logs() as logs:
        w.cycle([ACME], DAY1 + timedelta(days=1))
    (changed,) = [e for e in logs if e["event"] == "board_health.changed"]
    assert changed["to_state"] == "quarantined"
    assert changed["candidates"] == ["Acme Labs"]


def test_an_inconclusive_reverify_is_not_retried_the_same_day(monkeypatch):
    """Nothing else moves on after a timed-out re-verify: ``reverify_at`` is
    still due, so only the per-day limit stops a probe every cycle."""
    monkeypatch.setenv(board_health.REROUTE_FLAG, "1")
    w = World(monkeypatch)
    w.seed("greenhouse:acme", _moved())
    w.answers[("lever", "acme")] = [_LEVER_JOB]
    w.probes[ACME] = BoardProbe("timeout", None, None, None)
    day30 = DAY1 + timedelta(days=30)
    w.cycle([ACME], day30)
    assert w.probed == [ACME]
    rec = w.record("greenhouse:acme")
    assert (rec["state"], rec["probed_at"]) == ("moved", day30.isoformat())
    w.cycle([ACME], day30 + timedelta(hours=3))
    assert w.probed == []
    w.cycle([ACME], day30 + timedelta(days=1))
    assert w.probed == [ACME]
