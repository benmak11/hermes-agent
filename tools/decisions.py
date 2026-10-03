# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Append-only vetting-decision log at ``users/{uid}/decisions``.

``user_decision`` on the job document is *current state*, written with an
in-place ``update``. That is the right shape for the product — every reader
wants the latest answer — and the wrong shape for a training label: there is
no timestamp, no history, and no record of the score the user was looking at
when they chose. An undo overwrites the original choice and it is gone.

So each change of a decision also appends one auto-id document here. The job
document keeps its ``user_decision`` exactly as before; nothing that reads it
changes. This collection is additive, write-only from the app's point of view,
and read for the first time by a ranking model.

``actor`` separates the two writers. A human skip is a label ("not for me");
the system's ``dismissed`` write when a posting disappears is not a judgement
about fit at all, and a model that cannot tell them apart learns that dead
postings are bad matches.

**``previous_decision`` comes from the job document as it was read *before*
the update**, which is the only place the prior answer still exists. Together
with ``decided_at`` it is what makes an undo reconstructible: approve → undo
→ reject is three events chained ``None → approved → pending``, not one
overwrite.

No ``expires_at``/TTL here, unlike ``tools.run_costs``: the point of the
collection is that the record outlives the job document it describes.

Two bounds on the data, stated here because they are cheap to know now and
expensive to rediscover from six months of labels:

1. **read-then-update is not atomic.** Every caller reads the job document,
   writes ``user_decision``, then logs. FastAPI runs ``decide()`` in a
   threadpool, so two decisions racing on one job can both read ``pending``
   and emit ``(approved, prev=pending)`` and ``(pending, prev=pending)`` — the
   second's replaced value was really ``approved``, which makes that undo
   indistinguishable from a first decision. ``decided_at`` is stamped at write
   time, so a pair of events can also land in the opposite order to the
   updates that caused them. **Deliberately not solved with a transaction**:
   the window is a few milliseconds of one user double-deciding one job, and a
   transaction is real complexity to buy for a log. A consumer that sees a
   repeated ``previous_decision`` on one ``job_id`` is looking at this.
2. **covered writers.** ``api.routes.jobs.decide`` (``actor: "user"``), plus
   three system dismissals: the post-skip probe in ``api.routes.jobs``, the
   liveness sweep in ``tools.ats.sweep`` (by volume, the main one), and the
   pre-flight check in ``api.routes.applications``. Those are every writer of
   ``user_decision`` the served app has as of this change, so the events
   replay to the document's current state — an invariant a later join on
   exposures will assume. The one uncovered writer is ``vetting_ui.py``, the
   superseded local Streamlit console, which is not deployed.
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
#: the undo/restore path and is a real decision — it is how "I changed my
#: mind" is distinguishable from "never reviewed".
#:
#: Mirrors ``models.job.Job.user_decision``, which is the field these events
#: describe. ``test_decision_events`` pins the two together, because a copy of
#: a Literal that nothing compares drifts silently.
DecisionValue = Literal[
    "pending", "approved", "rejected", "starred", "applied", "dismissed"
]

Actor = Literal["user", "system"]


def score_snapshot(job_doc: dict | None) -> dict | None:
    """What the scorer said about this job, as of the decision.

    **The fields are under ``match``, not at the top level.** A scored job doc
    is ``{company, title, url, ..., match: {overall_score, breakdown,
    recommendation, reasoning, ...}}``; reading ``overall_score`` off the
    document returns ``None`` for every job ever scored, and the label store is
    then silently worthless. This project has already paid a debugging round
    for that path once.

    An *unscored* job has no ``match`` key, and gets ``None`` rather than a
    dict of nulls: "we never scored this" and "we scored it and got null" are
    different facts, and a model trained on the second when the first is true
    is learning from noise.

    ``scored_with`` (model + prompt versions) does not exist yet — it is
    written by ML-readiness Task 3. The key is present and ``None`` so the
    documents written before that task and after it have one shape.
    """
    match = (job_doc or {}).get("match")
    if not match:
        return None
    return {
        "overall_score": match.get("overall_score"),
        "breakdown": match.get("breakdown"),
        "recommendation": match.get("recommendation"),
        "scored_with": None,
    }


def log_decision(
    user_ref: firestore.DocumentReference,
    *,
    job_id: str,
    decision: DecisionValue,
    previous_decision: str | None,
    job_doc: dict | None,
    actor: Actor = "user",
) -> None:
    """Append one decision event. Never raises.

    Called *after* the ``user_decision`` write has succeeded, so an event
    exists only for a decision that really changed.

    Swallowing every failure is deliberate: a lost label is an inconvenience,
    a 500 on a decision is a broken product. The exception is logged rather
    than dropped, which is what makes a systematically failing write visible.

    Synchronous, because both call sites are: ``decide()`` is a ``def`` route
    on the sync client, and the dismissal task already drives that client's
    ``update``. An ``asyncio.run`` here would build and memoise a client
    against a loop that dies with the request — the trap ``PUT /profile`` hit.
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
            }
        )
    except Exception:
        log.exception(
            "decision.event_write_failed",
            job_id=job_id,
            decision=decision,
            actor=actor,
        )
