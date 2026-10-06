# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Distributed-tracing helpers, all inert unless ``TRACE_REQUESTS`` is on.

A real ``TracerProvider`` exporting to Cloud Trace is installed at boot whatever
this flag says, so every span opened here has to go through :func:`span` rather
than a bare tracer: off means no span, no header, no OpenTelemetry import.

Spans are billed. Sampling is decided per service by ``OTEL_TRACES_SAMPLER`` /
``OTEL_TRACES_SAMPLER_ARG``, and the SDK default samples **every** request, so
set a ratio on the API before turning this on. The API never lets a caller's
trace header decide sampling; see ``setup_request_tracing`` in
``api/app_utils/telemetry.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_TRACER_NAME = "hermes"


def enabled() -> bool:
    """True when ``TRACE_REQUESTS`` asks for request, queue and run spans."""
    return os.getenv("TRACE_REQUESTS", "").strip().lower() in {"1", "true", "on"}


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Run the block inside a new current span, or inside nothing when off.

    Yields the span (``None`` when off). An exception is recorded on the span,
    which is marked as errored, and then re-raised unchanged.
    """
    if not enabled():
        yield None
        return
    from opentelemetry import trace

    tracer = trace.get_tracer(_TRACER_NAME)
    attrs = {k: v for k, v in attributes.items() if v is not None}
    with tracer.start_as_current_span(name, attributes=attrs) as current:
        yield current


def trace_headers() -> dict[str, str]:
    """W3C ``traceparent`` (+ ``tracestate``) for the current span, for a task.

    Empty when off or when no valid span is current. Injected with an explicit
    W3C propagator, not the process-global one, so it does not depend on how
    this service extracts inbound headers.
    """
    if not enabled():
        return {}
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    if not trace.get_current_span().get_span_context().is_valid:
        return {}
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return carrier
