# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Request-context middleware: a correlation id + access log per request.

Binds ``request_id``, ``method`` and ``path`` into the structlog context before
the route runs, so every log line emitted while handling the request (including
``log.exception`` in a route, ``deps.verify_user`` binding ``user_id``, and the
tools it calls) is stitched together by ``request_id``. The same id is echoed
back in the ``X-Request-Id`` response header for client-side correlation.

The access-log lines below spell method/path/status into the message rather
than only into fields, so "GET /jobs/pending" is legible in a Cloud Logging
list view without expanding the payload; the same values still land as
separate fields for filtering.

On the worker (``WORKER_MODE``) only, the Cloud Tasks hop is correlated too:
``origin_request_id`` / ``origin_run_id`` name what enqueued the task, and
``task_name`` / ``retry_count`` / ``execution_count`` come from Cloud Tasks'
own headers. The worker still mints its own ``request_id``. These fields are
for logging only; nothing branches on them. Useful Cloud Logging filters::

    -- everything one click caused, one hop down
    jsonPayload.request_id="X" OR jsonPayload.origin_request_id="X"
    -- everything a run caused on the worker (chains: repeat with the new run_id)
    jsonPayload.run_id="R" OR jsonPayload.origin_run_id="R"
    -- every attempt of one task, and only the retries
    jsonPayload.task_name="T"
    jsonPayload.task_name="T" AND jsonPayload.retry_count>0
"""

from __future__ import annotations

import re
import time

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from obs.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    new_request_id,
    safe_correlation_id,
)
from tools.queues import ORIGIN_REQUEST_ID_HEADER, ORIGIN_RUN_ID_HEADER, worker_mode

# Health/SSE chatter we don't want an access-log line for on every poll.
_QUIET_PATHS = {"/health", "/healthz", "/readiness", "/liveness"}

# Cloud Tasks task ids are capped at 500 characters by Cloud Tasks itself.
_TASK_NAME_MAX_LEN = 500
_COUNT = re.compile(r"[0-9]{1,6}")


def _task_context(headers: Headers) -> dict[str, str | int]:
    """Origin and Cloud Tasks fields from a worker request's headers.

    Each is included only when present and well-formed; malformed values are
    dropped rather than logged.
    """
    fields: dict[str, str | int] = {}
    for header, key in (
        (ORIGIN_REQUEST_ID_HEADER, "origin_request_id"),
        (ORIGIN_RUN_ID_HEADER, "origin_run_id"),
    ):
        value = safe_correlation_id(headers.get(header))
        if value:
            fields[key] = value
    task_name = safe_correlation_id(
        headers.get("x-cloudtasks-taskname"), max_len=_TASK_NAME_MAX_LEN
    )
    if task_name:
        fields["task_name"] = task_name
    # Retry count includes every earlier attempt; execution count leaves out
    # 5xx failures. An unhandled exception here becomes a 500, so a gap between
    # them usually means this handler failed, not that the platform did.
    for header, key in (
        ("x-cloudtasks-taskretrycount", "retry_count"),
        ("x-cloudtasks-taskexecutioncount", "execution_count"),
    ):
        raw = headers.get(header)
        if raw is not None and _COUNT.fullmatch(raw):
            fields[key] = int(raw)
    return fields


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._log = get_logger("api.request")

    async def dispatch(self, request: Request, call_next) -> Response:
        clear_request_context()
        # Honor an inbound id (from the web client, or a load balancer) so trails
        # span hops. EventSource can't set headers, so SSE endpoints pass it as a
        # ?request_id= query param — accept either, header first.
        request_id = (
            request.headers.get("x-request-id")
            or request.query_params.get("request_id")
            or new_request_id()
        )
        bind_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        # Only the private worker reads these; on the public API they are
        # browser-settable and would only mislead.
        if worker_mode():
            bind_request_context(**_task_context(request.headers))

        quiet = request.url.path in _QUIET_PATHS
        start = time.perf_counter()
        if not quiet:
            self._log.info(f"{request.method} {request.url.path}")

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            # The unhandled error itself — the failure point we want traceable.
            self._log.exception(
                f"{request.method} {request.url.path} raised an unhandled exception",
                duration_ms=duration_ms,
            )
            # Answer with the correlation id instead of re-raising into a
            # bare 500: the UI surfaces "request <id>", which is the string to
            # paste into Cloud Logging (jsonPayload.request_id) to find the
            # traceback above. HTTPException never lands here.
            return JSONResponse(
                {"detail": "Internal server error", "request_id": request_id},
                status_code=500,
                headers={"X-Request-Id": request_id},
            )

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        if not quiet:
            self._log.info(
                f"{request.method} {request.url.path} {response.status_code} "
                f"({duration_ms}ms)",
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
        response.headers["X-Request-Id"] = request_id
        return response
