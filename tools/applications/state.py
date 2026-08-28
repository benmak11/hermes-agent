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

from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore

from obs.logging import get_logger

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

#: The statuses that mean "a process is supposed to be working on this right
#: now", mapped to how long that claim stays valid. Cloud Tasks caps dispatch at
#: 1800s (``tools.queues._DISPATCH_DEADLINE_SECONDS``) and the worker's
#: ``timeoutSeconds`` matches, so 20 minutes bounds a real run with margin.
#:
#: **Nothing reaps these yet.** This defines the shape so the reaper can be
#: added without re-deciding it; until then :func:`try_transition` writes a
#: lease only when one is passed explicitly, so no new field appears on live
#: documents.
IN_PROGRESS: dict[str, int] = {
    "queued": 900,
    "tailoring": 1200,
    "submitting": 1200,
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


def lease_for(status: str, *, now: datetime | None = None) -> dict | None:
    """The lease an in-progress ``status`` should carry, or ``None``.

    ``expires_at`` is what a reaper compares against: past it, the claiming
    process is presumed dead and the application may be failed or re-queued.
    """
    seconds = IN_PROGRESS.get(status)
    if seconds is None:
        return None
    now = now or _now()
    return {
        "status": status,
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=seconds)).isoformat(),
    }


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


def append_note(ref, status: str, message: str) -> bool:
    """Append a timeline entry **without** touching the document's status.

    For progress chatter: the submitter emits a label per step ("Filling
    standard fields", "submitting"), which is a display string, not a lifecycle
    edge. ``update`` rather than ``set(merge=True)`` so a late note can't
    resurrect a document the undo path deleted.
    """
    try:
        ref.update(
            {TIMELINE_FIELD: firestore.ArrayUnion([timeline_event(status, message)])}
        )
        return True
    except NotFound:
        return False


def _payload(to: str, note: str | None, lease: Lease, extra: dict | None) -> dict:
    if extra:
        clashes = sorted(set(extra) & set(OWNED_FIELDS))
        if clashes:
            raise ValueError(f"{', '.join(clashes)} may only be written by state.py")
    payload = dict(extra or {})
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
