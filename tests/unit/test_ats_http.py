# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The two efficiency properties of the shared board fetch.

``fetch_board_json`` fronts 196 of the 198 boards a discovery cycle touches, so
both of these are per-cycle multipliers:

* one pooled client for a whole fan-out (:func:`tools.ats._http.board_client`),
  rather than one built and torn down per board;
* bounded retry on 429/5xx — and emphatically *not* on 404, which is the normal
  answer for the stale unvetted slugs discovery probes on every run.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tools.ats import _http

_URL = "https://example.com/board"

#: Captured at import, before ``instant_backoff`` can patch them away, so the
#: shipped values remain assertable.
_SHIPPED_ATTEMPTS = _http._RETRY_ATTEMPTS
_SHIPPED_INITIAL_WAIT = _http._RETRY_INITIAL_WAIT
_SHIPPED_MAX_WAIT = _http._RETRY_MAX_WAIT


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    """Keep the retry policy's shape, drop its wall-clock cost."""
    monkeypatch.setattr(_http, "_RETRY_INITIAL_WAIT", 0)
    monkeypatch.setattr(_http, "_RETRY_MAX_WAIT", 0)


def _count_clients(monkeypatch, transport: httpx.MockTransport) -> list[int]:
    """Force every AsyncClient onto ``transport`` and count constructions."""
    built = [0]
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        built[0] += 1
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return built


# --------------------------------------------------------- the pooled client


def test_scope_lends_one_client_to_every_fetch(monkeypatch):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": 1}))
    built = _count_clients(monkeypatch, transport)

    seen: list[httpx.AsyncClient | None] = []

    async def main():
        async with _http.board_client():
            for i in range(5):
                seen.append(_http._client.get())
                assert await _http.fetch_board_json("greenhouse", f"c{i}", _URL) == {
                    "ok": 1
                }

    asyncio.run(main())

    assert built[0] == 1, "the scope must build exactly one client for 5 fetches"
    assert len({id(c) for c in seen}) == 1


def test_without_a_scope_each_fetch_still_builds_its_own(monkeypatch):
    """The unscoped path is unchanged, so every existing caller is unaffected."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": 1}))
    built = _count_clients(monkeypatch, transport)

    async def main():
        for i in range(3):
            assert await _http.fetch_board_json("lever", f"c{i}", _URL) == {"ok": 1}

    asyncio.run(main())

    assert built[0] == 3
    assert _http._client.get() is None


def test_scope_is_released_even_when_a_fetch_raises(monkeypatch):
    """No client outlives its scope — the ContextVar is reset in a finally."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    _count_clients(monkeypatch, transport)

    async def main():
        with pytest.raises(ValueError):
            async with _http.board_client():
                assert _http._client.get() is not None
                raise ValueError("boom")
        return _http._client.get()

    assert asyncio.run(main()) is None


def test_concurrent_fetches_in_one_scope_share_the_client(monkeypatch):
    """Tasks created inside the scope inherit it — this is the gather case."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": 1}))
    built = _count_clients(monkeypatch, transport)

    async def main():
        async with _http.board_client():
            await asyncio.gather(
                *(_http.fetch_board_json("ashby", f"c{i}", _URL) for i in range(10))
            )

    asyncio.run(main())
    assert built[0] == 1


# ---------------------------------------------------------------- the retries


def _counting_transport(responses: list[httpx.Response]):
    """Answers with ``responses`` in order, repeating the last one forever."""
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls[0], len(responses) - 1)
        calls[0] += 1
        return responses[i]

    return httpx.MockTransport(handler), calls


def test_429_is_retried_and_then_succeeds(monkeypatch):
    transport, calls = _counting_transport(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"ok": 1})]
    )
    _count_clients(monkeypatch, transport)

    result = asyncio.run(_http.fetch_board_json("greenhouse", "acme", _URL))

    assert result == {"ok": 1}
    assert calls[0] == 3


def test_5xx_is_retried_then_gives_up_returning_none(monkeypatch):
    transport, calls = _counting_transport([httpx.Response(503)])
    _count_clients(monkeypatch, transport)

    assert asyncio.run(_http.fetch_board_json("lever", "acme", _URL)) is None
    assert calls[0] == _http._RETRY_ATTEMPTS


def test_404_is_never_retried(monkeypatch):
    """A slug that is not on this platform is a normal answer, and must stay fast.

    Discovery probes every unvetted slug against every platform on every cycle,
    so retrying 404s would multiply the cycle's most common response by three
    for no possible gain.
    """
    transport, calls = _counting_transport([httpx.Response(404)])
    _count_clients(monkeypatch, transport)

    assert asyncio.run(_http.fetch_board_json("ashby", "not-here", _URL)) is None
    assert calls[0] == 1


def test_403_is_never_retried(monkeypatch):
    """Nor is anything else a retry cannot fix."""
    transport, calls = _counting_transport([httpx.Response(403)])
    _count_clients(monkeypatch, transport)

    assert asyncio.run(_http.fetch_board_json("ashby", "acme", _URL)) is None
    assert calls[0] == 1


def test_transport_errors_are_not_retried(monkeypatch):
    """Timeouts are the one failure that could threaten the cycle deadline."""
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        raise httpx.ConnectTimeout("too slow")

    _count_clients(monkeypatch, httpx.MockTransport(handler))

    assert asyncio.run(_http.fetch_board_json("greenhouse", "acme", _URL)) is None
    assert calls[0] == 1


def test_the_retry_budget_stays_small():
    """Pins the *cost* of the policy, not just its shape.

    A retry loop inside a cycle with a 1800s dispatch deadline is only safe
    while it is small, and every one of these knobs can be raised without
    breaking a single behavioural test above — so the bound is asserted
    directly. tenacity waits ``min(initial * 2**(n-1) + jitter, max)`` per
    retry, with jitter < 1.

    If you are here because you raised one of these deliberately: recompute the
    worst case against the deadline and the fan-out width in
    ``tools.discovery.pipeline._FETCH_CONCURRENCY`` before changing the numbers.
    """
    assert _SHIPPED_ATTEMPTS == 3

    worst_case = sum(
        min(_SHIPPED_INITIAL_WAIT * 2 ** (n - 1) + 1.0, _SHIPPED_MAX_WAIT)
        for n in range(1, _SHIPPED_ATTEMPTS)
    )
    assert worst_case <= 4.0, f"a doomed board can now stall {worst_case}s"

    # 198 boards, _FETCH_CONCURRENCY at a time, every one of them failing.
    from tools.discovery.pipeline import _FETCH_CONCURRENCY

    all_boards_failing = (198 / _FETCH_CONCURRENCY) * worst_case
    assert all_boards_failing < 120, "retry backoff can now threaten the cycle"


def test_retries_reuse_the_scoped_client(monkeypatch):
    """A retry must not quietly reopen a connection the scope already owns."""
    transport, calls = _counting_transport(
        [httpx.Response(500), httpx.Response(200, json={"ok": 1})]
    )
    built = _count_clients(monkeypatch, transport)

    async def main():
        async with _http.board_client():
            return await _http.fetch_board_json("greenhouse", "acme", _URL)

    assert asyncio.run(main()) == {"ok": 1}
    assert calls[0] == 2
    assert built[0] == 1
