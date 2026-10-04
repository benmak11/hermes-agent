# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Collect applications whose worker died, and give the user a way forward.

A process claiming an in-progress status can be killed mid-run, and then the
terminal write in ``run_tailoring``/``run_submission``'s ``except`` never
executes: the document sits in ``queued``, ``tailoring`` or ``submitting``
forever, with ``submit`` and ``regenerate`` 409ing, the undo path refusing to
delete it and the liveness sweep sparing it. This is the scheduled pass that
collects those.

No decision made here is acted on outside the swap that re-checks it. Every
recovery starts with :func:`state.try_claim_lease`, which swaps status and
lease together and re-reads both on its retry, and every status write after it
carries ``allowed_from``. A document that moved between the read and the write
is left where it is for the next pass to re-decide.

Staleness is the lease, not the age: an unexpired lease is first-hand evidence
from the process doing the work, and nothing here may touch such a document.
``queued`` is the exception, because nothing claims it in the ordinary flow, so
there is no lease to read until the reaper writes one.

The apply fork is the safety property. A dead ``tailoring`` run costs ~$0.002
to redo and is retried automatically. A dead ``submitting`` run is not, because
a crash *after* the Submit click may have filed a real application, and nothing
undoes a second one. So ``submitting`` forks on ``submit_attempted_at``, which
``run_submission`` writes immediately before the click:

- no marker — nothing was sent. Released to ``failed`` with a note saying so.
- marker present — the outcome is unknown. Released to ``failed`` with
  ``submission_uncertain`` set and a note telling the user to check their email
  first. **Never re-enqueued, at any attempt count**; that is what the
  ``hermes-apply`` queue's ``max_attempts = 1`` protects.

``failed`` for both, and ``submitting → ready_for_review`` is deliberately not
an edge: "ready to send" is the wrong thing to say about a document that may
already be in an employer's ATS. The two branches are told apart by the note
and by the ``submission_uncertain`` boolean rather than a new
``ApplicationStatus``, because ``web/`` renders a closed union and would show
an unknown status as "failed — open to retry".

Unleased ``submitting`` is not reaped at all. The route writes the status and
the run writes the lease, so an unleased document may simply be one the worker
has not picked up yet: ambiguous, not dead. This pass reports it and moves on,
leaving the age arithmetic in ``cli/unwedge_submitting`` to an operator.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from obs.logging import get_logger
from tools.applications import state

log = get_logger("tools.applications.reaper")

#: Statuses this pass queries for. A single-field ``in`` filter, so it needs no
#: composite index — the reason it is not narrowed further server-side.
REAPABLE: list[str] = ["queued", "tailoring", "submitting"]

#: How many times a document may be recovered automatically before it is failed
#: for the user to pick up by hand. Bounds the one loop here that can repeat:
#: re-dispatch → the run dies again → re-dispatch. Each retry costs one tailoring
#: run (~$0.002), so the cap is about not looping forever, not about money.
MAX_ATTEMPTS = 3

#: Documents one pass may look at, per user. A latency bound, not a policy:
#: this pass runs in-request inside the hourly ``cron_tick``, which Cloud
#: Scheduler gives ~180s before retrying the whole fan-out, and a recovered
#: document costs ~200ms.
#:
#: Without it, a large backlog (every stale document at once, on the first run
#: after a deploy) overruns the deadline and is re-dispatched en masse into a
#: ``tailor`` queue provisioned at 1 dispatch/second — which takes longer than
#: the ``queued`` lease to drain, so the reaper starts failing work the queue
#: was processing correctly. The cap turns that cliff into a drip.
#:
#: It applies to documents scanned, not recovered, which is what bounds the
#: work. Truncation is reported in the tally, since a pass that ran out of
#: budget otherwise looks exactly like a pass with nothing to do. There is
#: deliberately no ``order_by`` — any ordering would need a composite index —
#: so a large backlog drains in key order over successive ticks.
MAX_PER_PASS = 25

#: Counts recoveries performed on this document since the last time it worked
#: or the user asked again. Written inside the compare-and-swap that performs
#: one, never beside it, or two passes can share an attempt number.
#:
#: It has an epoch because the cap bounds *consecutive* failures: two swaps in
#: ``api.routes.applications`` clear it, ``run_tailoring``'s
#: ``→ ready_for_review`` publish and ``regenerate``'s ``→ queued``. As a
#: lifetime total it would leave a long-lived application permanently one stale
#: tick from :data:`GAVE_UP_NOTE`, which dispatches nothing while telling the
#: user to press a button that cannot help.
#:
#: Deliberately not ``submit_attempts``: that counter names the apply task, and
#: touching it dedupes a real submission into silence.
ATTEMPTS_FIELD = "reap_attempts"

