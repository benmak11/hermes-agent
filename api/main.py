# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""FastAPI gateway for the hermes pipelines.

Serves the web API used by the Next.js frontend: ``/jobs``, ``/profile``,
``/applications``, ``/settings``, ``/companies``, ``/account`` (all
Firebase-authenticated) plus the ``/tasks/*`` worker handlers, which the same
image answers only when deployed with ``WORKER_MODE=1``.

The work itself lives in ``tools/`` (deterministic pipelines) and ``cli/``
(their batch runners); this module is the HTTP edge in front of them.
"""

import os

import google.auth
from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.app_utils.middleware import RequestContextMiddleware
from api.app_utils.telemetry import setup_cloud_otel, setup_telemetry
from api.app_utils.typing import Feedback
from api.deps import dev_mode, verify_user
from api.routes import account as account_routes
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

# Structured logging first, so everything below (telemetry, route imports)
# logs through the same JSON-on-stdout pipeline Cloud Logging ingests.
configure_logging()
setup_telemetry()
_, project_id = google.auth.default()
logger = get_logger(__name__)

# Origins allowed to call the API (the Next.js frontend). Configure via
# WEB_ORIGINS or ALLOW_ORIGINS (comma-separated); defaults to local dev.
# Exactly one CORSMiddleware ends up installed below — two would duplicate the
# Access-Control-Allow-Origin header and break the browser.
_origins_env = (
    os.getenv("WEB_ORIGINS") or os.getenv("ALLOW_ORIGINS") or "http://localhost:3000"
)
allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]

# ``/docs`` and ``/openapi.json`` are a development tool, and they were
# answering 200 unauthenticated on production. Nothing behind them is data or a
# billable model — they publish the *shape* of the API — so this is
# reconnaissance aid rather than a vulnerability, and the fix is
# correspondingly plain: don't publish it.
#
# Gated on ``deps.dev_mode()`` rather than a flag of its own, because that is
# already the codebase's answer to "is this a developer's machine?" — Terraform
# never sets AUTH_DEV_MODE, so a deployed revision cannot turn these back on by
# accident.
app = FastAPI(
    docs_url="/docs" if dev_mode() else None,
    openapi_url="/openapi.json" if dev_mode() else None,
    # ``/redoc`` renders the same document from the same URL, so it is the same
    # surface and moves with them.
    redoc_url="/redoc" if dev_mode() else None,
)
setup_cloud_otel()
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# No separate origin check on top of CORS: every route here requires a Firebase
# bearer token, which a cross-origin page cannot attach.

app.title = "hermes"
app.description = "API gateway for the hermes job-search pipelines"

# Per-request correlation id + structured access log. Added last so it is the
# outermost middleware and wraps every route.
app.add_middleware(RequestContextMiddleware)

# Web vetting API (Firebase-auth job + company endpoints).
app.include_router(jobs_routes.router)
app.include_router(companies_routes.router)
app.include_router(applications_routes.router)
app.include_router(profile_routes.router)
app.include_router(discovery_routes.router)
app.include_router(account_routes.router)
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
    # Reads back the app that was actually built, not the flag that was meant
    # to build it — this line exists to answer "which mode is this revision in?"
    docs_published=app.docs_url is not None,
    execution_mode="queued" if queues.enabled() else "in_process",
    worker_mode=queues.worker_mode(),
    worker_url=os.getenv("WORKER_URL") or None,
    tasks_location=os.getenv("TASKS_LOCATION", "us-central1"),
    # Value never logged — only whether the cron endpoint is reachable at all.
    cron_configured=queues.worker_mode() or bool(os.getenv("CRON_SECRET")),
    allow_origins=allow_origins,
    project_id=project_id,
)


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
