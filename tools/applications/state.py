# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The application lifecycle as a state machine, with exactly one writer.

:func:`try_transition` is the only place an application's ``status`` is ever
written, and it is a compare-and-swap. Without that, a double-click on Submit
has both requests read ``ready_for_review``, both pass the check, and both
start a live ATS submission — a duplicate real job application.

:func:`try_claim_lease` is a second compare-and-swap, for the question the
status cannot answer: which process is running this right now. The status is
claimed by the API request; the lease is claimed by the run, so a redelivered
task finds a live lease and does nothing. That holds only because
:data:`IN_PROGRESS` outlives the dispatch deadline. What actually keeps
duplicates out of production today is ``max_attempts = 1`` on the
``hermes-apply`` queue; the lease is what keeps the code correct if that
changes, and what the reaper reads.

The mechanism is the update-time precondition: read a snapshot, write with
``last_update_time=snap.update_time``, and the loser gets
``FailedPrecondition``, re-reads, and finds its transition no longer legal.

Legality is a table, :data:`TRANSITIONS`, not a set of ``if``s. A terminal
status has no outgoing edges, and neither does an unknown one —
``TRANSITIONS.get`` on a legacy or hand-edited value returns the empty set, so
nothing can act on a document it does not understand. That is the whole
backward-compatibility story: no migration.

The table covers ``ApplicationStatus`` values only. ``pending → scored →
approved`` are facts about the *Job* document and are deliberately not here;
approval is the entry event that creates an Application in :data:`INITIAL`.

Synchronous on purpose: every caller holds a synchronous ``firestore.Client``,
and two call sites are sync FastAPI routes already run in a threadpool. An
``async def`` here would put their blocking reads on the event loop.
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

# ``write_option`` is a staticmethod factory: it builds a precondition and never
# touches a client or the network. Bound once here so this module needs no client
# instance, and so a test that swaps out ``firestore.Client`` cannot take it too.
_precondition = firestore.Client.write_option

# Fields this module owns. Nothing outside it may write them, and a content
# write must strip them before merging, or a blanket ``set()`` wipes the
# timeline.
STATUS_FIELD = "status"
TIMELINE_FIELD = "timeline"
LEASE_FIELD = "lease"
OWNED_FIELDS = (STATUS_FIELD, TIMELINE_FIELD, LEASE_FIELD)

#: The status every Application is created in. The tailoring task claims its
#: work by moving queued → tailoring, so an application that was never picked
#: up stays visibly queued instead of looking like one whose worker died.
INITIAL = "queued"

#: The whole lifecycle contract. Keys and values are ``ApplicationStatus``
#: values from ``models/application.py`` and nothing else.
#:
#: ``→ posting_removed`` hangs off every non-terminal status because the
#: posting dying is an external fact, not a step in the flow.
#: ``tools.ats.sweep.ACTIVE_APP_STATUSES`` decides which of those a background
#: sweep may act on; it spares ``submitting`` so a sweep cannot yank a document
#: out from under a browser mid-submit.
TRANSITIONS: dict[str, frozenset[str]] = {
    # The tailoring task claims its work by moving out of queued.
    "queued": frozenset({"tailoring", "failed", "posting_removed"}),
    "tailoring": frozenset({"ready_for_review", "failed", "posting_removed", "queued"}),
    # → queued is "regenerate": the user asks for another tailoring pass.
    # → submitted is the user saying they applied themselves (MANUAL_SUBMIT_FROM).
    "ready_for_review": frozenset(
        {"submitting", "submitted", "queued", "posting_removed"}
    ),
    "failed": frozenset({"submitting", "submitted", "queued", "posting_removed"}),
    "submitting": frozenset({"submitted", "failed", "posting_removed"}),
    "submitted": frozenset({"responded"}),
    "responded": frozenset(),
    "posting_removed": frozenset(),
}

#: Where the manual "I applied" edge may start. Every caller writing
#: ``→ submitted`` must pass ``allowed_from``: this set for the manual path,
#: ``{"submitting"}`` for the automated one, so neither can complete the other.
MANUAL_SUBMIT_FROM: frozenset[str] = frozenset({"ready_for_review", "failed"})

#: Statuses nothing can leave. Derived, so it cannot drift from the table.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    status for status, outgoing in TRANSITIONS.items() if not outgoing
)

#: Seconds a claim stays valid, and **the inequality that makes it a lock**:
#: a lease must outlive the longest run it guards, or it stops being one.
#:
#: Cloud Tasks caps dispatch at ``_DISPATCH_DEADLINE_SECONDS`` and the worker's
#: ``timeoutSeconds`` matches, so that is the longest a task can run before the
#: queue may redeliver. A lease shorter than the deadline is worse than none: a
#: killed worker may be past the Submit click, and the retry would find the
#: lease expired and file the application again. Hence grace added on top, and
#: derived rather than restated so the two cannot drift.
_LEASE_GRACE_SECONDS = 60
_LEASE_SECONDS = _DISPATCH_DEADLINE_SECONDS + _LEASE_GRACE_SECONDS