#: Set when a submission died with the Submit click already behind it. Backend
#: only — nothing in ``web/`` reads it, and nothing should have to for the user
#: to be safe, because the note beside it says the same thing in words.
UNCERTAIN_FIELD = "submission_uncertain"

#: Written by ``run_submission``'s progress callback at the point of no return,
#: and read here and nowhere else.
#:
#: Scoped to one attempt, not to the document: ``POST /submit`` clears it inside
#: the swap that claims ``→ submitting``, so the fork below asks whether *this*
#: run clicked. Otherwise a retry after a ``release_uncertain`` would be
#: reported uncertain however early it died. :data:`UNCERTAIN_FIELD` carries
#: that fact forward instead.
CLICKED_FIELD = "submit_attempted_at"

#: Notes are rendered verbatim by ``web/``, so these are user-facing copy. A
#: re-dispatch deliberately writes none: it changes no status, and an hourly
#: entry saying so would bury the timeline under bookkeeping.
REQUEUE_NOTE = "tailoring was interrupted — re-queued automatically."
GAVE_UP_NOTE = (
    "tailoring could not be completed after several automatic attempts. "
    "Use Regenerate to try again."
)
NEVER_CLICKED_NOTE = (
    "submission was interrupted before the application was sent — nothing was "
    "submitted, so it is safe to submit again."
)
UNCERTAIN_NOTE = (
    "submission was interrupted after the application was sent. It is UNKNOWN "
    "whether it went through; check your email for a confirmation from the "
    "employer before submitting again."
)

#: What one document's inspection concludes. Every one is counted and reported:
#: a document this pass declines to act on stays stuck.
Verdict = Literal[
    "alive",
    "ambiguous",
    "redispatch",
    "requeue",
    "give_up",
    "release_unstarted",
    "release_uncertain",
]

#: Verdicts that put work back on a queue. ``release_uncertain`` is not here and
#: must never be — see the module docstring.
DISPATCHING: frozenset[str] = frozenset({"redispatch", "requeue"})

#: ``(user_id, job_id) -> was it scheduled?``
DispatchFn = Callable[[str, str], bool]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value) -> datetime | None:
    """A stored timestamp as an aware datetime, or ``None`` if unusable."""
    if isinstance(value, datetime):  # Firestore hands timestamps back as these
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def last_activity_at(doc: dict) -> datetime | None:
    """When anything last happened to this document, or ``None``.

    The newest timeline entry: every status write and every progress note
    appends one, so a live, chattering run looks recent even when it started
    long ago. ``cli/unwedge_submitting`` measures from ``last_submitted_at``
    instead, answering the narrower "how long since the submit request".
    """
    stamps = [
        _parse_iso(event.get("at"))
        for event in (doc.get("timeline") or [])
        if isinstance(event, dict)
    ]
    usable = [s for s in stamps if s is not None]
    return max(usable) if usable else None


def attempts(doc: dict) -> int:
    """Recoveries already performed on this document. Never negative."""
    try:
        return max(0, int(doc.get(ATTEMPTS_FIELD) or 0))
    except (TypeError, ValueError):
        return 0


def _has_lease(doc: dict) -> bool:
    """Does this document carry something that is actually a lease?

    ``isinstance``, not ``in``: ``state.lease_is_held`` reads a non-dict
    ``lease`` as unheld, which on ``submitting`` would turn "unreadable" into
    "free to fail". Treating it as no lease sends it down the ambiguous path.
    """
    return isinstance(doc.get(state.LEASE_FIELD), dict)


def is_stale(doc: dict, *, now: datetime) -> bool:
    """Has whatever was working on this document stopped? Pure.

    Where a lease exists it is the whole answer, and an unreadable one counts
    as live: the lease comes from the process doing the work and already
    outlives the longest run it guards, so consulting the age too would only
    delay collecting a document whose owner is known dead.

    Age is the fallback for a document with no lease, which after
    :func:`classify`'s checks means ``queued`` or a ``tailoring`` document
    predating leases. Unknown age counts as stale — it means no timeline at
    all, which no document written by this build has.
    """
    if _has_lease(doc):
        return not state.lease_is_held(doc, now=now)
    floor = state.IN_PROGRESS.get(doc.get(state.STATUS_FIELD) or "")
    if floor is None:
        return False
    when = last_activity_at(doc)
    return when is None or (now - when).total_seconds() >= floor


