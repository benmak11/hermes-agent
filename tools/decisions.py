# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Append-only vetting-decision log at ``users/{uid}/decisions``.

``user_decision`` on the job document is current state, overwritten in place;
that is right for the product and useless as a training label. Every change of
a decision therefore also appends one auto-id document here, carrying a
timestamp, the previous value and the score the user was looking at. The job
document is unchanged, and this collection is write-only from the app's point
of view.

``actor`` separates the two writers: a human skip is a label, while the
system's ``dismissed`` write for a vanished posting is no judgement about fit.
``previous_decision`` is read off the job document *before* the update — the
only place the prior answer still exists — which is what makes an undo
reconstructible. No ``expires_at``/TTL: the record has to outlive the job
document it describes.

Two bounds on the data:

1. read-then-update is not atomic. Callers read the job document, write
   ``user_decision``, then log, and ``decide()`` runs in a threadpool, so two
   decisions racing on one job can both report ``prev=pending`` and can land
   out of order. Deliberately not solved with a transaction — the window is a
   few milliseconds of one user double-deciding one job. A repeated
   ``previous_decision`` on one ``job_id`` is this.
2. every deployed writer of ``user_decision`` logs here, so the events replay
   to the document's current state. The one uncovered writer is the superseded
   local Streamlit console ``vetting_ui.py``, which is not deployed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.decisions")

#: Sub-collection of the user document.
COLLECTION = "decisions"

#: Every value ``user_decision`` can take, from either writer. ``pending`` is
#: the undo/restore path and is a real decision, not "never reviewed".
#: Mirrors ``models.job.Job.user_decision``; ``test_decision_events`` pins the
#: two together.
DecisionValue = Literal[
    "pending", "approved", "rejected", "starred", "applied", "dismissed"
]

Actor = Literal["user", "system"]


def score_snapshot(job_doc: dict | None) -> dict | None:
    """What the scorer said about this job, as of the decision.

    The score fields live under the job document's ``match`` key, not at the
    top level; reading them off the document returns ``None`` for every job
    ever scored, and has cost a debugging round once already.

    An unscored job has no ``match`` key and gets ``None`` for the whole
    snapshot rather than a dict of nulls: "never scored" and "scored, got
    null" are different facts. ``scored_with`` is likewise copied off the
    document and is ``None`` for a job scored before that field existed —
    never reconstructed from today's constants, which would label an old job
    with the current model and prompt.
    """
    match = (job_doc or {}).get("match")
    if not match:
        return None
    return {
        "overall_score": match.get("overall_score"),
        "breakdown": match.get("breakdown"),
        "recommendation": match.get("recommendation"),
        # Top level, beside ``match`` — not inside it. Reading it off
        # ``match`` returns ``None`` for every job ever scored, silently.
        "scored_with": (job_doc or {}).get("scored_with"),
    }


def log_decision(
    user_ref: firestore.DocumentReference,
    *,
    job_id: str,
    decision: DecisionValue,
    previous_decision: str | None,
    job_doc: dict | None,
    actor: Actor = "user",
    shown_at: str | None = None,
) -> None:
    """Append one decision event. Never raises.

    Writes to Firestore and swallows every failure, logging it: a lost label
    is better than a 500 on a decision. Call it *after* the ``user_decision``
    write has succeeded, so an event exists only for a decision that changed.

    ``shown_at`` (from ``tools.exposures.latest_shown_at``) joins the label to
    the impression it answered. The key is always present, and ``None`` means
    no exposure is known — legitimately so for decisions made off a shelf,
    before ``LOG_EXPOSURES`` was on, or by ``actor: "system"``.

    Synchronous, because both call sites drive the sync client; an
    ``asyncio.run`` here would memoise a client against a loop that dies with
    the request.
    """
    try:
        user_ref.collection(COLLECTION).add(
            {
                "job_id": job_id,
                "decision": decision,
                "previous_decision": previous_decision,
                "decided_at": datetime.now(UTC).isoformat(),
                "actor": actor,
                "score_snapshot": score_snapshot(job_doc),
                "shown_at": shown_at,
            }
        )
    except Exception:
        log.exception(
            "decision.event_write_failed",
            job_id=job_id,
            decision=decision,
            actor=actor,
        )
