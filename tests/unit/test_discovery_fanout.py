# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The board fan-out is bounded, and it fetches through one pooled client.

``all_active_companies()`` is ~198 slugs spread over a handful of hosts (72
Ashby, 64 Greenhouse, 60 Lever). The cycle used to hand all 198 to a bare
``asyncio.gather``, which opens every connection at once — the shape most
likely to earn the 429s the retry policy then has to absorb, and the reason
there was never a warm pool to reuse.
"""

from __future__ import annotations

import asyncio

from test_company_prefs import _FakeDB

import tools.discovery.pipeline as discovery
from tools.ats import _http


def _companies(n: int) -> list[tuple[str, str, str]]:
    return [("greenhouse", f"c{i}", "known") for i in range(n)]


def _pool(n: int):
    """Stand in for ``all_active_companies``, which now takes the user's
    exclusion overlay. These tests are about the fan-out, not the overlay, so
    they ignore it — ``test_company_prefs.py`` is where it is pinned."""
    return lambda exclusions=frozenset(): _companies(n)


def _no_exclusions() -> _FakeDB:
    """An empty overlay — the only state that exists until the write path lands."""
    return _FakeDB()


def _tracking_fetcher(state: dict):
    """A fetcher that records how many of its peers are in flight with it."""

    async def fetcher(slug: str, user_id: str):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        # Yield enough times that every already-started task gets to run; an
        # unbounded gather parks all 60 here before any of them finishes.
        for _ in range(3):
            await asyncio.sleep(0)
        state["live"] -= 1
        return []

    return fetcher


def test_fan_out_is_bounded(monkeypatch):
    state = {"live": 0, "peak": 0}
    monkeypatch.setattr(discovery, "all_active_companies", _pool(60))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _tracking_fetcher(state))

    asyncio.run(discovery.run_discovery("u1", concurrency=5, db=_no_exclusions()))

    assert state["peak"] == 5, f"expected 5 boards in flight, saw {state['peak']}"


def test_the_default_bound_is_applied(monkeypatch):
    """Not just when a test passes one in — the shipped default binds too."""
    state = {"live": 0, "peak": 0}
    monkeypatch.setattr(discovery, "all_active_companies", _pool(198))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _tracking_fetcher(state))

    asyncio.run(discovery.run_discovery("u1", db=_no_exclusions()))

    assert state["peak"] == discovery._FETCH_CONCURRENCY
    assert state["peak"] < 198


def test_every_board_is_still_fetched(monkeypatch):
    """Bounding concurrency must not drop work — all 198 still run."""
    fetched: list[str] = []

    async def fetcher(slug: str, user_id: str):
        fetched.append(slug)
        return []

    monkeypatch.setattr(discovery, "all_active_companies", _pool(198))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    summary = asyncio.run(discovery.run_discovery("u1", db=_no_exclusions()))

    assert sorted(fetched) == sorted(s for _, s, _ in _companies(198))
    assert len(summary["empty_boards"]) == 198


def test_a_failing_board_still_lands_in_failures(monkeypatch):
    """The semaphore sits inside the gather, so return_exceptions still applies."""

    async def fetcher(slug: str, user_id: str):
        if slug == "c1":
            raise RuntimeError("board exploded")
        return []

    monkeypatch.setattr(discovery, "all_active_companies", _pool(3))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    summary = asyncio.run(discovery.run_discovery("u1", db=_no_exclusions()))

    assert [f["slug"] for f in summary["failures"]] == ["c1"]
    assert len(summary["empty_boards"]) == 2


def test_fetchers_run_inside_the_shared_client_scope(monkeypatch):
    """Every board sees the same lent client — this is what pools connections."""
    seen: list[object] = []

    async def fetcher(slug: str, user_id: str):
        seen.append(_http._client.get())
        return []

    monkeypatch.setattr(discovery, "all_active_companies", _pool(12))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    asyncio.run(discovery.run_discovery("u1", db=_no_exclusions()))

    assert len(seen) == 12
    assert all(c is not None for c in seen), "no client was lent to the fetchers"
    assert len({id(c) for c in seen}) == 1, "the fan-out used more than one client"


def test_the_client_scope_closes_after_the_cycle(monkeypatch):
    monkeypatch.setattr(discovery, "all_active_companies", _pool(2))

    async def fetcher(slug: str, user_id: str):
        return []

    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    async def main():
        await discovery.run_discovery("u1", db=_no_exclusions())
        return _http._client.get()

    assert asyncio.run(main()) is None


# ---------------------------------------------------------------------------
# Finding and scoring are two verbs now
#
# Finding jobs is free. Scoring them is the money — a Vertex batch the moment
# the backlog clears BATCH_MIN_PENDING, which a fresh account always does. The
# manual button therefore runs the free verb by default and the paid one only
# with a consent token; every *unattended* trigger keeps scoring, because
# there is nobody there to make the second click and the auto-discovery toggle
# is the consent for those.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import api.routes.discovery as routes_discovery  # noqa: E402
from tools.matching import batch_runs  # noqa: E402


def _cycle_fakes(monkeypatch, *, jobs=60):
    """Everything ``run_discovery_cycle`` touches except the spend decision."""
    scoring: list[str] = []
    found = [SimpleNamespace(id=f"j{i}") for i in range(jobs)]

    async def fake_run_discovery(user_id):
        return {
            "jobs": found,
            "jobs_by_platform": {},
            "failures": [],
            "empty_boards": [],
            "boards_cached": 0,
            "boards_fetched": 1,
        }

    async def fake_persist_new_jobs(items):
        return len(items)

    async def fake_score_or_start_run(user_id):
        scoring.append("batch")
        return {"scored": 1, "discarded": 0, "failed": 0, "pending": jobs}

    async def fake_score_pending_jobs(user_id, **kw):
        scoring.append("online")
        return {"scored": 1, "discarded": 0, "failed": 0, "pending": jobs}

    async def noop_cost(*a, **kw):
        return None

    async def fake_prefs(user_id):
        return None

    monkeypatch.setattr(routes_discovery, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(routes_discovery, "load_job_preferences", fake_prefs)
    monkeypatch.setattr(routes_discovery, "prefilter_jobs", lambda j, p: (j, {}))
    monkeypatch.setattr(routes_discovery, "persist_new_jobs", fake_persist_new_jobs)
    monkeypatch.setattr(routes_discovery, "score_pending_jobs", fake_score_pending_jobs)
    monkeypatch.setattr(batch_runs, "score_or_start_run", fake_score_or_start_run)
    monkeypatch.setattr(routes_discovery, "persist_run_cost", noop_cost)

    async def fake_backlog(user_id):
        # The backlog the priced second click would cover. One query over
        # ``jobs`` in production; a fixed number here, deliberately unlike
        # ``jobs`` above so a branch that went back to reporting "jobs found"
        # instead of "jobs waiting" shows up as a wrong number, not a
        # coincidence.
        return 9219

    monkeypatch.setattr(routes_discovery, "_backlog", fake_backlog)
    monkeypatch.setattr(routes_discovery, "_extend_slot", lambda *a, **kw: True)
    monkeypatch.setattr(routes_discovery, "_release_slot", lambda *a, **kw: True)
    written: list[dict] = []
    monkeypatch.setattr(
        routes_discovery,
        "_user_ref",
        lambda uid: SimpleNamespace(
            get=lambda: SimpleNamespace(to_dict=lambda: {}),
            set=lambda doc, merge=False: written.append(doc),
        ),
    )
    return scoring, written


def test_a_manual_run_without_a_scoring_token_persists_jobs_and_scores_nothing(
    monkeypatch,
):
    """T7. The find-only leg has to actually *find* — a guard that works by
    doing nothing at all is not the fix."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    scoring, written = _cycle_fakes(monkeypatch, jobs=60)

    asyncio.run(
        routes_discovery.run_discovery_cycle("u1", trigger="manual", score=False)
    )

    assert scoring == [], "the unconfirmed manual run reached a scorer"
    metrics = written[0]["discovery_state"]["last_discovery"]
    assert metrics["new_jobs"] == 60  # the free half really ran
    assert metrics["scored"] == 0
    # **The backlog, not the jobs this run found.** It used to be ``new``,
    # so a find-only click on a 9,219-job backlog reported 60 — and the
    # Profile card, which labels this "waiting to be scored", said 60 while
    # the confirm sheet beside it offered to score 200. One number, one
    # meaning: tools.matching.score.count_unscored.
    assert metrics["unscored_backlog"] == 9219
    # And the zero above is "never asked", not "tried and failed".
    assert metrics["scored_leg"] is False


