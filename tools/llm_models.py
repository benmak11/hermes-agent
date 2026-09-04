# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The Gemini model ids used by the matching pipeline, in one place.

The scoring path — JD parsing and Pro scoring — imports its ids from here
rather than declaring them, because a model id declared in two modules is a
retune waiting to happen: change one and the two callers quietly start talking
to different models.

**This is not every Gemini call in the repo, and editing these does not retune
the others.** Deliberate independent declarations, each with its own reasoning
at the site: ``tools/tailoring/objective.py`` (``OBJECTIVE_MODEL``),
``tools/profile/extract.py`` (inline), and ``tools/matching/batch.py``
(``BATCH_FLASH_MODEL``, a genuinely different model — the batch API does not
serve the ``-latest`` alias). Fold one in only by making it import from here;
do not assume it already does.

This module is deliberately **import-free** — no env mutation, no credential
lookup, no third-party import — so that the ids are a property of this file
rather than of the environment a process happens to boot in.

**Do not change these values as a cleanup.** They are load-bearing catalog ids,
not preferences (see ``PRO_MODEL`` below).
"""

from __future__ import annotations

#: High-volume, cheap work: JD parsing. (Résumé tailoring runs on its own
#: ``OBJECTIVE_MODEL`` — changing this does not move it.)
FLASH_MODEL = "gemini-flash-latest"

#: The call worth paying for: job scoring. (Résumé extraction hardcodes the
#: same id inline — changing this does not move it.) The Gemini 3 Pro
#: model available to this project is "gemini-3.1-pro-preview" — there is no
#: bare "gemini-3-pro" id in its Vertex catalog, and substituting one 404s.
PRO_MODEL = "gemini-3.1-pro-preview"
