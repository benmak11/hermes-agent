# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The application lifecycle as a state machine, with exactly one writer.

Every status the funnel goes through used to be written by a blind
``ref.set(..., merge=True)`` scattered across ``api/routes``. Two of those
writes were races with real consequences: a double-click on Submit had both
requests read ``ready_for_review``, both pass the check, and both start a live
ATS submission — a **duplicate real job application**. This module makes that
impossible by making one function the only place a ``status`` is ever written,
and making that function a compare-and-swap.

There is a second compare-and-swap here, :func:`try_claim_lease`, for the
question the status can't answer: **which process is running this right now.**
The status claim is taken by the API request (a double-click loses it); the
lease claim is taken by the worker that receives the task, so a redelivered
Cloud Task finds a live lease and does nothing instead of starting a second
live ATS submission.

That last sentence is only true because :data:`IN_PROGRESS` outlives the
dispatch deadline — see the note on it. It is also, today, *not* the thing
keeping duplicate submissions out of production: the ``hermes-apply`` queue is
provisioned with ``max_attempts = 1`` (deployment/terraform/single-project/
worker.tf), so a failed apply task is never redelivered at all. The lease is
what makes the code correct if that ever changes, and what a reaper can read;
the queue setting is what is load-bearing right now.

**The mechanism is the update-time precondition**, the same one
``tools.matching.batch_runs`` uses to stop racing resumers from double-
submitting a paid Pro batch: read a snapshot, then write with
``last_update_time=snap.update_time`` so the write fails if anything touched
the document in between. The loser gets ``FailedPrecondition``, re-reads, finds
the status has moved on, and discovers its transition is no longer legal.

**Legality is a table, not a set of ``if``s.** :data:`TRANSITIONS` is the whole
contract. Two properties fall out of it for free:

- A terminal status has no outgoing edges, so nothing can revive it.
- An **unknown status has no outgoing edges either** — ``TRANSITIONS.get`` on a
  legacy or hand-edited value returns the empty set, so no code path can act on
  a document it doesn't understand while the UI still renders it. That is the
  backward-compatibility story: no migration, no ``normalize()``.

**What this table is not.** It covers ``ApplicationStatus`` values only. The
``pending → scored → approved`` sequence is *not* in here: those are facts about
the **Job** document (``user_decision`` plus the presence of ``match``), and no
document holds both. Approval is the entry event that *creates* an Application
in :data:`INITIAL`; a table spanning both would be a fake abstraction over two
unrelated documents.

Shape is modelled on ``tools.matching.budget``: pure logic that unit-tests
against fakes, plus one thin Firestore-touching function.

Synchronous on purpose — every caller (``api/routes/applications.py``,
``api/routes/jobs.py``) holds a synchronous ``firestore.Client`` reference, and
two of the call sites are synchronous FastAPI routes that FastAPI already runs
in a threadpool. An ``async def`` here would force those routes onto the event
loop where their blocking Firestore reads would stall it.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import uuid4

from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore

from obs.logging import get_logger

# The lease TTL is defined *against* the dispatch deadline (see IN_PROGRESS), so
# it is imported rather than restated. tools.queues imports nothing from here.
from tools.queues import _DISPATCH_DEADLINE_SECONDS

log = get_logger("tools.applications")

# ``write_option`` is a *staticmethod* factory — it builds a precondition and
# never touches a client or the network. Bound once here so this module needs no
# client instance to construct one, and so a test that swaps out
# ``firestore.Client`` (to fake a collection) can't take the precondition with it.
_precondition = firestore.Client.write_option

# Fields this module owns. Nothing outside it may write them, and a content
# write (the tailoring result) must strip them before merging — a blanket
# ``set()`` of the model used to wipe the whole timeline.
STATUS_FIELD = "status"
TIMELINE_FIELD = "timeline"
LEASE_FIELD = "lease"
OWNED_FIELDS = (STATUS_FIELD, TIMELINE_FIELD, LEASE_FIELD)