def test_the_cron_tick_still_scores(monkeypatch):
    """T8. Splitting the verbs on an unattended run would mean nothing is ever
    scored — there is no second click coming. The toggle is the consent.

    Driven from :func:`tick_user`, not from the cycle, and that matters: the
    scoring leg can be lost at either end. The tick could dispatch
    ``score=False``, or the cycle could refuse to score what it was handed.
    Only a test that runs the whole tick -> dispatch -> cycle path catches
    both. QUEUE_MODE is off so ``dispatch_cycle`` runs the cycle inline and
    this really is end to end.
    """
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    scoring, written = _cycle_fakes(monkeypatch, jobs=60)

    # A user who opted in, with no recorded run, so the loop is due. The
    # claim is a compare-and-swap against Firestore; granted here.
    monkeypatch.setattr(
        routes_discovery,
        "_user_ref",
        lambda uid: SimpleNamespace(
            get=lambda: SimpleNamespace(
                to_dict=lambda: {
                    "discovery_settings": {
                        "auto_discovery": True,
                        "liveness_sweep": False,
                    }
                }
            ),
            set=lambda doc, merge=False: written.append(doc),
        ),
    )
    monkeypatch.setattr(routes_discovery, "_claim_slot", lambda *a, **kw: True)

    asyncio.run(routes_discovery.tick_user("u1", force_check=True))

    assert scoring == ["online"], "the unattended loop stopped scoring"
    assert written[0]["discovery_state"]["last_discovery"]["scored_leg"] is True


