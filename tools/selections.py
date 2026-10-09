# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Selection log at ``users/{uid}/selections`` — which unscored jobs were given
scoring slots, and why.

One document per pick made under ``PRERANK_MODE`` ``shadow`` or ``on``
(:mod:`tools.matching.selection`); ``off`` writes nothing. Each item records
the arm that chose it, so the ``explore`` picks — a uniform sample of the pool
— can evaluate the prerank without the bias of the ``rank`` picks. Append-only
and never raises: losing a record must never cost a scoring run.

``run_id`` is the structlog run context's, so a record joins to the scored
jobs' ``scored_run_id`` and to the run's cost ledger.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TypedDict

from google.cloud import firestore

from obs.logging import current_run_id, get_logger

log = get_logger("tools.selections")

#: Sub-collection of the user document.
COLLECTION = "selections"


class SelectionItem(TypedDict):
    """One chosen job. ``rank_in_pool`` is its 1-based position in the whole
    unscored pool by ``prerank.sort_key``."""

    job_id: str
    arm: str
    prerank: float
    features: dict[str, float]
    signals: dict[str, str]
    rank_in_pool: int


class ShadowItem(TypedDict):
    """One job the prerank would have chosen, in ``shadow`` mode."""

    job_id: str
    prerank: float


async def log_selection(
    user_ref: firestore.AsyncDocumentReference,
    *,
    mode: str,
    prerank_version: int,
    explore_rate: float | None,
    pool_size: int,
    items: list[SelectionItem],
    shadow_top: list[ShadowItem] | None = None,
) -> None:
    """Append one selection record. Never raises; failures are logged.

    ``shadow_top`` is written only when given (``shadow`` mode).
    """
    try:
        doc: dict = {
            "selected_at": datetime.now(UTC).isoformat(),
            "run_id": current_run_id(),
            "mode": mode,
            "prerank_version": prerank_version,
            "explore_rate": explore_rate,
            "pool_size": pool_size,
            "items": [dict(item) for item in items],
        }
        if shadow_top is not None:
            doc["shadow_top"] = [dict(item) for item in shadow_top]
        await user_ref.collection(COLLECTION).document(uuid.uuid4().hex).set(doc)
    except Exception:
        log.exception("selection.write_failed", mode=mode, count=len(items))
