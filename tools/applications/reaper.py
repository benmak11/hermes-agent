# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Collect applications whose worker died, and give the user a way forward.

Every in-progress status is claimed by a process that can be killed mid-run —
a Cloud Run eviction, a revision rollout, an instance scaled to zero. When that
happens the terminal write in ``run_tailoring``/``run_submission``'s ``except``
never executes, and the document sits in ``queued``, ``tailoring`` or
``submitting`` forever: ``submit`` and ``regenerate`` 409 out of ``submitting``,
the undo path refuses to delete it, and the liveness sweep spares it. This is
the scheduled pass ``cli/unwedge_submitting`` calls "the real fix", and the
thing ``state.IN_PROGRESS`` leases were shaped for.

**The whole module is one read-decide-write loop, which is the shape that has
gone wrong in every PR of this phase.** So: no decision made here is acted on
outside the swap that re-checks it. Every recovery starts with
:func:`state.try_claim_lease` — which compare-and-swaps *status and lease
together*, re-reading both on its retry — and every status write that follows
carries ``allowed_from``. A document that moved between the read and the write
is left exactly where it is, and the next pass re-decides it from scratch.

Staleness: the lease, not the age
---------------------------------
For a document that has a lease, an unexpired lease means a process is running
it and nothing here may touch it, whatever the timestamps say. Age is inference;
a lease is first-hand evidence from the process doing the work. The one status
decided by age is ``queued``, because nothing claims it in the ordinary flow
(``run_tailoring`` claims by leaving it), so there is no lease to read until the
reaper writes one itself.

The apply fork, which is the safety property here
-------------------------------------------------
A dead ``tailoring`` run costs ~$0.002 to redo, so it is retried automatically.
A dead ``submitting`` run cannot be, because a crash *after* the Submit click
may have filed a real application at a real company, and retrying files a second
one. Nothing undoes that. So ``submitting`` forks on ``submit_attempted_at`` —
written by ``run_submission``'s ``progress`` callback the instant the submitter
reports :data:`tools.submitters.SUBMIT_CLICKED`, immediately before the click:

- **no marker** — the browser never clicked, so nothing was sent. Released to
  ``failed``, which is the status the user can act on, with a note saying so.
- **marker present** — the outcome is unknown. Released to ``failed`` with
  ``submission_uncertain`` set and a note telling the user to check their email
  before retrying. **Never re-enqueued, at any attempt count.** This is what
  the ``hermes-apply`` queue's ``max_attempts = 1`` exists to protect, and the
  reaper must not become the thing that undoes it.

``failed`` for both, deliberately. ``submitting → ready_for_review`` is *not* an
edge in ``state.TRANSITIONS`` and must not become one: ``run_tailoring``
publishes its result with a bare ``→ ready_for_review`` and no ``allowed_from``,
so opening that edge would let a slow duplicate tailoring run move a *live*
submission back to reviewable and clear the submitter's lease — inviting exactly
the duplicate application this fork exists to prevent. ``failed`` is also what
``_abandon_unstarted_claim`` already writes for the same "claimed but nothing
clicked" fact. The two branches are told apart by the note and by
``submission_uncertain``, which is a plain boolean rather than a new
``ApplicationStatus``: ``web/`` renders a closed union and would show an unknown
status as "failed — open to retry", which is precisely the wrong thing to say
about a submission that may have gone through.

Unleased ``submitting`` is not reaped at all
--------------------------------------------
``state.IN_PROGRESS`` spells out why: ``POST /applications/{id}/submit`` writes
the status and the run writes the lease, so an unleased ``submitting`` document
may simply be one whose worker has not picked the task up yet. Absence of a
lease there is **ambiguous, not dead**, and this pass reports it and moves on —
the age arithmetic in ``cli/unwedge_submitting`` is where that judgement lives,
because it is a judgement, and it belongs to an operator rather than to an
hourly job. Contrast ``tailoring``, where status and lease are written together
and an absent lease means only a document predating leases.
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

#: Documents one pass may look at, per user. **A latency bound, not a policy.**
#:
#: This pass runs in-request, serialised behind every other user's, inside the
#: hourly ``cron_tick`` that Cloud Scheduler gives ~180s before it retries the
#: whole fan-out. A recovered document costs a claim, a re-read, a transition and
#: an enqueue — 4-5 round trips, ~200ms — so 25 caps one user's contribution at
#: ~5s even when every document it sees is stale.
#:
#: The run that needs the cap is the **first one after deploy**, when every
#: ``queued``/``tailoring``/``submitting`` document accumulated since the funnel
#: existed is simultaneously past the age floor. Unbounded, a few hundred of
#: those overrun the deadline, the scheduler retries, and the tick restarts
#: having finished nothing. Worse, they would all be re-dispatched at once into a
#: ``tailor`` queue provisioned at 1 dispatch/second, and a backlog that takes
#: longer than the ``queued`` lease to drain gets re-dispatched on the next tick
#: — three ticks of that and the reaper starts failing work the queue was
#: processing correctly. A per-pass cap turns that cliff into a drip.
#:
#: The cap applies to documents **scanned**, not recovered, because that is what
#: bounds the work: it is the safe direction (a pass full of live leases costs
#: almost nothing and simply looks at fewer documents). Truncation is reported in
#: the tally rather than swallowed — a pass that ran out of budget looks exactly
#: like a pass with nothing to do, and those must not be confusable.
#:
#: There is deliberately no ``order_by``: any ordering here would need a
#: composite index, and the single-field query is the reason this pass needs no
#: index at all. Firestore returns a stable key-ordered prefix, so a large
#: backlog drains over successive ticks rather than fairly — acceptable, and
#: visible through ``truncated``.
MAX_PER_PASS = 25