def test_the_cycle_scores_whatever_trigger_asked_it_to(monkeypatch):
    """The cycle half of T8, isolated: handed ``score=True`` it spends, and
    the QUEUE_MODE branch still chooses the half-price batch path."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    scoring, written = _cycle_fakes(monkeypatch, jobs=60)

    asyncio.run(routes_discovery.run_discovery_cycle("u1", trigger="cron"))

    assert scoring == ["batch"]
    assert written[0]["discovery_state"]["last_discovery"]["scored_leg"] is True


def test_a_confirmed_manual_run_scores_in_one_go(monkeypatch):
    """Positive control for T7: the split is the token, not the trigger."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    scoring, _ = _cycle_fakes(monkeypatch, jobs=60)

    asyncio.run(
        routes_discovery.run_discovery_cycle("u1", trigger="manual", score=True)
    )

    assert scoring == ["batch"]


def test_the_find_only_task_goes_to_its_own_worker_route(monkeypatch):
    """The rollout-skew guard, at the enqueue end.

    A ``{"score": false}`` field on ``/tasks/discovery`` would be dropped by
    an old worker revision mid-rollout and the backlog scored anyway — the
    guard failing open, silently, on every deploy. A path an old worker does
    not serve 404s and Cloud Tasks retries instead.
    """
    enqueued: list[tuple] = []

    def fake_enqueue(queue, path, payload, *, task_id=None):
        enqueued.append((path, payload, task_id))
        return True

    monkeypatch.setattr(routes_discovery.queues, "enqueue", fake_enqueue)

    routes_discovery.enqueue_cycle("discovery", "u1", trigger="manual", score=False)
    routes_discovery.enqueue_cycle("discovery", "u1", trigger="manual", score=True)

    scan, scored = enqueued
    assert scan[0] == routes_discovery.SCAN_TASK_PATH == "/tasks/discovery/scan"
    assert scored[0] == "/tasks/discovery"
    # No ``score`` field anywhere in the payload: the *path* carries the verb,
    # so there is nothing for an old revision to ignore.
    assert "score" not in scan[1] and "score" not in scored[1]
    # Different ids, or the free click and the paid click in the same minute
    # would dedupe into each other.
    assert scan[2] != scored[2]
