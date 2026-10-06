# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Structured logging for hermes (structlog + stdlib interop).

One config drives the whole process. In production (Cloud Run, detected via the
``K_SERVICE`` env var) logs render as **JSON on stdout** — the format Cloud
Logging ingests natively, mapping our ``severity`` field and linking each line
to its Cloud Trace span. Locally they render as a colorized console.

Why structlog rather than bare ``logging``:
- key/value events instead of f-string prose → filterable in Cloud Logging.
- ``contextvars`` binding (request_id, user_id, trace) that rides along every
  log line in an async request without threading it through call signatures.
- stdlib interop via ``ProcessorFormatter`` so third-party logs (uvicorn,
  google-cloud, httpx, firebase) flow through the *same* renderer.

Call :func:`configure_logging` once at process start (``api.main`` and each CLI
runner do). Everything else just calls :func:`get_logger`.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from obs import tracing

# Libraries that log at INFO on every call and drown out the signal. Kept at
# WARNING unless LOG_LEVEL is explicitly DEBUG.
_NOISY_LOGGERS = (
    "uvicorn.access",
    "google.auth",
    "google.api_core",
    "google.cloud",
    "urllib3",
    "httpx",
    "httpcore",
)

_configured = False

# Ids that arrive from outside the process (headers) are kept only if they are
# this conservative shape; anything else is dropped, never truncated.
_SAFE_ID_CHARS = re.compile(r"[A-Za-z0-9._-]+")
_SAFE_ID_MAX_LEN = 64


def _use_json() -> bool:
    """JSON in prod, console locally. ``LOG_FORMAT`` overrides the autodetect."""
    fmt = os.getenv("LOG_FORMAT", "").lower()
    if fmt == "json":
        return True
    if fmt in ("console", "text", "pretty"):
        return False
    # Cloud Run sets K_SERVICE; treat any such managed runtime as production.
    return bool(os.getenv("K_SERVICE"))


