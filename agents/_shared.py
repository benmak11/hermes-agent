# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
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

# GOOGLE_CLOUD_LOCATION drives where Gemini requests are served. us-central1
# matches the Cloud Run deployment region. If a model (e.g. gemini-3-pro)
# requires the global endpoint, set GOOGLE_CLOUD_LOCATION=global.
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
FLASH_MODEL = "gemini-flash-latest"
PRO_MODEL = "gemini-3-pro"
# The Application agent drives a browser via Computer Use, which requires a
# computer-use-capable Gemini model. gemini-3-pro is used here; swap if you
# adopt a dedicated computer-use preview model.
COMPUTER_USE_MODEL = "gemini-3-pro"

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
