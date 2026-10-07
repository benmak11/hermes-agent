# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-user scoring budget: a pre-run reservation of job slots.

A fresh signup's first discovery cycle fetches ~13,000 jobs and, uncapped,
spends $28-32 scoring them automatically on onboarding completion. Before a
scorer loads anything it reserves at most ``SCORING_BUDGET_PER_CYCLE`` slots
(and no more than ``SCORING_BUDGET_PER_DAY`` across the UTC day) and scores
only what it was granted, which makes the worst case a known fixed number.

Reserve, don't count. A counter incremented as scoring proceeds is one
contended write per job and still cannot stop a run that has already streamed
13K job docs into memory. A reservation is one transaction per run, taken
*before* the query, and what it grants becomes the query's ``limit``, so
concurrent runs for one user get disjoint slices. Slots a run never reaches are
refunded when it ends.

A slot is charged per job **attempted**, not per LLM call, so a job rejected
locally as out-of-family costs a slot despite costing nothing. Charging only on
a priced call would let an all-rejection backlog stream without limit; the
price of not doing so is that draining a large backlog takes longer.

Rollover is lazy, at read time inside the transaction: a stored ``day`` that is
not today zeroes *both* counters, and a ``cycle_id`` that is not the run asking
zeroes the cycle counter. No cron, no scheduled reset. The cycle window rolls
with the day because the per-cycle cap is held equal to the daily one, so the
rollover hands out nothing the daily reset was not already handing out, and a
window whose slots were never used or released costs at most the rest of its
day.

State lives in one map on the user doc, ``users/{uid}.scoring_budget``::

    day: "2026-08-23"        # UTC date jobs_scored_today belongs to
    jobs_scored_today: int
    cycle_id: "<run_id>"     # run that opened the current cycle window
    jobs_scored_this_cycle: int
    updated_at: iso

Env contract (same shape as ``tools.queues``):
- ``SCORING_BUDGET_PER_CYCLE`` — slots one cycle may score (default 3).
- ``SCORING_BUDGET_PER_DAY`` — slots one user may score per UTC day (3).

The defaults are calibrated against the 2026-08-23 measurement, priced on a
fully rated job: $0.00279 per Flash parse plus $0.01649 per cached Pro score,
so $0.0193/job rather than the $0.0098 blended average (which includes jobs
rejected for free and describes a population that no longer exists). Three a
day is ~$1.74/month per user at the ceiling, against ~$117.60/month for the
400/day this replaced. The per-cycle cap is held equal to the daily one.

Changing these defaults does not change a running service: both vars are
hand-set on Cloud Run and appear in no terraform CI check. They still have to
be right, because a service recreated from terraform restores whatever they
say.

Exceeding the budget is a normal outcome, not an error: the run scores what it
was granted, logs ``matching.budget_capped`` at info, and the rest of the
backlog waits for the next cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum

from google.cloud import firestore

from obs.logging import current_run_id, get_logger

log = get_logger("tools.matching")

# The map on users/{uid} holding the counters below.
FIELD = "scoring_budget"

DEFAULT_PER_CYCLE = 3
DEFAULT_PER_DAY = 3


class _CycleDefault(Enum):
    """Sentinel type for :data:`CURRENT_RUN` (an enum so it types cleanly)."""

    CURRENT_RUN = "current_run"


#: Default for every scorer's ``cycle_id``: the ambient ``run_id`` opens the
#: window. ``None`` cannot serve as that default, because it already means
#: something else — "draw down whatever window is open".
CURRENT_RUN = _CycleDefault.CURRENT_RUN

# What a scorer may be told about which cycle window it belongs to.
CycleId = str | None | _CycleDefault


def resolve_cycle_id(cycle_id: CycleId) -> str | None:
    """``CURRENT_RUN`` → the ambient run; anything else passes through."""
    return current_run_id() if cycle_id is CURRENT_RUN else cycle_id


@dataclass(frozen=True)
class Limits:
    """How many jobs one cycle, and one UTC day, may score."""

    per_cycle: int
    per_day: int

    @classmethod
    def from_env(cls) -> Limits:
        return cls(
            per_cycle=_int_env("SCORING_BUDGET_PER_CYCLE", DEFAULT_PER_CYCLE),
            per_day=_int_env("SCORING_BUDGET_PER_DAY", DEFAULT_PER_DAY),
        )


@dataclass(frozen=True)
class Reservation:
    """What one run was granted, and what is left after it.

    ``cycle_id`` and ``day`` name the window the grant was drawn from; pass
    both back to :func:`release` so a refund cannot credit a successor. It
    is ``None`` only when the reserving run had no ``run_id`` bound and the
    user had no window open yet.
    """

    granted: int
    capped: bool
    remaining_cycle: int
    remaining_day: int
    cycle_id: str | None
    #: UTC date the slots were drawn on; pass it back to :func:`release` too.
    day: str | None = None


