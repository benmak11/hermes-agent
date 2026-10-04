# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Impression log at ``users/{uid}/exposures`` — what was shown, in what order.

``tools.decisions`` records the choice; this records the candidate set the
choice was made against, which is what ranking metrics and position-bias
corrections need. ``exploration`` is carried here even though the API response
strips it, so the sampled jobs can be separated from the exposure bias of
sitting below every higher-scoring job.

One document per ``/jobs/pending`` response, polls included, so the collection
grows with tab-open time rather than with user activity and nothing is
de-duplicated. Any rate computed over raw rows measures how long a background
batch ran: collapse consecutive rows with identical ``items``, or join on
``request_id``, first. Default off (``LOG_EXPOSURES``).

``rank`` is a position in the returned candidate set, not an impression — the
review page renders a one-card deck, so only rank 0 was ever on a screen.
``request_id`` is the id the request middleware already bound, not a fresh one,
so an exposure joins to the server logs for its request. No ``expires_at``/TTL,
for ``tools.decisions``' reason: the record has to outlive the job documents it
describes.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TypedDict

from google.cloud import firestore

from obs.logging import current_request_id, get_logger

log = get_logger("tools.exposures")

#: Sub-collection of the user document.
COLLECTION = "exposures"


class ExposureItem(TypedDict):
    """One row of a shown list.

    ``rank`` is 0-based: the index of the item in the list actually returned,
    so ``items[i]["rank"] == i``, matching ``enumerate`` and the
    ``1 / log2(rank + 2)`` discount the ranking metrics use.
    """

    job_id: str
    rank: int
    overall_score: float | None
    exploration: bool


def enabled() -> bool:
    """Whether ``LOG_EXPOSURES`` is set. Default off.

    Off disables the reads too: the route builds no items and the decision path
    takes no lookup.
    """
    return os.getenv("LOG_EXPOSURES", "").strip().lower() in {"1", "true", "on"}


def log_exposure(
    user_ref: firestore.DocumentReference,
    *,
    items: list[ExposureItem],
    min_score: int,
    request_id: str | None = None,
) -> None:
    """Append one impression to Firestore. Never raises; failures are logged.

    ``min_score`` is the threshold that defined this choice set and is not
    reconstructible from ``items``, so it is recorded separately — it is a
    request parameter and really does vary between impressions.

    Synchronous, because ``/jobs/pending`` is a ``def`` route on the sync
    client; an ``asyncio.run`` here would memoise a client against a loop that
    dies with the request. ``request_id`` falls back to the middleware-bound
    one, and is a parameter only so a background task can be handed the id read
    on the request's own thread, where the contextvar is still bound.
    """
    try:
        user_ref.collection(COLLECTION).add(
            {
                "shown_at": datetime.now(UTC).isoformat(),
                "request_id": request_id or current_request_id(),
                "min_score": min_score,
                "items": [dict(item) for item in items],
            }
        )
    except Exception:
        log.exception("exposure.write_failed", count=len(items))


def latest_shown_at(user_ref: firestore.DocumentReference, job_id: str) -> str | None:
    """When this user was last shown ``job_id``, or ``None``. Never raises.

    Stamped onto the decision event so a label joins to the impression it
    answered. Only the latest exposure is examined, and ``None`` is returned
    unless it contains ``job_id``: decisions also arrive from the starred and
    skipped shelves, which never fetch ``/jobs/pending``, and stamping those
    with an unrelated list's timestamp would invent a time-to-decide nothing
    downstream could detect as wrong. ``None`` means "not known" — no exposure,
    no match, or a failed read — never a measured value.

    Wrapped, membership test included, because it is a read on the decision
    path: a transient Firestore error must cost the event a field, never the
    user their approval. Ordered by ``shown_at`` descending because auto-ids
    are random.

    Any time-to-decide derived from this is biased short: the review page
    re-polls ``/jobs/pending`` every 3s while work is running, so ``shown_at``
    is the last re-fetch of the list, not the first sighting of the card.
    """
    try:
        snaps = list(
            user_ref.collection(COLLECTION)
            .order_by("shown_at", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )
        if not snaps:
            return None
        doc = snaps[0].to_dict() or {}
        shown = [item.get("job_id") for item in (doc.get("items") or [])]
        if job_id not in shown:
            return None
        return doc.get("shown_at")
    except Exception:
        log.exception("exposure.lookup_failed", job_id=job_id)
        return None
