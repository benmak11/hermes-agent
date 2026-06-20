# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared configuration and model helpers for the hermes agents.

Imported as a top-level module: the ADK agent loader puts the ``agents``
directory on ``sys.path``, so each agent package imports this as ``_shared``.
"""

from __future__ import annotations

import os

import google.auth
from google.adk.models import Gemini
from google.genai import types

# ---------------------------------------------------------------------------
# Environment / Vertex AI configuration (runs once on first import).
# ---------------------------------------------------------------------------
# Resolve the GCP project from Application Default Credentials unless one is
# already supplied via the environment (.env or the shell). Explicit values win.
if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
    try:
        _, _project_id = google.auth.default()
        if _project_id:
            os.environ["GOOGLE_CLOUD_PROJECT"] = _project_id
    except Exception:
        pass

# GOOGLE_CLOUD_LOCATION drives where Gemini requests are served. gemini-3-pro
# is served from the "global" endpoint (it is NOT available in regional
# endpoints like us-central1), so default to global. This is independent of the
# Cloud Run deployment region (which stays us-central1).
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
FLASH_MODEL = "gemini-flash-latest"
# The Gemini 3 Pro model available to this project is "gemini-3.1-pro-preview".
# There is no bare "gemini-3-pro" id in the Vertex catalog.
PRO_MODEL = "gemini-3.1-pro-preview"
# The Application agent drives a browser via Computer Use. Using the Gemini 3 Pro
# model here; verify computer-use support when wiring the real browser backend
# (a dedicated computer-use model may be needed).
COMPUTER_USE_MODEL = "gemini-3.1-pro-preview"

_RETRY = types.HttpRetryOptions(attempts=3)


def flash() -> Gemini:
    """Returns a configured Gemini Flash model instance."""
    return Gemini(model=FLASH_MODEL, retry_options=_RETRY)


def pro() -> Gemini:
    """Returns a configured Gemini 3 Pro model instance."""
    return Gemini(model=PRO_MODEL, retry_options=_RETRY)


def computer_use_model() -> Gemini:
    """Returns the Gemini model used by the Computer Use Application agent."""
    return Gemini(model=COMPUTER_USE_MODEL, retry_options=_RETRY)