#: Counts recoveries performed on this document **since the last time it worked
#: or the user asked again**. Written *inside* the compare-and-swap that performs
#: one, never beside it — a bump that outlives a claim that lost would let two
#: passes share an attempt number, and the cap would stop bounding anything.
#:
#: **It has an epoch, and needs one.** The cap bounds *consecutive* failures, so
#: two swaps in ``api.routes.applications`` clear it: ``run_tailoring``'s
#: ``→ ready_for_review`` publish (the pipeline demonstrably works for this
#: document) and ``regenerate``'s ``→ queued`` (the user asked again). Without
#: that, the count is a lifetime total: an application recovered three times
#: during a queue outage and then tailored perfectly stays one stale tick away
#: from :data:`GAVE_UP_NOTE` forever — and that give_up dispatches *nothing*
#: while telling the user to press Regenerate, which is the button that cannot
#: help. A closed loop with a wrong instruction in it.
#:
#: Deliberately *not* ``submit_attempts``: that counter names the apply task, and
#: resetting or double-bumping it dedupes a real submission into silence.
ATTEMPTS_FIELD = "reap_attempts"

#: Set when a submission died with the Submit click already behind it. Backend
#: only — nothing in ``web/`` reads it, and nothing should have to for the user
#: to be safe, because the note beside it says the same thing in words.
UNCERTAIN_FIELD = "submission_uncertain"

#: Written by ``run_submission``'s progress callback at the point of no return.
#: Read here and nowhere else.
CLICKED_FIELD = "submit_attempted_at"

#: Notes are rendered verbatim by ``web/``, so these are user-facing copy. There
#: is deliberately no note for a re-dispatch: it changes no status, the document
#: reads "queued" before and after, and an hourly entry saying so would bury the
#: timeline the user actually needs under bookkeeping. The log line carries it.
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

