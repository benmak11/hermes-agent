# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Who may sign in at all — Phase 4 D1, shipped with enforcement off.

``web/src/app/login/page.tsx`` reads ``NEXT_PUBLIC_INVITE_CODES`` as a
client-side gate, but the API never validates it — Google sign-up is
unconditionally open. Phase 1's per-user scoring budget
(:mod:`tools.matching.budget`) bounds one user's spend once they are in;
nothing bounds *how many* users get in. This module is that bound::

    allowlist/{lowercased-email}
      {
        "email":      "user@example.com",
        "added_at":   "2026-09-04T12:00:00+00:00",
        "added_by":   "op or uid of the inviter",
        "note":       str | None,
        "revoked":    bool,
      }

**Keyed on email, not uid.** At invite time an operator only has an email
address — the account may not exist yet — and this has to match what Firebase
Auth carries on the token being verified, which is what ``api.deps`` checks
against. ``users/{uid}.email`` is a *different* field: it is résumé-extracted
by onboarding and is not guaranteed to be the login address at all. Every
function here keys off the Auth email, never the profile document.

**The predicate fails closed; this is the opposite bias from the discovery
guards on purpose.** ``api.routes.discovery.run_discovery_cycle`` and friends
fail *open* on an unreadable lease or a broken read, because refusing there
wedges one user's background loops shut for no good reason — the guard exists
to survive crashes, not to block spend. :func:`is_allowed` is a spend gate: a
read error, a missing doc, or a malformed doc must all come back "not
allowed", because the failure mode of guessing wrong here is a signup nobody
approved, not a delayed background loop. Two guards, two different jobs,
deliberately different biases — not an inconsistency to reconcile.

**Enforcement is a separate flag from the machinery.** :func:`enforced` reads
``ALLOWLIST_ENFORCED``, unset by default — same shape as
``tools.queues.enabled`` (``QUEUE_MODE``) and
``tools.matching.pipeline.geo_enforce_enabled`` (``GEO_GATE_ENFORCE``). This
module works, and is tested, with the flag on; every real caller in this PR
checks the flag first and is a no-op while it is off. Flipping it on — after
the three real accounts are seeded — is Phase 4 D2, a separate PR.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.allowlist")

#: Top-level collection; documents are keyed on the lowercased, stripped email.
COLLECTION = "allowlist"


def enforced() -> bool:
    """Off unless explicitly switched on. See the module docstring."""
    return os.getenv("ALLOWLIST_ENFORCED", "").strip().lower() in {"1", "true", "on"}


def _key(email: str | None) -> str:
    """The document id for an email — the one normalization every caller must
    share, or a doc written by one casing is invisible to a check under
    another."""
    return (email or "").strip().casefold()


async def is_allowed(db, email: str | None) -> bool:
    """Is ``email`` an active allowlist seat? One read, fully fails closed.

    Every one of these comes back ``False``: no email, no document, a document
    that is not a mapping, a document whose ``revoked`` is truthy (including a
    malformed non-bool truthy value — that is still the safe direction to
    fail), and any exception raised while reading. There is no path in this
    function that reaches ``return True`` except a document that exists, is a
    mapping, and says ``revoked`` is falsy.
    """
    key = _key(email)
    if not key:
        return False
    try:
        snap = await db.collection(COLLECTION).document(key).get()
    except Exception as e:
        log.warning("allowlist.check_failed", email_key=key, error=str(e)[:200])
        return False
    if not snap.exists:
        return False
    doc = snap.to_dict()
    if not isinstance(doc, dict):
        # A document Firestore reports as existing but whose body doesn't come
        # back as a mapping is not a shape add() ever wrote.
        return False
    return not doc.get("revoked")


async def _active_seats(db, transaction) -> int:
    """How many non-revoked allowlist docs exist, read *inside* ``transaction``.

    A full collection ``stream()`` rather than a ``count()`` aggregation.
    ``AsyncAggregationQuery.get`` does accept a ``transaction=`` in this
    client version, but nothing else in this codebase runs one, and at
    real-world seat counts (tens, not thousands) a stream is exactly as cheap
    and keeps this module on the pattern every other Firestore reader here
    already uses.
    """
    seats = 0
    async for snap in db.collection(COLLECTION).stream(transaction=transaction):
        doc = snap.to_dict() or {}
        if not doc.get("revoked"):
            seats += 1
    return seats