#: The status every Application is created in. Approving a job enqueues the
#: work; the background tailoring task *claims* it by moving queued → tailoring,
#: so an application that never got picked up is visibly still queued rather
#: than indistinguishable from one whose worker died mid-run.
INITIAL = "queued"

#: The whole lifecycle contract. Keys and values are ``ApplicationStatus``
#: values from ``models/application.py`` and nothing else.
#:
#: ``→ posting_removed`` hangs off *every* non-terminal status because the
#: posting dying is an external fact discovered by whoever looks (the liveness
#: sweep in ``tools.ats.sweep``, or the pre-flight check in tailoring and
#: submission), not a step in the flow. ``tools.ats.sweep.ACTIVE_APP_STATUSES``
#: is what decides which of those a *background* sweep may act on — it
#: deliberately spares ``submitting`` so a sweep can't yank a document out from
#: under a browser mid-submit.
TRANSITIONS: dict[str, frozenset[str]] = {
    # The tailoring task claims its work by moving out of queued.
    "queued": frozenset({"tailoring", "failed", "posting_removed"}),
    "tailoring": frozenset({"ready_for_review", "failed", "posting_removed", "queued"}),
    # → queued is "regenerate": the user asks for another tailoring pass.
    "ready_for_review": frozenset({"submitting", "queued", "posting_removed"}),
    "failed": frozenset({"submitting", "queued", "posting_removed"}),
    "submitting": frozenset({"submitted", "failed", "posting_removed"}),
    "submitted": frozenset({"responded"}),
    "responded": frozenset(),
    "posting_removed": frozenset(),
}

#: Statuses nothing can leave. Derived, so it cannot drift from the table.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    status for status, outgoing in TRANSITIONS.items() if not outgoing
)

#: Seconds a claim stays valid, and **the inequality that makes it a lock**:
#: a lease must outlive the longest run it guards, or it stops being one.
#:
#: Cloud Tasks caps dispatch at ``_DISPATCH_DEADLINE_SECONDS`` (1800) and the
#: worker's ``timeoutSeconds`` matches, so 1800s is the longest a task can run
#: before Cloud Run kills it and the queue may redeliver. An earlier version of
#: this file had the inequality backwards — a 1200s lease against 1800s of work
#: — which is worse than no lease at all: worker A is killed at T+1800 with the
#: browser possibly *past* the Submit click, the retry lands at T+1860, reads
#: an expired lease, claims it, and files the application a second time. The
#: grace is added on top of the deadline rather than subtracted from it, and
#: the value is derived so the two cannot drift apart.
_LEASE_GRACE_SECONDS = 60
_LEASE_SECONDS = _DISPATCH_DEADLINE_SECONDS + _LEASE_GRACE_SECONDS

#: The statuses that mean "a process is supposed to be working on this right
#: now". Uniform: each one is claimed by a task subject to the same dispatch
#: deadline, so each needs the same floor. ``queued`` carries one for the
#: reaper's benefit — nothing claims that status today.
#:
#: ``tools.applications.reaper`` is what expires these. Three paths write a
#: lease: :func:`try_claim_lease` (``run_submission``'s delivery claim on
#: ``submitting``, and the reaper's own claim on all three), ``run_tailoring``'s
#: ``queued → tailoring`` claim, which takes status and lease in one write, and
#: the reaper's ``→ queued`` recovery, which lands a fresh ``queued`` lease so
#: the next pass backs off instead of re-dispatching hourly.
#:
#: **The asymmetry the reaper is built on.** For ``tailoring`` the status and
#: the lease are written together, so an absent lease there means a document
#: predating leases entirely. For ``submitting`` they are written by two
#: different processes: ``POST /applications/{id}/submit`` writes the status and
#: the run writes the lease, so there is a real window between the two — and,
#: when the dispatch between them fails outright, a document that stays there.
#: Every path that can drive a submission does take this lease (the claim lives
#: in ``run_submission`` itself, not in the ``/tasks/apply`` handler, precisely
#: so that is true of the in-process path too), but "submitting and no lease" is
#: **ambiguous, not dead**: it must fall back to the age arithmetic in
#: ``cli/unwedge_submitting`` and must never be read as "the owner is gone". The
#: reaper honours that by refusing to touch an unleased ``submitting`` document
#: at all — see its module docstring.
#:
#: ``queued`` is the exception to "the lease decides": nothing claims that
#: status in the ordinary flow, so the reaper decides its staleness by age and
#: then takes this lease itself, as its own re-dispatch bookkeeping.
IN_PROGRESS: dict[str, int] = {
    "queued": _LEASE_SECONDS,
    "tailoring": _LEASE_SECONDS,
    "submitting": _LEASE_SECONDS,
}


