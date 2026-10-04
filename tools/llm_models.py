# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The Gemini model ids used by the matching pipeline, in one place.

The scoring path — JD parsing and Pro scoring — imports its ids from here
rather than declaring them, so the two callers cannot drift onto different
models.

This is not every Gemini call in the repo, and editing these does not retune
the others: ``tools/tailoring/objective.py``, ``tools/profile/extract.py`` and
``tools/matching/batch.py`` declare their own, deliberately. Fold one in only
by making it import from here.

Deliberately import-free — no env mutation, no credential lookup, no
third-party import — so the ids are a property of this file rather than of the
environment a process boots in.

Do not change these values as a cleanup. They are load-bearing catalog ids,
not preferences.
"""

from __future__ import annotations

#: High-volume, cheap work: JD parsing. (Résumé tailoring declares the same id
#: as its own ``OBJECTIVE_MODEL`` — changing this does not move it.)
#:
#: A concrete pinned id, not the ``gemini-flash-latest`` alias. Google can
#: repoint an alias with no commit here, which has cost this project twice: a
#: backlog run where every parse call 400ed on ``thinking_level``
#: (2026-07-08), and provenance records naming a model the alias no longer
#: serves. ``gemini-2.5-flash`` is what the alias serves today, and is the same
#: id ``tools.matching.batch.BATCH_FLASH_MODEL`` pins because batch prediction
#: rejects aliases.
#:
#: A pin also stops inheriting Google's fixes, so moving it is deliberate:
#:
#: 1. confirm the new id is in *this* project's Vertex catalog at
#:    ``GOOGLE_CLOUD_LOCATION=global`` — a 404 is a location problem, not a
#:    name problem;
#: 2. add a pricing entry in ``obs.llm_cost._PRICING_PER_MILLION``, or
#:    ``compute_cost_usd`` returns ``None`` and ``tools.tailoring.rates``
#:    raises at import, taking the service down at startup;
#: 3. check the thinking knob — 2.5 takes only ``thinking_budget``, 3.x takes
#:    either but never both;
#: 4. expect ``scored_with`` to split the corpus at that commit: scores either
#:    side of the move are not comparable.
FLASH_MODEL = "gemini-2.5-flash"

#: The call worth paying for: job scoring. (Résumé extraction hardcodes the
#: same id inline — changing this does not move it.) A concrete pinned id:
#: there is no bare "gemini-3-pro" in this project's Vertex catalog, and
#: substituting one 404s every scoring call.
PRO_MODEL = "gemini-3.1-pro-preview"
