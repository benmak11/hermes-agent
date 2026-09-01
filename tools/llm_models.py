# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The Gemini model ids, in one place.

These used to be declared twice — ``agents/_shared.py`` and
``tools/matching/pipeline.py`` — with identical values and a "keep in sync"
comment doing the enforcing. Two declarations of a model id is a retune waiting
to happen: change one, and the deterministic matching pipeline and the ADK
agents quietly start talking to different models.

This module is deliberately **import-free**. ``agents/`` is otherwise a
self-contained package (its modules import only ``_shared``, their siblings,
and ``google.adk``), so the one edge it gains here has to be inert: no env
mutation, no credential lookup, no third-party import, nothing that could fail
or slow down at import time. The Dockerfile already ships ``tools/`` alongside
``agents/`` and runs from the repo root, which is what makes the edge resolve.

**Do not change these values as a cleanup.** They are load-bearing catalog ids,
not preferences (see ``PRO_MODEL`` below).
"""

from __future__ import annotations

#: High-volume, cheap work: JD parsing, and the ADK agents that only route.
FLASH_MODEL = "gemini-flash-latest"

#: The call worth paying for (scoring, résumé extraction). The Gemini 3 Pro
#: model available to this project is "gemini-3.1-pro-preview" — there is no
#: bare "gemini-3-pro" id in its Vertex catalog, and substituting one 404s.
PRO_MODEL = "gemini-3.1-pro-preview"

#: The Application agent drives a browser via Computer Use. Using the Gemini 3
#: Pro model here; verify computer-use support when wiring the real browser
#: backend (a dedicated computer-use model may be needed).
COMPUTER_USE_MODEL = "gemini-3.1-pro-preview"
