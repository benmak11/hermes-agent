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

import tools.discovery.pipeline as discovery
from tools.ats import _http


def _companies(n: int) -> list[tuple[str, str, str]]:
    return [("greenhouse", f"c{i}", "known") for i in range(n)]


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
    monkeypatch.setattr(discovery, "all_active_companies", lambda: _companies(60))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _tracking_fetcher(state))

    asyncio.run(discovery.run_discovery("u1", concurrency=5))

    assert state["peak"] == 5, f"expected 5 boards in flight, saw {state['peak']}"


def test_the_default_bound_is_applied(monkeypatch):
    """Not just when a test passes one in — the shipped default binds too."""
    state = {"live": 0, "peak": 0}
    monkeypatch.setattr(discovery, "all_active_companies", lambda: _companies(198))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _tracking_fetcher(state))

    asyncio.run(discovery.run_discovery("u1"))

    assert state["peak"] == discovery._FETCH_CONCURRENCY
    assert state["peak"] < 198


def test_every_board_is_still_fetched(monkeypatch):
    """Bounding concurrency must not drop work — all 198 still run."""
    fetched: list[str] = []

    async def fetcher(slug: str, user_id: str):
        fetched.append(slug)
        return []

    monkeypatch.setattr(discovery, "all_active_companies", lambda: _companies(198))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    summary = asyncio.run(discovery.run_discovery("u1"))

    assert sorted(fetched) == sorted(s for _, s, _ in _companies(198))
    assert len(summary["empty_boards"]) == 198


def test_a_failing_board_still_lands_in_failures(monkeypatch):
    """The semaphore sits inside the gather, so return_exceptions still applies."""

    async def fetcher(slug: str, user_id: str):
        if slug == "c1":
            raise RuntimeError("board exploded")
        return []

    monkeypatch.setattr(discovery, "all_active_companies", lambda: _companies(3))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    summary = asyncio.run(discovery.run_discovery("u1"))

    assert [f["slug"] for f in summary["failures"]] == ["c1"]
    assert len(summary["empty_boards"]) == 2


def test_fetchers_run_inside_the_shared_client_scope(monkeypatch):
    """Every board sees the same lent client — this is what pools connections."""
    seen: list[object] = []

    async def fetcher(slug: str, user_id: str):
        seen.append(_http._client.get())
        return []

    monkeypatch.setattr(discovery, "all_active_companies", lambda: _companies(12))
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    asyncio.run(discovery.run_discovery("u1"))

    assert len(seen) == 12
    assert all(c is not None for c in seen), "no client was lent to the fetchers"
    assert len({id(c) for c in seen}) == 1, "the fan-out used more than one client"


def test_the_client_scope_closes_after_the_cycle(monkeypatch):
    monkeypatch.setattr(discovery, "all_active_companies", lambda: _companies(2))

    async def fetcher(slug: str, user_id: str):
        return []

    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    async def main():
        await discovery.run_discovery("u1")
        return _http._client.get()

    assert asyncio.run(main()) is None
