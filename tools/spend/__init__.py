# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Consent for spending the user's money.

Two pieces, deliberately separate:

- :mod:`tools.spend.estimate` — *what would this cost?* A range with its
  provenance and the cap that bounds it, derived from the budget grant the
  user has left, never from the size of their backlog.
- :mod:`tools.spend.consent` — *did they say yes to that?* A single-use
  Firestore token minted with an estimate attached and consumed by the route
  that spends.

**This is not the spend cap.** ``tools.matching.budget`` is, it is untouched,
and nothing here can grant a slot. The budget bounds what a yes can cost; this
package is only about the yes.

The FastAPI half — the ``confirm`` body field and the dependency that answers
402 — lives in ``api.deps`` rather than here, so ``tools/`` stays free of any
import from ``api/`` or from FastAPI. That boundary holds everywhere else in
this package and the CLIs depend on it.
"""

from tools.spend.consent import ConsentRequired, consume, preflight
from tools.spend.estimate import (
    ACTIONS,
    DISCOVERY_SCAN,
    SCORE_BACKLOG,
    Estimate,
    build,
)

__all__ = [
    "ACTIONS",
    "DISCOVERY_SCAN",
    "SCORE_BACKLOG",
    "ConsentRequired",
    "Estimate",
    "build",
    "consume",
    "preflight",
]