def _add_trace_correlation(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Attach the active OpenTelemetry trace/span so logs link to Cloud Trace.

    The gateway already exports OTel spans (``setup_telemetry`` + ``otel_to_cloud``);
    emitting the trace id in the Cloud Logging special fields makes each log line
    clickable straight to its span. No-op when no span is in flight.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not getattr(ctx, "is_valid", False):
            return event_dict
        trace_id = f"{ctx.trace_id:032x}"
        project = os.getenv("GOOGLE_CLOUD_PROJECT")
        event_dict["logging.googleapis.com/trace"] = (
            f"projects/{project}/traces/{trace_id}" if project else trace_id
        )
        event_dict["logging.googleapis.com/spanId"] = f"{ctx.span_id:016x}"
        event_dict["logging.googleapis.com/trace_sampled"] = bool(
            ctx.trace_flags & 0x01
        )
    except Exception:  # observability must never break the request
        pass
    return event_dict


def _gcp_severity(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Map structlog's ``level`` onto the ``severity`` Cloud Logging reads."""
    level = event_dict.get("level")
    if level:
        event_dict["severity"] = level.upper()
    return event_dict


def _rename_event_key(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Cloud Logging shows the ``message`` field as the entry summary."""
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def configure_logging(*, force_json: bool | None = None) -> None:
    """Configure structlog + stdlib logging for the whole process (idempotent)."""
    global _configured
    if _configured:
        return

    json_logs = force_json if force_json is not None else _use_json()
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    # Run for BOTH structlog-native and stdlib ("foreign") records so a uvicorn
    # or google-cloud log line carries the same timestamp/level/trace fields.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _add_trace_correlation,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to the stdlib ProcessorFormatter for final rendering.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if json_logs:
        final_processors: list[Any] = [
            _rename_event_key,
            _gcp_severity,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        final_processors = [
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *final_processors,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    if level != "DEBUG":
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    # Route uvicorn through our handler instead of its own (avoids double lines);
    # our request middleware already emits structured access logs.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger (configures logging on first use)."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)


def new_request_id() -> str:
    """A short, URL-safe id for correlating one request's log lines."""
    return uuid.uuid4().hex


def bind_request_context(**kwargs: Any) -> None:
    """Bind key/values onto the current context so every later log carries them."""
    structlog.contextvars.bind_contextvars(**kwargs)


def bind_run_context(runner: str, **kwargs: Any) -> str:
    """Bind a batch-run correlation context; returns the generated ``run_id``.

    The batch counterpart of the request middleware: every ``python -m cli.*``
    invocation (cron discovery, matching, tailoring, ...) binds one ``run_id``
    so all log lines of that run — across the pipelines and fetchers it calls —
    are stitched together, exactly like ``request_id`` does for HTTP requests.
    Filter in Cloud Logging with ``jsonPayload.run_id="..."``.
    """
    run_id = new_request_id()
    structlog.contextvars.bind_contextvars(run_id=run_id, runner=runner, **kwargs)
    return run_id


@contextmanager
def run_context(runner: str, **kwargs: Any) -> Iterator[str]:
    """Scoped variant of :func:`bind_run_context` for in-process background runs.

    FastAPI background tasks run sequentially in the request's (copied) context,
    so a bare ``bind_contextvars`` would leak one run's ``run_id`` into the next
    task. This binds ``run_id`` + ``runner`` (+ extras) only for the ``with``
    block and restores the previous context on exit. Yields the ``run_id``.
    With ``TRACE_REQUESTS`` on, the block also runs inside a ``run <runner>``
    span, so spans opened during the run nest under it.
    """
    run_id = new_request_id()
    with (
        structlog.contextvars.bound_contextvars(run_id=run_id, runner=runner, **kwargs),
        tracing.span(
            f"run {runner}", **{"hermes.run_id": run_id, "hermes.runner": runner}
        ),
    ):
        yield run_id


def current_run_id() -> str | None:
    """The ``run_id`` bound by :func:`run_context`, or None outside a run.

    Lets code that is *not* logging (cost accumulation, the ``scored_run_id``
    stamp on a job doc) read the same correlation id every log line already
    carries, without threading it through call signatures.
    """
    return structlog.contextvars.get_contextvars().get("run_id")


def current_request_id() -> str | None:
    """The ``request_id`` the middleware bound, or None outside a request.

    The request-scoped twin of :func:`current_run_id`, for code that is not
    logging but needs the same correlation id — an exposure row joins back to
    the request that produced it. Reading rather than minting is the point: a
    fresh id would correlate with nothing.
    """
    return structlog.contextvars.get_contextvars().get("request_id")


def current_origin_request_id() -> str | None:
    """The enqueuing request's id the worker middleware bound, or None."""
    return structlog.contextvars.get_contextvars().get("origin_request_id")


def current_origin_run_id() -> str | None:
    """The enqueuing run's id the worker middleware bound, or None."""
    return structlog.contextvars.get_contextvars().get("origin_run_id")


def safe_correlation_id(
    value: object, *, max_len: int = _SAFE_ID_MAX_LEN
) -> str | None:
    """``value`` if it is a string of ``[A-Za-z0-9._-]`` up to ``max_len``, else None.

    For correlation ids that cross a process boundary: they are client-influenced,
    so anything else is dropped whole rather than logged or stored.
    """
    if not isinstance(value, str) or not 0 < len(value) <= max_len:
        return None
    return value if _SAFE_ID_CHARS.fullmatch(value) else None


def clear_request_context() -> None:
    """Drop all context-bound values (call at the start of each request)."""
    structlog.contextvars.clear_contextvars()


def log_agent_start(logger: Any, agent: str, **context: Any) -> float:
    """Log the explicit kickoff of a background "agent" cycle (discovery, sweep,
    tailoring, submission, profile extraction, ...).

    Standardizes the event name, so filtering Cloud Logging on
    ``jsonPayload.message="agent.started"`` shows every agent kickoff, and
    requires the caller to say what is being initiated via ``context``.

    Returns a ``time.perf_counter()`` start mark — pass it to
    :func:`log_agent_end`.
    """
    logger.info("agent.started", agent=agent, **context)
    return time.perf_counter()


def log_agent_end(
    logger: Any, agent: str, started: float, *, outcome: str, **result: Any
) -> None:
    """Log the explicit end of a background "agent" cycle (pairs with
    :func:`log_agent_start`).

    ``outcome`` is the field to filter and group on — ``"completed"``,
    ``"failed"``, ``"discarded"``, ``"posting_removed"`` — and ``**result``
    carries whatever summary is useful for that agent.

    A ``duration_ms`` in ``result`` is renamed to ``step_duration_ms`` rather
    than passed through. Splatting it collided with this function's own
    keyword and raised ``TypeError`` *after* the caller's success write, so
    the caller's ``except`` logged a failure on a run that had done its whole
    job — three weeks of sweeps logged ``outcome=failed`` that way. Fixed here
    so no future caller can reintroduce it.
    """
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    step_duration_ms = result.pop("duration_ms", None)
    if step_duration_ms is not None:
        result["step_duration_ms"] = step_duration_ms
    logger.info(
        "agent.finished",
        agent=agent,
        outcome=outcome,
        duration_ms=duration_ms,
        **result,
    )
