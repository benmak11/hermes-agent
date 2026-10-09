# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared HTTP fetch for the ATS board APIs, with failure-visible logging.

The fetchers used to swallow every ``httpx.HTTPError`` and return ``[]``,
which made a rate-limited/broken board indistinguishable from an empty one in
the discovery summary. This helper keeps the "missing board is not an error"
contract (callers still get ``None`` → empty list) but logs *why* a board
returned nothing, so failure points show up in Cloud Logging.

Two efficiency properties live here, both of which only pay off across a whole
discovery cycle:

* **Connection pooling** — see :func:`board_client`. A caller that opens the
  scope lends one client to every fetch underneath it.
* **Bounded retry** on 429/5xx — see :data:`_RETRY_ATTEMPTS`. A 404 is *not*
  retried: discovery routinely probes stale unvetted slugs and "not on this
  platform" is a normal, fast answer, not a failure.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from obs.logging import get_logger

log = get_logger("tools.ats")

_TIMEOUT = 30

#: The client the current task should fetch through, when a caller has opened a
#: :func:`board_client` scope.
#:
#: A ContextVar, deliberately not a module-level memo. An
#: ``httpx.AsyncClient``'s pool binds to whichever loop first uses it and every
#: ``cli/`` entry point gets a fresh loop, a memo would never be closed, and —
#: a failure this project has shipped once — it would keep handing out a client
#: built before a test's patch, so patching the constructor catches nothing.
#: With a ContextVar nothing is cached between cycles, the scope closes the
#: client on exit, and a caller who opens no scope is unaffected.
_client: ContextVar[httpx.AsyncClient | None] = ContextVar(
    "ats_board_client", default=None
)


@asynccontextmanager
async def board_client() -> AsyncIterator[httpx.AsyncClient]:
    """Lend one pooled client to every :func:`fetch_board_json` call inside.

    Open this once around a fan-out; without it each fetch builds and tears
    down its own client and no connection is reused.

    The value is read from a ContextVar, which asyncio tasks inherit from the
    context active when they were *created*, so the scope must be entered
    before the ``gather``. Nesting is safe: the inner scope wins and the outer
    client is restored on exit.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = _client.set(client)
        try:
            yield client
        finally:
            _client.reset(token)


#: Fetch outcomes, as recorded by :func:`fetch_board_json`.
OK = "ok"
NOT_FOUND = "not_found"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
TIMEOUT = "timeout"
ERROR = "error"


@dataclass
class FetchOutcome:
    """What the last board fetch inside a :func:`capture_outcome` scope saw.

    ``outcome`` stays ``None`` when nothing went through
    :func:`fetch_board_json`; ``status`` is the HTTP status when there was one.
    """

    outcome: str | None = None
    status: int | None = None


_outcome: ContextVar[FetchOutcome | None] = ContextVar(
    "ats_fetch_outcome", default=None
)


@contextmanager
def capture_outcome() -> Iterator[FetchOutcome]:
    """Record the outcome of every :func:`fetch_board_json` call inside.

    Enter it inside the per-board coroutine, not around a ``gather``: tasks
    copy the context they were created in, so a slot opened before the
    fan-out would be one object shared by every board.
    """
    slot = FetchOutcome()
    token = _outcome.set(slot)
    try:
        yield slot
    finally:
        _outcome.reset(token)


def _record(outcome: str, status: int | None) -> None:
    slot = _outcome.get()
    if slot is not None:
        slot.outcome, slot.status = outcome, status


def classify_status(status: int) -> str:
    """The outcome for a non-2xx answer that survived the retries."""
    if status == 404:
        return NOT_FOUND
    if status == 429:
        return RATE_LIMITED
    if 500 <= status < 600:
        return SERVER_ERROR
    return ERROR


#: Bounded retry for 429/5xx. tenacity waits
#: ``min(initial * 2**(n-1) + jitter, max)`` per retry with jitter < 1, so three
#: attempts means two waits of at most 1.5s and 2.0s — roughly 3.5s for a board
#: that never recovers. At ``_FETCH_CONCURRENCY`` = 20 over 198 boards that is
#: ~35s even if *every* board were failing, well inside the 1800s dispatch
#: deadline. ``tests/unit/test_ats_http.py`` pins that budget.
#:
#: ``_RETRY_MAX_WAIT`` does not currently bind — the exponential only reaches
#: it on the fourth retry and there are two. It is a ceiling for whoever
#: raises ``_RETRY_ATTEMPTS``.
_RETRY_ATTEMPTS = 3
_RETRY_INITIAL_WAIT = 0.5
_RETRY_MAX_WAIT = 4.0


def _is_retryable(exc: BaseException) -> bool:
    """Only a 429 or a 5xx is worth asking again for.

    Everything else is either a normal answer (404: the slug is not on this
    platform) or something a retry cannot fix (403, malformed URL). Transport
    errors — timeout, DNS — are deliberately *not* retried either: at 30s a
    piece they are the one failure that could actually threaten the cycle
    deadline, and the board is already allowed to come back empty.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status = exc.response.status_code
    return status == 429 or 500 <= status < 600


async def fetch_board_json(platform: str, slug: str, url: str) -> Any | None:
    """GET a public board API and return the parsed JSON, or ``None`` on failure.

    - 404 → the slug is not on this platform (or the board was taken down);
      logged at info since discovery routinely probes stale unvetted slugs.
    - 429 / 5xx → retried up to :data:`_RETRY_ATTEMPTS` times with backoff;
      logged at warning only once the retries are spent.
    - any other HTTP status / transport error (timeout, DNS) → logged at
      warning: these are the real failure points to watch.

    Inside a :func:`capture_outcome` scope the outcome is also recorded there.
    """
    start = time.perf_counter()
    try:
        response = await _get_with_retry(url)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        _record(classify_status(status), status)
        level = log.info if status == 404 else log.warning
        level(
            "ats.fetch.failed",
            platform=platform,
            slug=slug,
            status=status,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return None
    except httpx.HTTPError as e:
        _record(TIMEOUT if isinstance(e, httpx.TimeoutException) else ERROR, None)
        log.warning(
            "ats.fetch.failed",
            platform=platform,
            slug=slug,
            error=f"{type(e).__name__}: {e}",
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return None

    data = response.json()
    _record(OK, response.status_code)
    log.debug(
        "ats.fetch.ok",
        platform=platform,
        slug=slug,
        duration_ms=round((time.perf_counter() - start) * 1000, 1),
    )
    return data


async def _get_with_retry(url: str) -> httpx.Response:
    """GET ``url``, retrying 429/5xx. Raises the last error once spent.

    ``reraise=True`` matters: the caller's ``except`` clauses above log the real
    ``httpx`` error, and tenacity would otherwise wrap it in a ``RetryError``
    that neither clause catches.
    """
    retrying = AsyncRetrying(
        stop=stop_after_attempt(_RETRY_ATTEMPTS),
        wait=wait_exponential_jitter(initial=_RETRY_INITIAL_WAIT, max=_RETRY_MAX_WAIT),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async for attempt in retrying:
        with attempt:
            return await _get(url)
    raise AssertionError("unreachable: AsyncRetrying either returns or raises")


async def _get(url: str) -> httpx.Response:
    """One GET through the scoped client, or a throwaway one if unscoped."""
    client = _client.get()
    if client is not None:
        response = await client.get(url)
        response.raise_for_status()
        return response
    async with httpx.AsyncClient(timeout=_TIMEOUT) as fresh:
        response = await fresh.get(url)
        response.raise_for_status()
        return response