def classify(doc: dict, *, now: datetime, max_attempts: int = MAX_ATTEMPTS) -> Verdict:
    """What should happen to this document? Pure — no I/O, no clock, no writes.

    Split out from the writing so the table is unit-testable without Firestore
    and so a dry run reports exactly what an execute would attempt. The verdict
    is still re-checked by the swap that acts on it: this decides, it does not
    authorise.
    """
    status = doc.get(state.STATUS_FIELD)
    if status not in REAPABLE:
        # Moved between the query and the read, or a legacy value the table
        # does not know. Either way: not ours.
        return "alive"

    if status == "submitting" and not _has_lease(doc):
        # Ambiguous, not dead: there is a real window in which a submission is
        # claimed but not yet leased. See state.IN_PROGRESS.
        return "ambiguous"

    if not is_stale(doc, now=now):
        return "alive"

    if status == "submitting":
        # The fork: whether a browser ever clicked, which only the marker
        # answers.
        return "release_uncertain" if doc.get(CLICKED_FIELD) else "release_unstarted"

    if attempts(doc) >= max_attempts:
        return "give_up"
    return "redispatch" if status == "queued" else "requeue"


def reap_one(
    ref,
    snap,
    doc: dict,
    verdict: Verdict,
    *,
    user_id: str,
    dispatch: DispatchFn,
    now: datetime,
) -> str:
    """Act on one verdict, carrying its precondition into every write.

    Returns the outcome to tally: the verdict itself when it was applied,
    ``"lost_race"`` when the document moved underneath us, or
    ``"not_dispatched"`` when the recovery landed but the work could not be
    scheduled.

    Claim, then act, then hand back on failure. The claim is
    :func:`state.try_claim_lease`, the only primitive here that checks status
    and lease inside one write, so losing it means someone else owns the
    document now. Every write after it carries ``allowed_from``, because
    ``try_transition`` retries once and that retry must not apply a decision
    made about a document that has since moved.
    """
    status = doc[state.STATUS_FIELD]
    owner = state.new_owner()
    # The counter rides in the claim itself. A claim that loses advances
    # nothing; a claim that wins advances it exactly once.
    bump = {ATTEMPTS_FIELD: attempts(doc) + 1} if verdict in DISPATCHING else None
    if not state.try_claim_lease(ref, snap, status, owner=owner, now=now, extra=bump):
        log.info("reaper.not_claimed", app_id=ref.id, status=status, verdict=verdict)
        return "lost_race"

    if verdict == "redispatch":
        # No status change: the document is already where the work belongs.
        # The claim above is the write, and the lease it leaves behind backs
        # the next pass off instead of re-dispatching hourly.
        return _dispatch_or_report(dispatch, user_id, doc, verdict)

    # From here every recovery is a status write naming the status it recovers
    # from, so try_transition's retry cannot apply it to a moved document.
    if verdict == "requeue":
        moved = state.try_transition(
            ref,
            ref.get(),
            "queued",
            note=REQUEUE_NOTE,
            allowed_from={"tailoring"},
            # A fresh queued lease rather than CLEAR_LEASE: it backs the next
            # pass off instead of letting it re-dispatch immediately.
            lease=state.lease_for("queued", owner=owner, now=now),
            # No counter here: the claim above already advanced it inside the
            # swap. Bumping again from the stale read would double-count.
        )
        if not moved:
            return _release(ref, owner, verdict)
        return _dispatch_or_report(dispatch, user_id, doc, verdict)

    if verdict == "give_up":
        note = GAVE_UP_NOTE
        extra = None
    else:
        # A release_* verdict. Re-read and decide the fork from that read:
        # try_claim_lease can succeed against a newer snapshot, and a stale
        # marker is the one staleness that could cost a duplicate real
        # application. Re-deciding can only move the verdict towards
        # "uncertain", since the marker is only ever set.
        doc = ref.get().to_dict() or doc
        verdict = "release_uncertain" if doc.get(CLICKED_FIELD) else "release_unstarted"
        uncertain = verdict == "release_uncertain"
        note = UNCERTAIN_NOTE if uncertain else NEVER_CLICKED_NOTE
        extra = {UNCERTAIN_FIELD: True} if uncertain else None

    if not state.try_transition(
        ref,
        ref.get(),
        "failed",
        note=note,
        allowed_from={status},
        lease=state.CLEAR_LEASE,
        extra=extra,
    ):
        return _release(ref, owner, verdict)

    if verdict == "release_unstarted":
        # try_transition's retry re-checks the status but not the marker, so a
        # zombie run that clicked in between could have been told "nothing was
        # submitted". Re-read and correct; the document is already ``failed``,
        # so this only adds the flag and a second note.
        if (ref.get().to_dict() or {}).get(CLICKED_FIELD):
            log.warning("reaper.clicked_after_release", app_id=ref.id)
            ref.update({UNCERTAIN_FIELD: True})
            state.append_note(ref, "failed", UNCERTAIN_NOTE)
            return "release_uncertain"

    log.info("reaper.released", app_id=ref.id, status=status, verdict=verdict)
    return verdict


