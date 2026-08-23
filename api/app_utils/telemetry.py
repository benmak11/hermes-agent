# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

import os

from obs.logging import get_logger

log = get_logger("api.telemetry")


def setup_cloud_otel() -> None:
    """Export traces and logs to Cloud Trace / Cloud Logging.

    ``get_fast_api_app(otel_to_cloud=True)`` installs these exporters as a side
    effect of building the ADK app. When the ADK surface is disabled (the
    default — see ``ADK_ENABLED`` in ``api/main.py``) nothing else would, so
    call this explicitly. It goes through ADK's own helpers so both paths
    configure OTel identically.

    Never fatal: telemetry is not worth failing a boot over.
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