async def add(
    db,
    email: str,
    *,
    added_by: str,
    note: str | None = None,
    max_users: int,
) -> bool:
    """Grant ``email`` a seat, subject to ``max_users``. ``True`` iff, once
    this returns, ``email`` is an active seat — whether this call created it,
    reactivated it, or it already was one.

    **This is the concurrency point of the whole module**, and the design is
    lifted deliberately from :func:`tools.matching.budget.reserve`, not from
    ``tools.applications.state.try_transition``. The state pattern exists for
    long-lived, cross-process work guarded by an update-time precondition and
    a TTL lease; this is a millisecond read-modify-write, and Firestore's own
    transaction retries already serialize it the way a lease would, without a
    lease's failure modes (an expired lease that never gets reaped, a held
    lease that blocks a legitimate retry). Read ``budget.reserve``'s docstring
    for the same argument made about the same shape of problem.

    **The seat count is read inside this transaction, not before it opens.**
    Checking ``count() < max_users`` and then opening a transaction to write
    is a read-then-write with no isolation between the two — two concurrent
    invites can each read "9 of 10 seats taken" and each write the 10th,
    landing 11. That exact bug shape (a check performed outside the
    transaction that is supposed to make it safe) is one this project has
    shipped and caught six times already; see :func:`_active_seats`, called
    from inside the transaction below, for how this one avoids it.

    Idempotent on an already-active email: re-inviting someone already on the
    list is a no-op success and does not re-consult the cap, so a slow retry
    of a successful invite can never be turned into a spurious "seat cap
    reached" by the cap having filled in between. A previously **revoked**
    email re-checks the cap like any other new grant — its old seat was
    already excluded from :func:`_active_seats` while revoked, so this is a
    real seat being consumed again, not a re-grant of one that was never
    freed.

    Errors propagate, same reasoning as ``budget.reserve``: a caller that
    can't check the seat count can't safely grant one either, and guessing in
    the permissive direction is exactly what a seat cap must not do.
    """
    key = _key(email)
    if not key:
        raise ValueError("email must not be blank")
    ref = db.collection(COLLECTION).document(key)

    @firestore.async_transactional
    async def _add(transaction) -> bool:
        snap = await ref.get(transaction=transaction)
        existing = snap.to_dict() if snap.exists else None
        if isinstance(existing, dict) and not existing.get("revoked"):
            return True  # already an active seat — nothing to do
        seats = await _active_seats(db, transaction)
        if seats >= max_users:
            return False
        transaction.set(
            ref,
            {
                "email": key,
                "added_at": datetime.now(UTC).isoformat(),
                "added_by": added_by,
                "note": note,
                "revoked": False,
            },
        )
        return True

    granted = await _add(db.transaction())
    if granted:
        log.info("allowlist.seat_added", email_key=key, added_by=added_by)
    else:
        log.warning("allowlist.seat_cap_reached", email_key=key, max_users=max_users)
    return granted


async def revoke(db, email: str, *, revoked_by: str) -> bool:
    """Revoke ``email``'s seat, freeing it for the next invite. ``True`` iff a
    document existed to revoke.

    **Not transactional against** :func:`add`'s seat count, unlike ``add``
    itself — deliberately. Revoking is a single document write with nothing
    else to isolate it from: the failure mode of a stale read here is "one
    invite briefly sees a seat as still taken that was in fact just freed",
    which costs at most a delayed grant, not the over-grant a spend gate
    exists to prevent. That asymmetry is why :func:`add` needs a transaction
    and this does not.
    """
    key = _key(email)
    if not key:
        return False
    ref = db.collection(COLLECTION).document(key)
    snap = await ref.get()
    if not snap.exists:
        return False
    await ref.set(
        {
            "revoked": True,
            "revoked_at": datetime.now(UTC).isoformat(),
            "revoked_by": revoked_by,
        },
        merge=True,
    )
    log.info("allowlist.revoked", email_key=key, revoked_by=revoked_by)
    return True


async def list_entries(db) -> list[dict]:
    """Every allowlist document, for the CLI's ``list`` command. Unordered."""
    return [
        {"email": snap.id, **(snap.to_dict() or {})}
        async for snap in db.collection(COLLECTION).stream()
    ]
