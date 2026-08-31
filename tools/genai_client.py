# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""One Vertex ``genai.Client`` per event loop, instead of one per call.

Nine call sites used to write ``genai.Client(vertexai=True)`` inline — three in
``tools.matching.batch``, four in ``tools.matching.pipeline``, one each in
``tools.tailoring.objective`` and ``tools.profile.extract`` — so a backlog run
scoring thousands of jobs built thousands of clients. Each construction builds
*two* httpx clients (sync and async) with a freshly loaded SSL context, and
neither is ever closed, because nothing owns them. That is per-call CPU plus a
steadily growing pile of unclosed connection pools in a long-lived worker.

Sharing is safe here because nothing in this codebase depends on client
identity: Vertex caches and batch jobs are server-side resources addressed by
resource name (``caches.delete(name=...)``, ``batches.get(name=...)``), and
``reap_match_caches`` already takes whichever client it is handed.

**Why keyed on the running loop.** ``genai.Client.__init__`` eagerly builds an
``httpx.AsyncClient``, whose connection pool binds to the first event loop that
uses it; reusing it under a second loop fails at the socket. Every ``cli/``
entry point calls ``asyncio.run`` (a fresh loop), the API and worker run one
long-lived loop, and ``tools.profile.extract`` calls the *sync* half with no
loop at all. Keying on the running loop — ``None`` when there is none — gives
each of those exactly one client and never crosses them. The loop is held by a
strong reference so its identity cannot be recycled into a stale hit.

**The cache must be resettable, and the unit suite must reset it.** A memo that
outlives a test hands back a client built before that test's patch was
installed, so patching the constructor catches nothing — this project has
already lost 39 production documents to exactly that shape (see
``tests/unit/conftest.py::no_production_firestore``). Hence
:func:`reset_vertex_client`, called autouse by the suite.
"""

from __future__ import annotations

import asyncio

from google import genai

#: ``(loop_or_None, client)`` for the most recently used loop. One slot is
#: enough: no process here interleaves two live event loops.
_cached: tuple[object | None, genai.Client] | None = None


def _loop_key() -> object | None:
    """The running event loop, or ``None`` for a synchronous caller."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def vertex_client() -> genai.Client:
    """The Vertex ``genai.Client`` for the current event loop, built on demand."""
    global _cached
    key = _loop_key()
    if _cached is not None and _cached[0] is key:
        return _cached[1]
    client = genai.Client(vertexai=True)
    _cached = (key, client)
    return client


def reset_vertex_client() -> None:
    """Drop the cached client. For tests — see this module's docstring."""
    global _cached
    _cached = None