#: The statuses that mean "a process is supposed to be working on this right
#: now". Uniform: each one is claimed by a task subject to the same dispatch
#: deadline, so each needs the same floor. ``queued`` carries one for the
#: reaper's benefit — nothing claims that status today.
#:
#: ``tools.applications.reaper`` is what expires these.
#:
#: The asymmetry the reaper is built on: for ``tailoring`` the status and lease
#: are written together, so an absent lease means a document predating leases.
#: For ``submitting`` they come from two processes — the route writes the
#: status, the run writes the lease — so "submitting and no lease" is ambiguous,
#: not dead, and must never be read as "the owner is gone". The reaper refuses
#: to touch such a document and leaves it to ``cli/unwedge_submitting``.
#:
#: ``queued`` is the exception to "the lease decides": nothing claims it in the
#: ordinary flow, so the reaper judges staleness by age and then takes this
#: lease itself as re-dispatch bookkeeping.
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

    ``owner`` names which run holds it, so a release can be checked rather
    than assumed — otherwise a slow run can clear a lease a later run took
    over. Optional in the stored shape, so documents written before the field
    existed still read back as valid leases.
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

    A lease whose ``expires_at`` cannot be read counts as held: refusing to
    claim only wedges the document, which the reaper or
    ``cli/unwedge_submitting`` can undo, while claiming anyway risks a
    duplicate real job application, which nothing can undo.
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

    This is what makes at-least-once task delivery safe. A status transition
    cannot do the job: the API request already claimed the work by CAS-ing
    ``→ submitting``, so by the time the run starts there is no legal edge
    left. The lease is claimed by the run itself (inside ``run_submission``,
    not the task handler, so the in-process path is fenced too).

    Returns ``True`` when this caller took the lease. ``False`` — the document
    is gone, its status moved on, another lease is live, or the precondition
    was lost twice — means do nothing, which is what makes a redelivered task
    a no-op instead of a second live ATS submission.

    Terminal writes on the claiming path pass :data:`CLEAR_LEASE`, so a
    finished run releases the lease in the same write as its outcome;
    :func:`release_lease` covers the case where that write lost its race. A
    run that dies leaves the lease to expire on the :data:`IN_PROGRESS` clock.

    ``extra`` lands in the same write as the claim and may not contain
    :data:`OWNED_FIELDS`. It exists for the reaper's retry counter, which
    bounds the re-dispatch loop and so must advance exactly once per winning
    claim and never on a losing one.

    Retries once on a lost precondition (``_backfill_job_url`` writes on read)
    and re-reads status and lease on that retry.
    """
    if status not in IN_PROGRESS:
        raise ValueError(f"{status!r} carries no lease; see state.IN_PROGRESS")
    # Validated before the loop: a clash is a programming error, not a race, so
    # it must raise whether or not the first attempt reaches the network.
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

    For the path where the run finished but its terminal ``try_transition``
    lost, leaving behind the lease that write would have cleared.

    Refuses when the lease belongs to someone else or carries no ``owner``: a
    lease we cannot prove is ours might belong to a live run, and letting it
    expire costs only time. ``False`` means nothing was released, including
    the ordinary case where the terminal write already cleared it.
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

    For progress chatter — the submitter's per-step labels are display
    strings, not lifecycle edges. ``update`` rather than ``set(merge=True)``,
    so a late note cannot resurrect a document the undo path deleted.

    ``extra`` lands in the same write, under the same :data:`OWNED_FIELDS`
    guard as :func:`try_transition`'s. It carries ``submit_attempted_at``, the
    point-of-no-return marker the reaper reads before retrying a dead run:
    written separately, a crash between the two writes could lose it.
    Deliberately not a compare-and-swap — the marker must land whatever else
    happened to the document — which is safe because the only writer that
    clears it runs in the API request before the apply task is dispatched.
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

    ``allowed_from`` narrows the table for this one call, and is re-checked on
    every attempt including the retry's re-read. A caller whose precondition
    is narrower than the table must pass it here rather than filter
    beforehand: filtering outside the swap is not a compare-and-swap. The
    liveness sweep is the case that proves it — ``submitting →
    posting_removed`` is a legal edge, so a sweep that lost its precondition
    to a user clicking Submit would otherwise retry and invalidate the posting
    mid-submit.

    Uses ``update``, never ``set``: an application deleted by the undo path
    must stay deleted, and ``set`` would recreate it.

    One retry, because ``_backfill_job_url`` writes on read and so a
    concurrent ``GET /applications`` fails the precondition without changing
    anything. The retry re-checks legality against the new status, so a
    genuine race fails on the table instead of overwriting the winner.
    """
    for attempt in (0, 1):
        if not snap.exists:
            return False
        current = (snap.to_dict() or {}).get(STATUS_FIELD)
        # Re-checked on the retry, not just the first read — the whole point
        # of taking the caller's precondition rather than letting it filter.
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