def _release(ref, owner: str, verdict: Verdict) -> str:
    """Hand back the claim a recovery took but could not use.

    The document moved between the claim and the write, so the claim now sits
    on someone else's status and would block them for its whole TTL.
    ``release_lease`` refuses unless the lease is provably ours.
    """
    state.release_lease(ref, ref.get(), owner)
    log.info("reaper.lost_race", app_id=ref.id, verdict=verdict)
    return "lost_race"


def _dispatch_or_report(
    dispatch: DispatchFn, user_id: str, doc: dict, verdict: Verdict
) -> str:
    """Schedule the tailoring run a recovery has just made room for.

    Never rolled back: an enqueue can report failure and still have created
    the task, so clearing the claim would free a run that may already have
    started. The document keeps its ``queued`` lease, that lease expires, and
    the next pass retries with the attempt counter already advanced.
    """
    job_id = doc.get("job_id")
    if not job_id:
        log.warning("reaper.no_job_id", app_id=doc.get("id"))
        return "not_dispatched"
    try:
        scheduled = dispatch(user_id, job_id)
    except Exception:
        log.exception("reaper.dispatch_failed", job_id=job_id, verdict=verdict)
        return "not_dispatched"
    if not scheduled:
        log.info("reaper.dispatch_deduped", job_id=job_id, verdict=verdict)
        return "not_dispatched"
    log.info("reaper.dispatched", job_id=job_id, verdict=verdict)
    return verdict


def reap_applications(
    user_id: str,
    *,
    dispatch: DispatchFn,
    db=None,
    now: datetime | None = None,
    execute: bool = True,
    max_attempts: int = MAX_ATTEMPTS,
    max_per_pass: int = MAX_PER_PASS,
) -> dict[str, int]:
    """One recovery pass over a user's in-progress applications.

    Returns a tally keyed by outcome, plus ``scanned``, ``recovered`` (the
    number of documents actually moved) and ``truncated`` (1 when the pass hit
    :data:`MAX_PER_PASS` and left work behind).

    ``execute=False`` classifies and reports without taking a lease, writing a
    field or dispatching anything: the whole read path and none of the write
    path.

    Synchronous, because ``state.try_transition`` is; callers on an event loop
    hand this to ``asyncio.to_thread``.
    """
    now = now or _now()
    db = db or firestore.Client()
    apps_ref = db.collection("users").document(user_id).collection("applications")

    tally: dict[str, int] = dict.fromkeys(
        (
            "scanned",
            "alive",
            "ambiguous",
            "redispatch",
            "requeue",
            "give_up",
            "release_unstarted",
            "release_uncertain",
            "lost_race",
            "not_dispatched",
            "errors",
            "truncated",
        ),
        0,
    )

    # A single-field ``in`` filter: no composite index, and the three statuses
    # are the whole of state.IN_PROGRESS. ``limit(max_per_pass + 1)`` reads one
    # document past the budget so "there is more" is read rather than inferred
    # from ``scanned == max_per_pass``; the extra is discarded, never acted on.
    query = apps_ref.where(
        filter=FieldFilter(state.STATUS_FIELD, "in", REAPABLE)
    ).limit(max_per_pass + 1)
    batch = list(query.stream())
    if len(batch) > max_per_pass:
        batch = batch[:max_per_pass]
        tally["truncated"] = 1
        # WARNING, not info: the backlog this pass could not reach is
        # invisible anywhere else.
        log.warning("reaper.truncated", user_id=user_id, limit=max_per_pass)
    for snap in batch:
        doc = snap.to_dict() or {}
        tally["scanned"] += 1
        try:
            verdict = classify(doc, now=now, max_attempts=max_attempts)
            if not execute or verdict in ("alive", "ambiguous"):
                tally[verdict] += 1
                continue
            outcome = reap_one(
                snap.reference,
                snap,
                doc,
                verdict,
                user_id=user_id,
                dispatch=dispatch,
                now=now,
            )
            tally[outcome] += 1
        except Exception:
            # One malformed or contended document must not abandon the pass.
            tally["errors"] += 1
            log.exception("reaper.document_failed", app_id=snap.id)

    tally["recovered"] = sum(
        tally[key]
        for key in (
            "redispatch",
            "requeue",
            "give_up",
            "release_unstarted",
            "release_uncertain",
        )
    )
    log.info("reaper.done", user_id=user_id, execute=execute, **tally)
    return tally