class _Sentinel(Enum):
    """Sentinel type for :data:`CLEAR_LEASE` (an enum so it types cleanly)."""

    CLEAR_LEASE = "clear_lease"


#: Pass as ``lease=`` to delete the lease field in the same write as the status.
CLEAR_LEASE = _Sentinel.CLEAR_LEASE

Lease = dict | _Sentinel | None


def _now() -> datetime:
    return datetime.now(UTC)


def can_transition(frm: str | None, to: str) -> bool:
    """Is ``frm → to`` a legal edge?

    ``frm`` may be ``None`` (a document with no status) or an unrecognised
    legacy value; both have no outgoing edges, which is the point.
    """
    return to in TRANSITIONS.get(frm or "", frozenset())


def timeline_event(status: str, note: str | None = None) -> dict:
    """One ``timeline`` entry, in the shape ``web/`` already renders.

    ``note`` is omitted rather than written as ``None`` so entries are
    byte-identical to the ones the routes wrote before this module existed.
    """
    event = {"at": _now().isoformat(), "status": status}
    if note is not None:
        event["note"] = note
    return event


def new_owner() -> str:
    """A fresh lease owner token. One per run, not one per process."""
    return uuid4().hex


def lease_for(
    status: str, *, owner: str | None = None, now: datetime | None = None
) -> dict | None:
    """The lease an in-progress ``status`` should carry, or ``None``.

    ``expires_at`` is what a reaper compares against: past it, the claiming
    process is presumed dead and the application may be failed or re-queued.

    ``owner`` names *which* run holds it, and exists so a release can be
    checked rather than assumed. Without it, an expiry lets two runs believe
    they hold the same document: B claims after A's lease lapses, then A —
    alive, merely slow — finishes and clears the lease, freeing the document
    for a third claim while B is still working. :func:`release_lease` refuses
    that. Optional in the *stored* shape on purpose: documents written before
    this field existed must still read back as valid leases.
    """
    seconds = IN_PROGRESS.get(status)
    if seconds is None:
        return None
    now = now or _now()
    lease = {
        "status": status,
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=seconds)).isoformat(),
    }
    if owner is not None:
        lease["owner"] = owner
    return lease


def _lease_expiry(lease) -> datetime | None:
    """``lease['expires_at']`` as an aware datetime, or ``None`` if unusable."""
    if not isinstance(lease, dict):
        return None
    value = lease.get("expires_at")
    if isinstance(value, datetime):  # Firestore hands timestamps back as these
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def lease_is_held(doc: dict, *, now: datetime | None = None) -> bool:
    """Is someone currently claiming this document? Pure.

    A lease whose ``expires_at`` can't be read counts as **held**. The bias is
    deliberate and asymmetric: refusing to claim leaves the document wedged
    (which ``cli/unwedge_submitting`` and, later, the reaper can undo), while
    claiming anyway risks a duplicate real job application, which nothing can
    undo.
    """
    lease = doc.get(LEASE_FIELD)
    if not isinstance(lease, dict):
        return False
    expiry = _lease_expiry(lease)
    if expiry is None:
        return True
    return expiry > (now or _now())


