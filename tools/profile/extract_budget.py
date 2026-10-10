# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-user daily cap on résumé extraction (``POST /profile/extract``).

Each extraction is one Gemini Pro call. The shape mirrors
:mod:`tools.discovery.budget`: a pure ``apply_*`` core with a transaction as a
thin shell, lazy rollover at read time, a fail-closed ``charge`` and a
never-raising ``refund``.

Charged before the model call, inside a transaction, so two concurrent uploads
cannot both take the last slot. Refunded only for failures before any model
call (an unreadable or empty upload); once Gemini has been called the money is
spent and the slot stays used, even if the extraction then failed.

State lives on the user doc, ``users/{uid}.extract_budget``::

    day: "2026-10-09"   # UTC date the count belongs to
    count: int
    updated_at: iso

A refused charge writes nothing. A charge against a missing user doc creates
it, as the extraction's own save would.

Env contract: ``PROFILE_EXTRACT_PER_DAY``, default 5; ``0`` or below turns the
cap off and the module then touches no Firestore at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.profile")

FIELD = "extract_budget"

DEFAULT_PER_DAY = 5

#: The kill switch, as in :mod:`tools.discovery.budget`.
OFF = 0


@dataclass(frozen=True)
class Limits:
    """How many extractions one user may run per UTC day."""

    per_day: int

    @property
    def enforced(self) -> bool:
        return self.per_day > OFF

    @classmethod
    def from_env(cls) -> Limits:
        raw = os.getenv("PROFILE_EXTRACT_PER_DAY", "").strip()
        if not raw:
            return cls(per_day=DEFAULT_PER_DAY)
        try:
            value = int(raw)
        except ValueError:
            log.warning("profile.extract_budget_env_invalid", value=raw[:40])
            return cls(per_day=DEFAULT_PER_DAY)
        return cls(per_day=max(value, OFF))


@dataclass(frozen=True)
class Charge:
    """The outcome of one charge attempt.

    ``charged`` says whether a slot was actually taken (and so may be
    refunded); it is ``False`` both for a refusal and when the cap is off.
    """

    granted: bool
    charged: bool
    #: Extractions counted today, this one included when granted.
    used: int
    #: ``None`` when the cap is off.
    per_day: int | None
    day: str
    resets_at: str


def day_key(now: datetime) -> str:
    """The stored window key: the UTC date, ``"2026-10-09"``."""
    return now.astimezone(UTC).date().isoformat()


def resets_at(now: datetime) -> str:
    """The next UTC midnight, as an ISO instant. The client renders it."""
    tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=UTC).isoformat()


def _count(state: dict) -> int:
    """A stored counter, floored at 0 — a hand-edited doc must not grant more."""
    try:
        return max(int(state.get("count") or 0), 0)
    except (TypeError, ValueError):
        return 0


def used(state: dict | None, *, now: datetime) -> int:
    """Extractions already counted on ``now``'s UTC day. Pure."""
    state = dict(state or {})
    return _count(state) if state.get("day") == day_key(now) else 0


def apply_charge(
    state: dict | None, *, now: datetime, limits: Limits
) -> tuple[dict | None, Charge]:
    """Take one slot from ``state``. Returns the new map (``None`` when
    refused, so nothing is written) and the outcome. Pure."""
    key = day_key(now)
    today = used(state, now=now)
    if today >= limits.per_day:
        return None, Charge(
            granted=False,
            charged=False,
            used=today,
            per_day=limits.per_day,
            day=key,
            resets_at=resets_at(now),
        )
    return (
        {"day": key, "count": today + 1, "updated_at": now.isoformat()},
        Charge(
            granted=True,
            charged=True,
            used=today + 1,
            per_day=limits.per_day,
            day=key,
            resets_at=resets_at(now),
        ),
    )


def apply_refund(state: dict | None, *, now: datetime, day: str) -> dict | None:
    """Give one slot back; ``None`` when the refund no longer applies.

    Only while the stored counter still describes ``day``: after the window
    rolled, a credit would hand out a slot nobody spent.
    """
    state = dict(state or {})
    if state.get("day") != day:
        return None
    return {
        **state,
        "count": max(_count(state) - 1, 0),
        "updated_at": now.isoformat(),
    }


def unlimited(now: datetime | None = None) -> Charge:
    """The grant when the cap is off: no read, no write, nothing to refund."""
    now = now or datetime.now(UTC)
    return Charge(
        granted=True,
        charged=False,
        used=0,
        per_day=None,
        day=day_key(now),
        resets_at=resets_at(now),
    )


def charge(db, user_id: str, *, limits: Limits | None = None) -> Charge:
    """Transactionally take one extraction slot for ``user_id``.

    Synchronous, for the profile routes' sync Firestore client. Errors
    propagate: an extraction that cannot be counted must not run.
    """
    limits = limits or Limits.from_env()
    if not limits.enforced:
        return unlimited()
    user_ref = db.collection("users").document(user_id)

    @firestore.transactional
    def _charge(transaction) -> Charge:
        snap = user_ref.get(transaction=transaction)
        state = (snap.to_dict() or {}).get(FIELD) if snap.exists else None
        new_state, outcome = apply_charge(state, now=datetime.now(UTC), limits=limits)
        if new_state is not None:
            transaction.set(user_ref, {FIELD: new_state}, merge=True)
        return outcome

    outcome = _charge(db.transaction())
    if not outcome.granted:
        # Info, not a warning: hitting the cap is the design working.
        log.info(
            "profile.extract_capped",
            user_id=user_id,
            used=outcome.used,
            per_day=outcome.per_day,
        )
    return outcome


def refund(db, user_id: str, *, day: str) -> None:
    """Hand back a slot that never reached the model. Never raises."""
    try:
        user_ref = db.collection("users").document(user_id)

        @firestore.transactional
        def _refund(transaction) -> bool:
            snap = user_ref.get(transaction=transaction)
            state = (snap.to_dict() or {}).get(FIELD) if snap.exists else None
            refunded = apply_refund(state, now=datetime.now(UTC), day=day)
            if refunded is None:
                return False
            transaction.set(user_ref, {FIELD: refunded}, merge=True)
            return True

        if _refund(db.transaction()):
            log.info("profile.extract_refunded", user_id=user_id)
    except Exception as e:
        # Worst case the user loses one of today's slots; a lost refund must
        # never turn the caller's 4xx into a 500.
        log.warning(
            "profile.extract_refund_failed", user_id=user_id, error=str(e)[:200]
        )
