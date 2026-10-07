# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Who may sign in at all — a cap on how many accounts exist.

:mod:`tools.matching.budget` bounds one user's spend once they are in; this
bounds how many get in. The login page's ``NEXT_PUBLIC_INVITE_CODES`` is a
client-side gate the API never validates, so without this Google sign-up is
unconditionally open::

    allowlist/{lowercased-email}
      {
        "email":      "user@example.com",
        "added_at":   "2026-09-04T12:00:00+00:00",
        "added_by":   "op or uid of the inviter",
        "note":       str | None,
        "revoked":    bool,
      }

Beside it, the queue of people who signed in without a seat::

    waitlist/{lowercased-email}
      {
        "uid":            "Firebase Auth uid of the account they created",
        "email":          "user@example.com",
        "source":         "hero" | "closing" | "nav" | "footer" | None,
        "email_verified": bool,
        "first_seen":     "2026-09-20T12:00:00+00:00",
        "last_seen":      "2026-09-20T12:00:00+00:00",
      }

Written by ``POST /account/signup`` for a non-allowlisted account, listed by
``cli.allowlist waitlist``, and removed on the grantee's first allowed sign-in
(or on account deletion). Keyed through the same :func:`_key` as the allowlist
so an operator's later ``add`` of the address lands on a matching id.

Keyed on the Firebase Auth email, never on uid and never on
``users/{uid}.email`` — that field is résumé-extracted and need not be the
login address. At invite time an operator may have nothing but an address, and
the account may not exist yet.

:func:`is_allowed` fails closed on every error, which is the opposite bias
from the discovery guards on purpose: guessing wrong here admits a signup
nobody approved, where guessing wrong there only delays a background loop.

Enforcement is a separate flag from the machinery. :func:`enforced` reads
``ALLOWLIST_ENFORCED``, unset by default; the module works and is tested with
it on, and every caller checks the flag first.

The seat cap is ``MAX_USERS``. :func:`seat_cap` reads it for the admin
page's grant route (``POST /admin/seats`` on ``hermes-api``); the CLI reads it
itself and also takes ``--max-users``. On the API an unset or invalid value
refuses every grant rather than meaning "unlimited".

Turning it on is a manual ``gcloud`` step. Seed and verify the real accounts
first, then set ``ALLOWLIST_ENFORCED=1`` on ``hermes-worker`` before
``hermes-api`` and confirm the worker's ``not_allowlisted`` count is 0 in
between: flipping the api first, or on an unseeded account, locks a real user
out with a 403 that no alert will catch. Rollback is
``gcloud run services update <service> --remove-env-vars ALLOWLIST_ENFORCED``.
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


def seat_cap() -> int | None:
    """``MAX_USERS`` as a positive int, else ``None``. ``None`` (unset, not an
    integer, or below 1) means grants must be refused — never unlimited."""
    try:
        cap = int(os.getenv("MAX_USERS", "").strip())
    except ValueError:
        return None
    return cap if cap >= 1 else None


def _key(email: str | None) -> str:
    """The document id for an email. Every caller must share this
    normalization, or a doc written under one casing is invisible to a check
    under another."""
    return (email or "").strip().casefold()


async def is_allowed(db, email: str | None) -> bool:
    """Is ``email`` an active allowlist seat? One read, fully fails closed.

    A blank email, a missing or non-mapping document, a truthy ``revoked``,
    and any read error all return ``False``. The only path to ``True`` is a
    document that exists, is a mapping, and has a falsy ``revoked``.
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

    A full ``stream()`` rather than a ``count()`` aggregation: at seat counts
    in the tens it is just as cheap and matches every other reader here.
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

    The seat count is read *inside* the transaction, like
    :func:`tools.matching.budget.reserve`. Checking ``count() < max_users``
    before opening one is a read-then-write with no isolation: two concurrent
    invites can each see 9 of 10 seats taken and each write the 10th.

    Idempotent on an already-active email — a no-op success that does not
    re-consult the cap, so a retry of a successful invite cannot turn into a
    spurious "cap reached". A previously revoked email re-checks the cap like
    any new grant, since its seat was freed when it was revoked.

    Errors propagate: a caller that cannot check the seat count must not grant
    one anyway.
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
        log.info("allowlist.seat_added", added_by=added_by)
    else:
        log.warning("allowlist.seat_cap_reached", max_users=max_users)
    return granted


async def revoke(db, email: str, *, revoked_by: str) -> bool:
    """Revoke ``email``'s seat, freeing it for the next invite. ``True`` iff a
    document existed to revoke.

    Not transactional against :func:`add`'s seat count, unlike ``add``: a
    stale read here only lets one invite briefly see a freed seat as taken,
    which costs a delayed grant rather than an over-grant.
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
    log.info("allowlist.revoked", revoked_by=revoked_by)
    return True


async def list_entries(db) -> list[dict]:
    """Every allowlist document, for the CLI's ``list`` command. Unordered."""
    return [
        {"email": snap.id, **(snap.to_dict() or {})}
        async for snap in db.collection(COLLECTION).stream()
    ]


#: Top-level collection of people who created an account without a seat.
#: Keyed exactly like ``COLLECTION`` — through :func:`_key` — so that the
#: operator's later ``allowlist add`` of the same address lands on a matching id.
WAITLIST_COLLECTION = "waitlist"


async def record_waitlist(
    db,
    *,
    uid: str,
    email: str | None,
    source: str | None,
    email_verified: bool,
    now: datetime | None = None,
) -> bool:
    """Record (or refresh) ``email``'s place in line. ``True`` iff created.

    ``first_seen`` and ``source`` are written on create only; a repeat call
    moves ``last_seen`` and nothing else. Read-then-write, not a transaction:
    two racing first calls each write the same ``first_seen`` to the second,
    which is not an outcome worth a transaction's machinery.
    """
    key = _key(email)
    if not key:
        raise ValueError("email must not be blank")
    ref = db.collection(WAITLIST_COLLECTION).document(key)
    stamp = (now or datetime.now(UTC)).isoformat()
    snap = await ref.get()
    if snap.exists:
        await ref.set({"last_seen": stamp}, merge=True)
        return False
    await ref.set(
        {
            "uid": uid,
            "email": key,
            "source": source,
            "email_verified": email_verified,
            "first_seen": stamp,
            "last_seen": stamp,
        }
    )
    log.info("waitlist.recorded", email_key=key, source=source)
    return True


async def remove_from_waitlist(db, email: str | None) -> None:
    """Drop ``email``'s waitlist doc. A no-op on a blank address or an absent doc."""
    key = _key(email)
    if not key:
        return
    await db.collection(WAITLIST_COLLECTION).document(key).delete()


async def list_waitlist(db) -> list[dict]:
    """Every waitlist document, for the CLI. Unordered (the CLI sorts)."""
    return [
        {"email": snap.id, **(snap.to_dict() or {})}
        async for snap in db.collection(WAITLIST_COLLECTION).stream()
    ]