def _int_env(name: str, default: int) -> int:
    """Non-negative int from env; anything unparseable falls back to default."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        log.warning("matching.budget_env_invalid", var=name, value=raw[:40])
        return default


def _count(state: dict, key: str) -> int:
    """A stored counter, floored at 0 — a hand-edited doc must not grant more."""
    try:
        return max(int(state.get(key) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _used(state: dict, *, today: str, same_cycle: bool) -> tuple[int, int]:
    """``(cycle_used, day_used)`` as of ``today``: a stored ``day`` that is not
    today reads both as zero, and ``same_cycle=False`` zeroes the cycle one."""
    if state.get("day") != today:
        return 0, 0
    cycle_used = _count(state, "jobs_scored_this_cycle") if same_cycle else 0
    return cycle_used, _count(state, "jobs_scored_today")


def remaining(
    state: dict | None,
    *,
    now: datetime,
    limits: Limits,
    opens_cycle: bool = False,
) -> tuple[int, int]:
    """``(remaining_cycle, remaining_day)`` a reservation would see. Read-only.

    ``opens_cycle`` asks for a run that opens its own window (a discovery
    cycle); the default draws on whatever window is open, as ad-hoc scoring
    does. Every display and quote reads through here so none can disagree with
    :func:`apply_reservation`.
    """
    cycle_used, day_used = _used(
        dict(state or {}), today=now.date().isoformat(), same_cycle=not opens_cycle
    )
    return (
        max(limits.per_cycle - cycle_used, 0),
        max(limits.per_day - day_used, 0),
    )


def apply_reservation(
    state: dict | None,
    wanted: int,
    *,
    now: datetime,
    cycle_id: str | None,
    limits: Limits,
) -> tuple[dict, Reservation]:
    """Draw ``wanted`` slots from ``state``; return the new map and the grant.

    Pure: every rollover and clamping rule lives here, and :func:`reserve` is
    only the transaction around it.

    A ``None`` ``cycle_id`` means "whatever window is already open", so an
    ad-hoc score task draws down the current cycle's remainder rather than
    opening a fresh one — otherwise anything that can trigger a scoring run
    could reset the per-cycle cap on demand.

    The cycle counter also starts over when the UTC day rolls. That cannot be
    used to reset the cap on demand, because the per-cycle cap equals the daily
    one and the daily counter resets at the same instant; it does mean a stale
    or leaked window blocks scoring for the rest of one day, not indefinitely.
    Within a day, ad-hoc requests share one window and cannot reset it.
    """
    state = dict(state or {})
    today = now.date().isoformat()
    stored_cycle = state.get("cycle_id")
    cycle = cycle_id if cycle_id is not None else stored_cycle
    cycle_used, day_used = _used(state, today=today, same_cycle=cycle == stored_cycle)

    remaining_cycle = max(limits.per_cycle - cycle_used, 0)
    remaining_day = max(limits.per_day - day_used, 0)
    granted = max(min(wanted, remaining_cycle, remaining_day), 0)

    return (
        {
            "day": today,
            "jobs_scored_today": day_used + granted,
            "cycle_id": cycle,
            "jobs_scored_this_cycle": cycle_used + granted,
            "updated_at": now.isoformat(),
        },
        Reservation(
            granted=granted,
            capped=granted < wanted,
            remaining_cycle=remaining_cycle - granted,
            remaining_day=remaining_day - granted,
            cycle_id=cycle,
            day=today,
        ),
    )


def apply_release(
    state: dict | None,
    unused: int,
    *,
    now: datetime,
    cycle_id: str | None,
    day: str | None = None,
) -> dict | None:
    """Give ``unused`` slots back; ``None`` when the refund no longer applies.

    Pure, like :func:`apply_reservation`. Each counter is credited only while
    it still describes the window the slots were taken from, so a refund
    arriving after midnight UTC or after a new cycle opened is dropped rather
    than crediting a successor window. ``day`` is the reservation's own date;
    without it a refund from yesterday could credit a window that reopened
    today under the same ``cycle_id``.
    """
    if unused <= 0:
        return None
    state = dict(state or {})
    today = now.date().isoformat()
    if state.get("day") != today or (day is not None and day != today):
        # Both counters belong to a day the slots were not taken from.
        return None
    refunded = dict(state)
    refunded["jobs_scored_today"] = max(_count(state, "jobs_scored_today") - unused, 0)
    if state.get("cycle_id") == cycle_id:
        refunded["jobs_scored_this_cycle"] = max(
            _count(state, "jobs_scored_this_cycle") - unused, 0
        )
    refunded["updated_at"] = now.isoformat()
    return refunded


def used(state: dict | None, *, now: datetime) -> int:
    """Jobs rated on the UTC day ``now`` falls in. Pure and read-only.

    A stored counter from a previous day reads as zero, the same lazy rollover
    the next reservation applies.

    It reports the counter, not the cap. Deriving it as ``per_day - remaining``
    clamps at the limit, which understates spend whenever the cap has been
    lowered under an account that already exceeded the new value.
    """
    state = dict(state or {})
    if state.get("day") != now.date().isoformat():
        return 0
    return _count(state, "jobs_scored_today")


def resets_at(now: datetime) -> str:
    """When ``jobs_scored_today`` rolls, as an ISO instant in UTC.

    Derived from the passed ``now`` rather than assumed to be UTC midnight, so
    it cannot disagree with the day key the next reservation computes. Always
    an instant, never a formatted local time; the client renders it. This is a
    different instant from ``tools.discovery.budget.resets_at`` (searches roll
    weekly, ratings nightly), so a surface showing both must show both.

    The cycle counter rolls at this same instant (and earlier, if a discovery
    cycle opens a new window).
    """
    tz = now.tzinfo or UTC
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=tz)
    return tomorrow.astimezone(UTC).isoformat()


def summary(reservation: Reservation | None, *, drawn: int | None = None) -> dict:
    """Budget fields for a run's counts dict / ``discovery_state``.

    Empty for an unbudgeted run (``ignore_budget``), so the UI shows nothing
    rather than a fabricated zero.

    ``drawn`` is how many slots the run actually filled. It marks a run that
    used every granted slot as capped even when the reservation itself was not
    trimmed, and it folds in the refund the caller's ``finally`` will make
    (``granted - drawn``). Without it the remaining counts are the figures from
    reserve time and understate what the user has left, which the Profile card
    renders verbatim. The refund is derived rather than passed because every
    call site computes it the same way; a caller reporting ``drawn`` for a run
    it never charged for passes 0 instead (``batch_runs``'s ``min_pending``
    short-circuit).

    Residual imprecision is display-only and always understating: a refund lost
    by ``release`` or declined by ``apply_release`` leaves this optimistic by at
    most one grant until the next cycle recomputes it.
    """
    if reservation is None:
        return {}
    refunded = 0 if drawn is None else max(reservation.granted - drawn, 0)
    return {
        "budget_granted": reservation.granted,
        "budget_remaining_cycle": reservation.remaining_cycle + refunded,
        "budget_remaining_day": reservation.remaining_day + refunded,
        "budget_capped": reservation.capped
        or (drawn is not None and drawn >= reservation.granted),
    }


async def reserve(
    db,
    user_id: str,
    wanted: int | None = None,
    *,
    cycle_id: CycleId = CURRENT_RUN,
    limits: Limits | None = None,
) -> Reservation:
    """Transactionally draw slots for one run. Writes to Firestore.

    ``wanted=None`` asks for a full cycle's worth. ``cycle_id`` defaults to the
    ambient run, which *opens* a window; pass ``None`` to draw down whatever
    window is already open instead.

    A plain transaction rather than the TTL-lease pattern ``batch_runs`` uses:
    this is a millisecond read-modify-write that Firestore's own transaction
    retries already serialize.

    Errors propagate — failing closed is the safe direction for a spend cap.
    """
    limits = limits or Limits.from_env()
    wanted = limits.per_cycle if wanted is None else wanted
    cycle = resolve_cycle_id(cycle_id)
    user_ref = db.collection("users").document(user_id)

    @firestore.async_transactional
    async def _reserve(transaction) -> Reservation:
        snap = await user_ref.get(transaction=transaction)
        state = (snap.to_dict() or {}).get(FIELD)
        new_state, reservation = apply_reservation(
            state, wanted, now=datetime.now(UTC), cycle_id=cycle, limits=limits
        )
        transaction.set(user_ref, {FIELD: new_state}, merge=True)
        return reservation

    reservation = await _reserve(db.transaction())
    if reservation.capped:
        # Info, not a warning: hitting the cap is the design working. The run
        # scores its slice and the remainder waits for the next cycle.
        log.info(
            "matching.budget_capped",
            user_id=user_id,
            wanted=wanted,
            **summary(reservation),
        )
    return reservation


async def release(
    db, user_id: str, unused: int, *, cycle_id: str | None, day: str | None = None
) -> None:
    """Hand back slots a run reserved but never drew on. Never raises.

    A read-modify-write rather than a blind ``Increment(-unused)``: the
    counters are windowed, and crediting a window the slots were not taken from
    would hand a later cycle budget it never earned.
    """
    if unused <= 0:
        return
    try:
        user_ref = db.collection("users").document(user_id)

        @firestore.async_transactional
        async def _release(transaction) -> bool:
            snap = await user_ref.get(transaction=transaction)
            state = (snap.to_dict() or {}).get(FIELD)
            refunded = apply_release(
                state, unused, now=datetime.now(UTC), cycle_id=cycle_id, day=day
            )
            if refunded is None:
                return False
            transaction.set(user_ref, {FIELD: refunded}, merge=True)
            return True

        if await _release(db.transaction()):
            log.info("matching.budget_released", user_id=user_id, unused=unused)
    except Exception as e:
        # Worst case the user keeps this cycle's over-reservation until the
        # window rolls; losing a refund must never fail the run that earned it.
        log.warning(
            "matching.budget_release_failed", user_id=user_id, error=str(e)[:200]
        )
