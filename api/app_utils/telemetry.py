# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

import os
import re
from typing import Any

from fastapi import FastAPI

from obs import tracing
from obs.logging import get_logger

log = get_logger("api.telemetry")


def setup_cloud_otel() -> None:
    """Export traces and logs to Cloud Trace / Cloud Logging.

    Called explicitly at boot, because ``api/main.py`` builds a plain FastAPI
    app that installs no exporters. It goes through the ADK telemetry helpers,
    which is the only reason ``google-adk`` is still a dependency; no ADK agent
    runtime is involved. Never fatal — telemetry is not worth failing a boot
    over.
    """
    try:
        import google.auth
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers

        credentials, project_id = google.auth.default()
        maybe_set_otel_providers(
            [
                get_gcp_exporters(
                    enable_cloud_tracing=True,
                    # Metrics stay off — ADK disables them too, pending a fix
                    # for exporter errors during shutdown.
                    enable_cloud_metrics=False,
                    enable_cloud_logging=True,
                    google_auth=(credentials, project_id),
                )
            ]
        )
        log.info("telemetry.cloud_otel", enabled=True)
    except Exception as e:
        log.warning("telemetry.cloud_otel_failed", error=str(e))


def _quiet_paths_regex() -> str:
    """URL regex matching exactly the middleware's quiet paths, any host."""
    from api.app_utils.middleware import _QUIET_PATHS

    paths = "|".join(re.escape(p) for p in sorted(_QUIET_PATHS))
    return rf"^[a-z]+://[^/]+(?:{paths})$"


class _NoExtractPropagator:
    """A text-map propagator that ignores every inbound trace header."""

    fields: frozenset[str] = frozenset()

    def extract(self, carrier: Any, context: Any = None, getter: Any = None) -> Any:
        from opentelemetry.context import Context

        return context if context is not None else Context()

    def inject(self, carrier: Any, context: Any = None, setter: Any = None) -> None:
        return None


class _NoCompletionHook:
    def on_completion(self, **kwargs: Any) -> None:
        return None


def _instrument_genai() -> None:
    """Emit a span per Gemini call; its log events and upload hook stay off."""
    from opentelemetry._logs import NoOpLoggerProvider
    from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor

    GoogleGenAiSdkInstrumentor().instrument(
        logger_provider=NoOpLoggerProvider(), completion_hook=_NoCompletionHook()
    )


def setup_request_tracing(app: FastAPI) -> bool:
    """With ``TRACE_REQUESTS`` on, trace requests and Gemini calls; else no-op.

    **Who samples:** the public API ignores every inbound trace header, so a
    browser's ``traceparent: ...-01`` can neither join nor force a trace; its
    own sampler (``OTEL_TRACES_SAMPLER``) decides. The OIDC-private worker
    reads W3C ``traceparent`` only, to continue the trace enqueued by the API.
    Replaces the process-global propagator, which nothing else here uses.
    Never fatal; returns whether tracing was installed.
    """
    if not tracing.enabled():
        return False
    from opentelemetry import propagate
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    from tools.queues import worker_mode

    on_worker = worker_mode()
    propagate.set_global_textmap(
        TraceContextTextMapPropagator() if on_worker else _NoExtractPropagator()
    )
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=_quiet_paths_regex(),
            # One span per request: the receive/send sub-spans would bill too.
            exclude_spans=["receive", "send"],
        )
    except Exception as e:
        log.warning("telemetry.request_tracing_failed", error=str(e))
        return False
    try:
        _instrument_genai()
    except Exception as e:
        log.warning("telemetry.genai_tracing_failed", error=str(e))
    log.info(
        "telemetry.request_tracing",
        enabled=True,
        inbound_context="traceparent" if on_worker else "ignored",
        sampler=os.getenv("OTEL_TRACES_SAMPLER", "parentbased_always_on"),
        sampler_arg=os.getenv("OTEL_TRACES_SAMPLER_ARG"),
    )
    return True


def setup_telemetry() -> str | None:
    """Configure OpenTelemetry and GenAI telemetry with GCS upload."""

    bucket = os.environ.get("LOGS_BUCKET_NAME")
    capture_content = os.environ.get(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"
    )
    if bucket and capture_content != "false":
        log.info(
            "telemetry.genai_capture",
            enabled=True,
            mode="NO_CONTENT",
        )
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "NO_CONTENT"
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl")
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        os.environ.setdefault(
            "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
        )
        commit_sha = os.environ.get("COMMIT_SHA", "dev")
        os.environ.setdefault(
            "OTEL_RESOURCE_ATTRIBUTES",
            f"service.namespace=hermes,service.version={commit_sha}",
        )
        path = os.environ.get("GENAI_TELEMETRY_PATH", "completions")
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            f"gs://{bucket}/{path}",
        )
    else:
        log.info(
            "telemetry.genai_capture",
            enabled=False,
            hint="set LOGS_BUCKET_NAME and OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT to enable",
        )

    return bucket
