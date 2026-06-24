# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""FastAPI gateway for the hermes multi-agent system.

Serves the agents discovered under ``agents/`` (Coordinator plus each
specialist) via the ADK web server. The Coordinator is the primary app;
clients call ``/run`` / ``/run_sse`` with ``app_name="coordinator"``.
"""

import os
from urllib.parse import quote

import google.auth
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging

from api.app_utils.telemetry import setup_telemetry
from api.app_utils.typing import Feedback
from api.routes import applications as applications_routes
from api.routes import companies as companies_routes
from api.routes import jobs as jobs_routes
from api.routes import profile as profile_routes

# Load local .env for dev (GOOGLE_CLOUD_*, AUTH_DEV_MODE, WEB_ORIGINS). No-op in
# Cloud Run, where env is provided by Terraform and no .env file is shipped.
load_dotenv()

setup_telemetry()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)

# Origins allowed to call the API (the Next.js frontend). This drives BOTH
# ADK's cross-origin gate for non-safe methods (POST/PUT/...) AND the CORS
# response headers — get_fast_api_app handles both from allow_origins, so no
# separate CORSMiddleware is needed (a second one would duplicate the
# Access-Control-Allow-Origin header and break the browser). Configure via
# WEB_ORIGINS or ALLOW_ORIGINS (comma-separated); defaults to local dev.
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

app: FastAPI = get_fast_api_app(
    agents_dir=AGENTS_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=True,
)
app.title = "hermes"
app.description = "API gateway for the hermes multi-agent system"

# Web vetting API (Firebase-auth job + company endpoints).
app.include_router(jobs_routes.router)
app.include_router(companies_routes.router)
app.include_router(applications_routes.router)
app.include_router(profile_routes.router)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