def lease_owner(doc: dict) -> str | None:
    """Who holds this document's lease, or ``None`` if it says (or has) nothing."""
    lease = doc.get(LEASE_FIELD)
    return lease.get("owner") if isinstance(lease, dict) else None


def try_claim_lease(
    ref,
    snap,
    status: str,
    *,
    owner: str,
    now: datetime | None = None,
    extra: dict | None = None,
) -> bool:
    """Compare-and-swap the **lease** of a document already in ``status``.

    The second compare-and-swap in this module, and the one that makes an
    at-least-once task delivery safe. A status transition can't do this job:
    ``POST /applications/{id}/submit`` already claims the work by CAS-ing
    ``→ submitting`` (that is what a double-click loses on), so by the time the
    run starts the status *is* the claim and there is no second edge left to
    take — ``submitting → submitting`` is illegal, exactly as it must be.
    Claiming the lease instead splits the two questions cleanly: **who owns this
    application** (the status, claimed by the API request) versus **who is
    running it right now** (the lease, claimed by the run — meaning
    ``run_submission`` itself, so that a queued task and an in-process
    background task are fenced by the same primitive rather than only one of
    them holding a claim).

    Returns ``True`` when this caller took the lease. ``False`` means: the
    document is gone, its status moved on, someone else's lease is still live,
    or the write lost its precondition twice — in every case the caller's
    correct response is to do nothing, which is what makes a redelivered task a
    no-op rather than a second live ATS submission.

    Every terminal write on the claiming path passes :data:`CLEAR_LEASE`, so a
    run that finishes releases the lease in the same write that records its
    outcome; :func:`release_lease` covers the case where that terminal write
    lost its race and the lease would otherwise be left behind. A run that
    *dies* leaves the lease to expire on the :data:`IN_PROGRESS` clock — a
    wedged document, which is the safe failure.

    ``owner`` is stamped on the lease so the release can be checked rather than
    assumed; :func:`new_owner` mints one per run.

    ``extra`` carries fields that must land **in the same write as the claim**,
    the same contract :func:`try_transition` offers, and it may not contain
    :data:`OWNED_FIELDS`. It exists for the reaper's retry counter: the counter
    is what bounds an automatic re-dispatch loop, so a claim that loses must not
    advance it and a claim that wins must advance it exactly once — which is
    only true if the claim and the bump are one write. This is the same reason
    ``submit_attempts`` rides inside the ``→ submitting`` swap rather than
    beside it. On the ``queued`` claim it is the *only* payload of consequence:
    nothing else claims that status, so the reaper's own bookkeeping is all
    there is to protect.

    Retries once on a lost precondition for the same reason
    :func:`try_transition` does (``_backfill_job_url`` writes on read), and
    re-reads the status and the lease on that retry rather than trusting the
    first read.
    """
    if status not in IN_PROGRESS:
        raise ValueError(f"{status!r} carries no lease; see state.IN_PROGRESS")
    # Validated before the loop: a clash is a programming error, not a race, and
    # it should raise whether or not the first attempt reaches the network.
    _reject_owned(extra)
    for attempt in (0, 1):
        if not snap.exists:
            return False
        doc = snap.to_dict() or {}
        current = doc.get(STATUS_FIELD)
        if current != status:
            log.info(
                "application.lease_wrong_status",
                app_id=getattr(ref, "id", None),
                status=current,
                wanted=status,
                attempt=attempt,
            )
            return False
        if lease_is_held(doc, now=now):
            log.info(
                "application.lease_held",
                app_id=getattr(ref, "id", None),
                status=current,
                lease=doc.get(LEASE_FIELD),
                attempt=attempt,
            )
            return False
        try:
            payload = _reject_owned(extra)
            payload[LEASE_FIELD] = lease_for(status, owner=owner, now=now)
            ref.update(
                payload,
                option=_precondition(last_update_time=snap.update_time),
            )
            return True
        except NotFound:
            log.info(
                "application.lease_missing",
                app_id=getattr(ref, "id", None),
                wanted=status,
            )
            return False
        except FailedPrecondition:
            if attempt:
                log.warning(
                    "application.lease_contended",
                    app_id=getattr(ref, "id", None),
                    status=current,
                )
                return False
            snap = ref.get()
    return False  # pragma: no cover - the loop always returns


