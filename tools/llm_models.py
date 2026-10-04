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
(``BATCH_FLASH_MODEL``, pinned because the batch API does not serve a
``-latest`` alias — it names the same model as ``FLASH_MODEL`` now that the
alias here is pinned too, and the two are still separate declarations because
the batch catalog and the interactive one can diverge again). Fold one in only
by making it import from here; do not assume it already does.

This module is deliberately **import-free** — no env mutation, no credential
lookup, no third-party import — so that the ids are a property of this file
rather than of the environment a process happens to boot in.

**Do not change these values as a cleanup.** They are load-bearing catalog ids,
not preferences (see ``PRO_MODEL`` below).
"""

from __future__ import annotations

#: High-volume, cheap work: JD parsing. (Résumé tailoring declares the same id
#: as its own ``OBJECTIVE_MODEL`` — changing this does not move it.)
#:
#: **Pinned, where this used to be ``gemini-flash-latest``.** The alias is a
#: moving target: Google can repoint it at a new generation with no commit in
#: this repo, and that has already cost this project twice — every parse call
#: in a backlog run 400ing on ``thinking_level`` (2026-07-08, see
#: ``pipeline._PARSE_JD_THINKING``), and a score-provenance record
#: (``score.scored_with``) that names a model the alias no longer serves, which
#: is unfalsifiable after the fact. A label store whose model column means
#: "whatever Google was serving that week" cannot answer the one question it
#: exists for: is a December score comparable to an October one.
#:
#: ``gemini-2.5-flash`` is the concrete id the alias serves today — stated
#: independently in ``obs.llm_cost`` (priced identically to the alias) and
#: confirmed by the live 400 above — so this pin is a no-op on behaviour today
#: and a stop on silent behaviour changes tomorrow. It is the same id
#: ``tools.matching.batch.BATCH_FLASH_MODEL`` already had to pin, because batch
#: prediction rejects aliases; the two now agree, deliberately, and
#: ``tests/unit/test_scored_with.py`` was rewritten to discriminate batch from
#: online by *which constant was read* rather than by the two ids differing.
#:
#: **A pin stops inheriting Google's fixes as well as its drift**, and that is
#: the real cost: a quality improvement, a latency improvement or a deprecation
#: window all arrive by the alias moving, and this file will not. Moving it is
#: a deliberate act with four steps: (1) confirm the new id exists in *this
#: project's* Vertex catalog at ``GOOGLE_CLOUD_LOCATION=global`` — a 404 here
#: is a location/catalog problem, not a name problem; (2) add a pricing entry
#: for it in ``obs.llm_cost._PRICING_PER_MILLION``, or ``compute_cost_usd``
#: returns ``None`` and ``tools.tailoring.rates`` raises **at import**, taking
#: the service down at startup; (3) check the thinking knob — 2.5 takes only
#: ``thinking_budget``, 3.x takes either but never both, so a 3.x pin makes
#: ``_PARSE_JD_THINKING`` and ``_OBJECTIVE_THINKING`` a choice rather than a
#: given; (4) bump nothing about the prompt versions, but expect
#: ``scored_with`` to split the corpus at that commit — scores either side of
#: the move are not comparable, which is exactly what the field is for.
FLASH_MODEL = "gemini-2.5-flash"

#: The call worth paying for: job scoring. (Résumé extraction hardcodes the
#: same id inline — changing this does not move it.) The Gemini 3 Pro
#: model available to this project is "gemini-3.1-pro-preview" — there is no
#: bare "gemini-3-pro" id in its Vertex catalog, and substituting one 404s.
PRO_MODEL = "gemini-3.1-pro-preview"
