# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""FastAPI gateway for the hermes multi-agent system.

Serves the web API used by the Next.js frontend: ``/jobs``, ``/profile``,
``/applications``, ``/settings``, ``/companies`` (all Firebase-authenticated)
plus the ``/tasks/*`` worker handlers.

The ADK agent surface (``/run``, ``/run_sse``, ``/apps/*`` and the ``/dev-ui``
console) is a development tool and is mounted only when ``ADK_ENABLED=1``.
See the comment on that flag below.
"""

import os
from urllib.parse import quote

import google.auth
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.app_utils.middleware import RequestContextMiddleware
from api.app_utils.telemetry import setup_cloud_otel, setup_telemetry
from api.app_utils.typing import Feedback
from api.deps import verify_user
from api.routes import applications as applications_routes
from api.routes import companies as companies_routes
from api.routes import discovery as discovery_routes
from api.routes import jobs as jobs_routes
from api.routes import profile as profile_routes
from api.routes import worker as worker_routes
from obs.logging import configure_logging, get_logger
from tools import queues

# Load local .env for dev (GOOGLE_CLOUD_*, AUTH_DEV_MODE, WEB_ORIGINS). No-op in
# Cloud Run, where env is provided by Terraform and no .env file is shipped.
load_dotenv()

# Structured logging first, so everything below (telemetry, ADK, route imports)
# logs through the same JSON-on-stdout pipeline Cloud Logging ingests.
configure_logging()
setup_telemetry()
_, project_id = google.auth.default()
logger = get_logger(__name__)

# Origins allowed to call the API (the Next.js frontend). Configure via
# WEB_ORIGINS or ALLOW_ORIGINS (comma-separated); defaults to local dev.
# Exactly one CORSMiddleware must end up installed — two would duplicate the
# Access-Control-Allow-Origin header and break the browser — so it is added in
# the non-ADK branch below only, since get_fast_api_app adds its own.
_origins_env = (
    os.getenv("WEB_ORIGINS") or os.getenv("ALLOW_ORIGINS") or "http://localhost:3000"
)
allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

# Agents live in the sibling "agents/" directory at the project root.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(PROJECT_ROOT, "agents")

# Cloud SQL session configuration
db_user = os.environ.get("DB_USER", "postgres")
db_name = os.environ.get("DB_NAME", "postgres")
db_pass = os.environ.get("DB_PASS")
instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")

session_service_uri = None
if instance_connection_name and db_pass:
    # Use Unix socket for Cloud SQL
    # URL-encode username and password to handle special characters (e.g., '[', '?', '#', '$')
    # These characters can cause URL parsing errors, especially '[' which triggers IPv6 validation
    encoded_user = quote(db_user, safe="")
    encoded_pass = quote(db_pass, safe="")
    # URL-encode the connection name to prevent colons from being misinterpreted
    encoded_instance = instance_connection_name.replace(":", "%3A")

    session_service_uri = (
        f"postgresql+asyncpg://{encoded_user}:{encoded_pass}@"
        f"/{db_name}"
        f"?host=/cloudsql/{encoded_instance}"
    )

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

# The ADK agent surface — /run, /run_sse, /apps/*, /list-apps and the /dev-ui
# console — is a debugging tool, not part of the product: nothing in web/ calls
# it, the frontend talks only to the routers below. ADK registers those routes
# with no auth dependency of its own, so on a service deployed
# --allow-unauthenticated (which hermes-api must be, to serve the browser app)
# they are an open, billable Gemini endpoint that anyone with the URL can drive.
#
# So it is off unless explicitly switched on: set ADK_ENABLED=1 locally to get
# /dev-ui back for agent work. In production the var is unset and every ADK
# path 404s.
ADK_ENABLED = os.getenv("ADK_ENABLED", "").strip().lower() in {"1", "true", "on"}

if ADK_ENABLED:
    from google.adk.cli.fast_api import get_fast_api_app

    app: FastAPI = get_fast_api_app(
        agents_dir=AGENTS_DIR,
        web=True,
        artifact_service_uri=artifact_service_uri,
        allow_origins=allow_origins,
        session_service_uri=session_service_uri,
        otel_to_cloud=True,
    )
else:
    app = FastAPI()
    # Both of these are side effects of get_fast_api_app, so reproduce them —
    # dropping the ADK surface should change the agent routes and nothing else.
    setup_cloud_otel()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Not reproduced: ADK's _OriginCheckMiddleware, which rejects non-safe
    # methods from unlisted origins. It guards ADK's cookie-less agent routes
    # against cross-origin form posts; every route here instead requires a
    # Firebase bearer token, which a cross-origin page cannot attach.

app.title = "hermes"
app.description = "API gateway for the hermes multi-agent system"

# Per-request correlation id + structured access log. Added last so it is the
# outermost middleware and wraps every route (web API and ADK endpoints alike).
app.add_middleware(RequestContextMiddleware)

# Web vetting API (Firebase-auth job + company endpoints).
app.include_router(jobs_routes.router)
app.include_router(companies_routes.router)
app.include_router(applications_routes.router)
app.include_router(profile_routes.router)
app.include_router(discovery_routes.router)
# /tasks/* handlers; they 404 unless this deployment sets WORKER_MODE=1.
app.include_router(worker_routes.router)


@app.post("/feedback")
def collect_feedback(
    feedback: Feedback, user_id: str = Depends(verify_user)
) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log
        user_id: Verified caller, injected by the auth dependency

    Returns:
        Success message
    """
    # Feedback.user_id is client-supplied and defaults to a random uuid; drop it
    # so the log carries the *verified* uid that verify_user bound into the
    # request context instead of whatever the caller claimed.
    payload = feedback.model_dump()
    payload.pop("user_id", None)
    logger.info("feedback.received", **payload)
    return {"status": "success"}


# One line at boot recording how this process will actually behave. All of it is
# environment-driven and none of it is in version control (see
# deployment/terraform/README.md), so "which mode is this revision in?" is
# otherwise only answerable by reading the Cloud Run config.
logger.info(
    "api.boot",
    adk_enabled=ADK_ENABLED,
    execution_mode="queued" if queues.enabled() else "in_process",
    worker_mode=queues.worker_mode(),
    worker_url=os.getenv("WORKER_URL") or None,
    tasks_location=os.getenv("TASKS_LOCATION", "us-central1"),
    # Value never logged — only whether the cron endpoint is reachable at all.
    cron_configured=queues.worker_mode() or bool(os.getenv("CRON_SECRET")),
    adk_sessions="cloudsql" if session_service_uri else "in_memory",
    allow_origins=allow_origins,
    project_id=project_id,
)


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
