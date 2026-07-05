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
import sys
import uuid
from typing import Any

import structlog

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


def clear_request_context() -> None:
    """Drop all context-bound values (call at the start of each request)."""
    structlog.contextvars.clear_contextvars()
