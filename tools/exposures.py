# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Impression log at ``users/{uid}/exposures`` — what was shown, in what order.

``tools.decisions`` records the choice. It cannot record what the choice was
made *against*: that a job was third of eleven, or that ten other jobs were
candidates the user never acted on. Without that, every ranking metric (nDCG,
MRR, recall@k) and every position-bias correction is uncomputable, and the
un-acted-on jobs — the majority of the data — are invisible.

**"Shown" is a candidate set, not an impression.** This records the list the
API returned. The review page (``web/src/app/app/page.tsx``) renders a
one-card deck — ``const top = jobs[0]`` — so only rank 0 was ever on a screen
and everything below it is prefetch. Treating ``items[1:]`` as seen-and-ignored
would over-count ignores by an order of magnitude. ``rank`` is therefore a
position in the returned candidate set, which is the right input to a ranking
metric and the wrong input to an impression model.

**This is what makes #97's exploration sample interpretable.** That sample is
unbiased in *selection* and biased in *exposure*: ``/jobs/pending`` sorts by
score descending, so every 21-59 job sits below every 60+ job and is only ever
reached in a session where the queue is cleared. A response-rate difference
between sampled and normal jobs is therefore partly a difference in how far
down the list they sat. Exposures are the only record that can separate the
two, which is why ``exploration`` is carried here even though the response
strips it: it is server-side data the client never sees, so it cannot bias the
decision the way a visible badge would.

**One document per response, including every poll — so this collection grows
with tab-open time, not with user activity.** The review page refetches
``/jobs/pending`` on a 3-second timer whenever anything is running or queued
(``web/src/lib/activity.ts`` ``POLL_ACTIVE_MS``), so a 10-minute batch with
that tab open writes ~200 near-identical documents and the user did nothing at
all. Nothing is de-duplicated here, on purpose: a dedupe would need a read on
the hot ``/jobs/pending`` path, and the decision of what counts as "the same
list shown twice" belongs to whoever analyses this, not to the writer.

**The consequence for any analysis: de-duplicate before counting anything.**
An ignore rate, an impression count or an exposure-weighted CTR computed over
raw rows measures how long a background batch ran. Collapse consecutive rows
with identical ``items`` first, or join on ``request_id`` and count sessions.
Default off (``LOG_EXPOSURES``, same shape as ``GEO_GATE_ENFORCE``) partly
because of this volume.

``request_id`` is **the id the request middleware already bound**, not a fresh
one. ``api.app_utils.middleware`` honours an inbound ``x-request-id`` header or
``?request_id=`` query param, echoes it as ``X-Request-Id``, and every log line
of the request carries it. Minting a second id here would produce an exposure
that cannot be joined to the server logs — or to the client's own record — for
the request that created it, which is most of the value of having an id.

No ``expires_at``/TTL, for ``tools.decisions``' reason: the record has to
outlive the job documents it describes.
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

    ``rank`` is **0-based**: the index of the item in the list actually
    returned, so ``items[i]["rank"] == i``. 0-based because it is a Python list
    index — it is produced by ``enumerate`` over the response list and every
    consumer will re-derive it the same way — and because the discount terms
    the metrics need (``1 / log2(rank + 2)``) are written against 0-based ranks
    in every library that computes them. The docstring of
    ``api.routes.jobs.list_pending_jobs`` says so too, since that is where the
    stamping happens.
    """

    job_id: str
    rank: int
    overall_score: float | None
    exploration: bool


def enabled() -> bool:
    """``LOG_EXPOSURES`` — default off, ``GEO_GATE_ENFORCE`` shape.

    Off means off *including the reads*: the route builds no items and the
    decision path takes no lookup, so the feature costs nothing and the
    ``/jobs/pending`` response is byte-for-byte what it always was.
    """
    return os.getenv("LOG_EXPOSURES", "").strip().lower() in {"1", "true", "on"}


def log_exposure(
    user_ref: firestore.DocumentReference,
    *,
    items: list[ExposureItem],
    min_score: int,
    request_id: str | None = None,
) -> None:
    """Append one impression. Never raises.

    ``min_score`` is the threshold that *defined* this choice set, and it is
    recorded because it is not reconstructible from ``items``: the lowest score
    present is only a lower bound on the threshold, and an empty list says
    nothing at all. Without it an analyst cannot tell "this job was not shown
    because the scorer rated it low" from "because the slider was at 90", and
    those are opposite conclusions about the scorer. It is a request parameter,
    so it really does vary between two impressions a minute apart.

    Same contract and same reasoning as ``decisions.log_decision``: a lost
    impression is an inconvenience, a 500 on the main screen is a broken
    product. The exception is logged rather than dropped, which is what makes a
    systematically failing write visible instead of a silently empty dataset.

    Synchronous, because ``/jobs/pending`` is a ``def`` route on the sync
    client. An ``asyncio.run`` here would build and memoise a client against a
    loop that dies with the request — the trap ``PUT /profile`` hit.

    ``request_id`` falls back to the middleware-bound one, which is what the
    caller should be relying on; it is a parameter only so the route can read
    it on the request's own thread and hand it to a background task, where the
    contextvar may no longer be bound.
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

    Stamped onto the decision event so a label can be joined to the impression
    it answered. **The ``job_id`` filter is the whole correctness of that
    join.** The most recent exposure is the user's latest *list*, which need
    not have contained the job being decided: ``web/.../app/tracking`` POSTs
    this same route from the starred and skipped shelves and never fetches
    ``/jobs/pending``, so a 09:00 review-queue impression of ``[A, B, C]``
    followed by a 14:00 shelf restore of ``Z`` would otherwise stamp ``Z``'s
    event with 09:00 — a list that never contained it, and a derived
    time-to-decide of five hours that nothing downstream could detect as
    wrong. Absent from the latest exposure's ``items`` means ``None``, which
    is the same honest answer as no exposure at all.

    ``None`` is returned, never today's timestamp, following
    ``decisions.score_snapshot``: absence must never be mistakable for a
    measured value. Three real cases produce it — no exposure (the flag was
    off), an exposure that did not contain this job (a shelf decision), and a
    failed read — and all three are "we do not know when this was shown".

    **Wrapped, because it is a read on the decision path**: Task 1 shipped an
    unwrapped pre-read there and a transient ``ServiceUnavailable`` turned into
    a failed approval with no tailoring dispatched. A ``DeadlineExceeded`` here
    must cost the event a field, never the user their approval — degraded,
    never absent. The membership test is inside the same ``try`` for the same
    reason: a malformed ``items`` must not 500 a decision either.

    **A time-to-decide computed from this carries a floor of one poll
    interval, and the sign is wrong.** The review page refetches
    ``/jobs/pending`` on a 3s timer while anything is running
    (``web/src/lib/activity.ts`` ``POLL_ACTIVE_MS``), so the exposure this
    returns was very likely written *after* the user had already read the card
    they are now deciding on — ``shown_at`` is the last time the list was
    re-fetched, not the first time the job was seen. Any latency measured off
    it is biased short by up to that interval, and any "decided in under 3
    seconds" population is an artefact of the poll.

    Ordered by ``shown_at`` descending rather than by document id: auto-ids are
    random, so "the last one added" is not recoverable from them. This needs a
    single-field index on ``shown_at``, which Firestore creates automatically.
    Only the latest exposure is examined — not the latest one *containing* the
    job — because the question is "was this card on the screen the user just
    acted from", and a match in an older list is a different impression.
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