def release_lease(ref, snap, owner: str) -> bool:
    """Drop a lease **this** caller holds, leaving everything else alone.

    The counterpart to :func:`try_claim_lease`, for the path where the run
    finished but its terminal ``try_transition`` lost — the status write carries
    :data:`CLEAR_LEASE`, so when it loses, the lease it would have cleared stays
    behind and blocks the next claim for its full TTL.

    Refuses when the lease belongs to someone else, or when it carries no
    ``owner`` at all: a lease we cannot prove is ours is one we might be about
    to steal from a live run, and letting it expire costs only time. Returns
    ``False`` for "nothing released" in every such case, including the ordinary
    one where the terminal write already cleared it.
    """
    for attempt in (0, 1):
        if not snap.exists:
            return False
        doc = snap.to_dict() or {}
        if lease_owner(doc) != owner:
            log.info(
                "application.lease_not_ours",
                app_id=getattr(ref, "id", None),
                owner=lease_owner(doc),
                attempt=attempt,
            )
            return False
        try:
            ref.update(
                {LEASE_FIELD: firestore.DELETE_FIELD},
                option=_precondition(last_update_time=snap.update_time),
            )
            return True
        except NotFound:
            return False
        except FailedPrecondition:
            if attempt:
                log.warning(
                    "application.lease_release_contended",
                    app_id=getattr(ref, "id", None),
                )
                return False
            snap = ref.get()
    return False  # pragma: no cover - the loop always returns


def creation_fields(*, note: str | None = None) -> dict:
    """Status + timeline for a brand-new Application.

    Creation is the one status write that isn't a transition — there is no
    prior document to compare against — so it lives here rather than being
    open-coded at the (single) call site in ``jobs.decide``.
    """
    return {
        STATUS_FIELD: INITIAL,
        TIMELINE_FIELD: [timeline_event(INITIAL, note)],
    }


def _reject_owned(extra: dict | None) -> dict:
    """``extra`` as a fresh payload dict, refusing any field this module owns."""
    if extra:
        clashes = sorted(set(extra) & set(OWNED_FIELDS))
        if clashes:
            raise ValueError(f"{', '.join(clashes)} may only be written by state.py")
    return dict(extra or {})


def append_note(ref, status: str, message: str, *, extra: dict | None = None) -> bool:
    """Append a timeline entry **without** touching the document's status.

    For progress chatter: the submitter emits a label per step ("Filling
    standard fields", "submitting"), which is a display string, not a lifecycle
    edge. ``update`` rather than ``set(merge=True)`` so a late note can't
    resurrect a document the undo path deleted.

    ``extra`` lands in the *same* write, under the same :data:`OWNED_FIELDS`
    guard as :func:`try_transition`'s. One progress note needs that:
    ``submit_attempted_at``, the point-of-no-return marker
    (``api.routes.applications.run_submission``'s ``progress``), which is the
    single fact the reaper's apply fork reads to decide whether a dead run may
    be retried. Written beside the note rather than with it, a crash between the
    two writes could leave the timeline saying the form was being submitted
    while the marker — the thing that stops an automatic re-submission — is
    missing. This is deliberately *not* a compare-and-swap: the marker must land
    whatever else has happened to the document.

    Nothing is lost by that. The only writer that ever *clears* the marker is
    the ``→ submitting`` swap in ``api.routes.applications.submit``, which runs
    in the API request before the apply task is dispatched — so it cannot race
    a note written by a browser that does not exist yet, and the two can only
    interleave in the one order that is correct.
    """
    payload = _reject_owned(extra)
    payload[TIMELINE_FIELD] = firestore.ArrayUnion([timeline_event(status, message)])
    try:
        ref.update(payload)
        return True
    except NotFound:
        return False


