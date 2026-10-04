# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""One Vertex ``genai.Client`` per event loop, instead of one per call.

Constructing a client inline per call built two httpx clients (sync and
async) with a freshly loaded SSL context each time, none of them ever closed,
so a backlog run left a growing pile of unclosed connection pools in a
long-lived worker. Sharing is safe because nothing here depends on client
identity — Vertex caches and batch jobs are addressed by resource name.

Keyed on the running loop because ``genai.Client.__init__`` eagerly builds an
``httpx.AsyncClient`` whose pool binds to the first event loop that uses it,
and reusing it under a second loop fails at the socket. ``cli/`` entry points
each call ``asyncio.run``, the API and worker run one long-lived loop, and
``tools.profile.extract`` calls the sync half with no loop at all. The loop is
held by a strong reference so its identity cannot be recycled into a stale
hit.

The cache must be resettable, and the unit suite resets it autouse via
:func:`reset_vertex_client`: a memo that outlives a test hands back a client
built before that test's patch, so patching the constructor catches nothing —
a shape that has already cost this project 39 production documents.
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