#: What one document's inspection concludes. Every one of these is counted and
#: reported: a document this pass declines to act on stays stuck, so silence is
#: never the right answer.
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

    The newest timeline entry, which is the *only* general answer: every status
    write appends one, and so does every progress note, so a run that is alive
    and chattering looks recent even when it started long ago.
    ``cli/unwedge_submitting`` measures from ``last_submitted_at`` instead
    because it is answering a narrower question ("how long since the submit
    request"), and the two are deliberately not shared.
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

    ``isinstance``, not ``in``: a ``lease`` field holding a non-dict is not a
    claim that expired, it is a document nothing here understands, and
    ``state.lease_is_held`` reads it as *unheld* — which on ``submitting`` would
    turn "unreadable" into "free to fail". Treating it as no lease at all sends
    it down the ambiguous path instead, which is the safe one.
    """
    return isinstance(doc.get(state.LEASE_FIELD), dict)


def is_stale(doc: dict, *, now: datetime) -> bool:
    """Has whatever was working on this document stopped? Pure.

    A live lease always wins, and an unreadable one counts as live
    (``state.lease_is_held`` is asymmetric on purpose). Where a lease exists it
    is the *whole* answer: it is written by the process doing the work and
    expires on a clock deliberately longer than the longest run it can guard, so
    consulting the age as well would only delay collecting a document whose
    owner has already been declared dead.

    Age is the fallback for a document with no lease at all, which after the
    checks in :func:`classify` means ``queued`` (nothing claims it) or a
    ``tailoring`` document predating leases. Unknown age counts as stale: it
    means no timeline at all, which no document written by this build has.
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

    Split out from the writing so the whole recovery table is unit-testable
    without Firestore *and* so the dry run reports exactly what an execute would
    attempt. The verdict is still re-checked by the swap that acts on it; this
    decides, it does not authorise.
    """
    status = doc.get(state.STATUS_FIELD)
    if status not in REAPABLE:
        # The query asked for these three, so this is a document that moved
        # between the query and the read, or a legacy value the table doesn't
        # know. Either way: not ours.
        return "alive"

    if status == "submitting" and not _has_lease(doc):
        # Ambiguous, not dead — see the module docstring and state.IN_PROGRESS.
        # There is a real window in which a submission is claimed but not yet
        # leased, and reading that as "the owner is gone" is how an application
        # that is about to be sent gets marked failed.
        return "ambiguous"

    if not is_stale(doc, now=now):
        return "alive"

    if status == "submitting":
        # **The fork.** The only question that matters is whether a browser
        # ever clicked, and only the marker can answer it.
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

    **Claim, then act, then hand back on failure.** The claim is
    :func:`state.try_claim_lease`, which is the only primitive here that checks
    the status *and* the lease inside one write and re-reads both on its retry —
    so losing it means "someone else owns this now", and the correct response is
    to do nothing. Anything written afterwards additionally carries
    ``allowed_from``, because ``try_transition`` retries once and that retry
    must not be able to apply a decision made about a document that has since
    moved.
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
        # No status change: ``queued → queued`` is not an edge, and there is
        # nothing to change — the document is already where the work belongs.
        # The claim above *is* the write, and the lease it leaves behind is the
        # back-off: the next pass finds it held and waits instead of
        # re-dispatching hourly.
        return _dispatch_or_report(dispatch, user_id, doc, verdict)

    # From here every recovery is a status write, and every one of them names
    # the status it is recovering from so try_transition's retry cannot apply it
    # to a document that has since moved on.
    if verdict == "requeue":
        moved = state.try_transition(
            ref,
            ref.get(),
            "queued",
            note=REQUEUE_NOTE,
            allowed_from={"tailoring"},
            # A fresh queued lease rather than CLEAR_LEASE, for the same reason
            # redispatch leaves one: it backs the next pass off for a lease's
            # worth of time instead of letting it re-dispatch immediately.
            lease=state.lease_for("queued", owner=owner, now=now),
            # No counter here: the claim above already advanced it, inside the
            # swap that proved this recovery was ours to make. Bumping again
            # from the stale read would be a second writer of the one field the
            # cap depends on.
        )
        if not moved:
            return _release(ref, owner, verdict)
        return _dispatch_or_report(dispatch, user_id, doc, verdict)

    if verdict == "give_up":
        note = GAVE_UP_NOTE
        extra = None
    else:
        # A release_* verdict. Re-read now the claim has landed and decide the
        # fork from *that* read: try_claim_lease can succeed against a snapshot
        # one write newer than the one this function was handed, and the marker
        # is the single field whose staleness could cost a duplicate real
        # application. Re-deciding costs one read and can only move the verdict
        # towards "uncertain", never away from it — the marker is only ever set.
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
        # Belt and braces on the one irreversible mistake this module can make.
        # try_transition retries once on a lost precondition, and that retry
        # re-checks the status but not the marker — so a zombie run that clicked
        # in the window between the read above and the write could have been
        # told "nothing was submitted". Re-read and correct: the document is
        # already ``failed``, so this only adds the flag and a second note.
        if (ref.get().to_dict() or {}).get(CLICKED_FIELD):
            log.warning("reaper.clicked_after_release", app_id=ref.id)
            ref.update({UNCERTAIN_FIELD: True})
            state.append_note(ref, "failed", UNCERTAIN_NOTE)
            return "release_uncertain"

    log.info("reaper.released", app_id=ref.id, status=status, verdict=verdict)
    return verdict


def _release(ref, owner: str, verdict: Verdict) -> str:
    """Hand back the claim a recovery took but could not use.

    The document moved between the claim and the write, so the claim now sits on
    someone else's status and would block them for its whole TTL. Same shape as
    ``_abandon_unstarted_claim``'s tail: ``release_lease`` refuses unless the
    lease is provably ours.
    """
    state.release_lease(ref, ref.get(), owner)
    log.info("reaper.lost_race", app_id=ref.id, verdict=verdict)
    return "lost_race"


def _dispatch_or_report(
    dispatch: DispatchFn, user_id: str, doc: dict, verdict: Verdict
) -> str:
    """Schedule the tailoring run a recovery has just made room for.

    **Never rolled back.** PR C's lesson, and it applies with more force here: an
    enqueue can report failure and still have created the task, so undoing the
    claim on a failed dispatch would clear the lease of a run that may already
    have started. The document keeps its ``queued`` lease, that lease expires,
    and the next pass tries again with the attempt counter already advanced —
    which is the bound that stops a broken queue from looping forever.
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

    ``execute=False`` classifies and reports without taking a single lease,
    writing a single field, or dispatching anything — the dry run has to be the
    *whole* read path and none of the write path, because a dry run that acts is
    how PR B shipped a bug.

    Synchronous, like ``cli/unwedge_submitting`` and for the same reason:
    ``state.try_transition`` is synchronous, going through it is non-negotiable,
    and callers on an event loop hand this to ``asyncio.to_thread``.
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
    # are the whole of state.IN_PROGRESS.
    #
    # ``limit(max_per_pass + 1)``: one document past the budget, so "there is
    # more" is a fact this pass *read* rather than one it infers from
    # ``scanned == max_per_pass``, which cannot tell a full pass from an exactly
    # full one. The extra document is discarded, never acted on.
    query = apps_ref.where(
        filter=FieldFilter(state.STATUS_FIELD, "in", REAPABLE)
    ).limit(max_per_pass + 1)
    batch = list(query.stream())
    if len(batch) > max_per_pass:
        batch = batch[:max_per_pass]
        tally["truncated"] = 1
        # WARNING, not info: the backlog this pass could not reach is invisible
        # anywhere else — a stuck application looks exactly like an idle one.
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
            # One malformed or contended document must not abandon the pass —
            # the rest of this user's stuck applications are still stuck.
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