def _payload(to: str, note: str | None, lease: Lease, extra: dict | None) -> dict:
    payload = _reject_owned(extra)
    payload[STATUS_FIELD] = to
    payload[TIMELINE_FIELD] = firestore.ArrayUnion([timeline_event(to, note)])
    if lease is CLEAR_LEASE:
        payload[LEASE_FIELD] = firestore.DELETE_FIELD
    elif lease is not None:
        payload[LEASE_FIELD] = lease
    return payload


def try_transition(
    ref,
    snap,
    to: str,
    *,
    note: str | None = None,
    lease: Lease = None,
    extra: dict | None = None,
    allowed_from: Collection[str] | None = None,
) -> bool:
    """Compare-and-swap the application's status. The only writer of ``status``.

    Returns ``True`` when the transition was applied, ``False`` when it wasn't —
    illegal edge, missing document, or lost race. **It never raises for those**:
    losing is a normal outcome (the second click of a double-click loses), and
    every caller's correct response is "do nothing further", not "500".

    ``snap`` is the read this swap is conditioned on. ``extra`` carries fields
    that must land atomically with the status (screenshots, confirmation,
    ``last_submitted_at``); it may not contain :data:`OWNED_FIELDS`. ``lease``
    writes a claim alongside the status, or :data:`CLEAR_LEASE` to drop one.

    ``allowed_from`` narrows the table for **this one call**: the current status
    must be in it on *every* attempt, the retry's re-read included. A caller
    whose precondition is narrower than the table must pass it here rather than
    check it itself — **filtering outside the swap is not a compare-and-swap.**
    The liveness sweep is the case that proves it: it may invalidate a
    pre-submission application but must never touch one that is ``submitting``,
    and ``submitting → posting_removed`` is a legal edge (the submission path
    itself uses it). With the check outside, a sweep that read
    ``ready_for_review``, lost the precondition to a user clicking Submit, and
    retried would re-read ``submitting``, find the edge legal, and mark the
    posting removed *while a browser was mid-submit* — losing the confirmation
    evidence for an application the user really did send.

    Uses ``update``, never ``set``: an application deleted by the undo path in
    ``jobs.decide`` must stay deleted, and ``set`` would recreate it from a
    write that was already in flight.

    **One retry.** ``_backfill_job_url`` writes on read, so a plain
    ``GET /applications`` concurrent with a transition bumps ``update_time`` and
    fails the precondition without changing anything. Re-reading and retrying
    once absorbs that; crucially the retry re-checks legality against the *new*
    status, so a genuine race (the other click already moved us to
    ``submitting``) fails on the table instead of overwriting the winner.
    """
    for attempt in (0, 1):
        if not snap.exists:
            return False
        current = (snap.to_dict() or {}).get(STATUS_FIELD)
        # Re-checked on the retry, not just the first read — that is the whole
        # point of taking the caller's precondition instead of letting it
        # filter beforehand.
        if allowed_from is not None and current not in allowed_from:
            log.info(
                "application.transition_not_allowed_from",
                app_id=getattr(ref, "id", None),
                status=current,
                wanted=to,
                attempt=attempt,
            )
            return False
        if not can_transition(current, to):
            log.info(
                "application.transition_rejected",
                app_id=getattr(ref, "id", None),
                status=current,
                wanted=to,
            )
            return False
        try:
            ref.update(
                _payload(to, note, lease, extra),
                option=_precondition(last_update_time=snap.update_time),
            )
            return True
        except NotFound:
            # Deleted underneath us (undo). Nothing to transition.
            log.info(
                "application.transition_missing",
                app_id=getattr(ref, "id", None),
                wanted=to,
            )
            return False
        except FailedPrecondition:
            if attempt:
                log.warning(
                    "application.transition_contended",
                    app_id=getattr(ref, "id", None),
                    status=current,
                    wanted=to,
                )
                return False
            snap = ref.get()
    return False  # pragma: no cover - the loop always returns
