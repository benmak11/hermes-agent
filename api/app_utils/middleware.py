# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Request-context middleware: a correlation id + access log per request.

Binds ``request_id``, ``method`` and ``path`` into the structlog context before
the route runs, so every log line emitted while handling the request (including
``log.exception`` in a route, ``deps.verify_user`` binding ``user_id``, and the
tools it calls) is stitched together by ``request_id``. The same id is echoed
back in the ``X-Request-Id`` response header for client-side correlation.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from obs.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    new_request_id,
)

# Health/SSE chatter we don't want an access-log line for on every poll.
_QUIET_PATHS = {"/health", "/healthz", "/readiness", "/liveness"}


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

        quiet = request.url.path in _QUIET_PATHS
        start = time.perf_counter()
        if not quiet:
            self._log.info("request.start")

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            # The unhandled error itself — the failure point we want traceable.
            self._log.exception("request.error", duration_ms=duration_ms)
            # Answer with the correlation id instead of re-raising into a bare
            # 500: the UI surfaces "request <id>", which is exactly the string
            # to paste into Cloud Logging (jsonPayload.request_id) to find the
            # traceback above. HTTPException never lands here — FastAPI turns
            # it into a response inside call_next.
            return JSONResponse(
                {"detail": "Internal server error", "request_id": request_id},
                status_code=500,
                headers={"X-Request-Id": request_id},
            )

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        if not quiet:
            self._log.info(
                "request.end",
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
        response.headers["X-Request-Id"] = request_id
        return response
