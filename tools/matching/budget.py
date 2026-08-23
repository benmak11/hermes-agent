# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-user scoring budget: a pre-run reservation of job slots.

One signup's first discovery cycle fetches ~13,000 jobs and, uncapped, spends
$28-32 scoring them — and it fires automatically on onboarding completion.
This module makes the worst case a known fixed number: before a scorer loads
anything it reserves at most ``SCORING_BUDGET_PER_CYCLE`` slots (and no more
than ``SCORING_BUDGET_PER_DAY`` across the UTC day), and scores only what it
was granted.

**Reserve, don't count.** The alternative — a counter incremented as scoring
proceeds — is one contended write per job, and still can't stop a run that has
already streamed 13K job docs into memory. A reservation is one transaction
per run, taken *before* the query, and what it grants becomes the query's
``limit``. Two instances scoring the same user concurrently each get a
disjoint slice. Whatever a run doesn't *reach* is refunded when it ends — the
slots granted beyond the backlog it actually had.

A slot is charged per job **attempted**, not per LLM call, so a job rejected
locally as out-of-family costs a slot despite costing no money. Deliberate: the
cheap alternative (charge only on a priced call) means an all-rejection backlog
streams without limit, which is the memory failure this exists to prevent. The
cost of it is that draining a large backlog takes longer than a pure
dollars-per-Pro-call model would suggest.

Rollover is **lazy, at read time inside the transaction**: a stored ``day``
that isn't today, or a ``cycle_id`` that isn't the run asking, means that
counter starts from zero. No cron, no scheduled reset job.

State lives in one map on the user doc, ``users/{uid}.scoring_budget``::

    day: "2026-08-23"        # UTC date jobs_scored_today belongs to
    jobs_scored_today: int
    cycle_id: "<run_id>"     # run that opened the current cycle window
    jobs_scored_this_cycle: int
    updated_at: iso

Env contract (same shape as ``tools.queues``):
- ``SCORING_BUDGET_PER_CYCLE`` — slots one cycle may score (default 200).
- ``SCORING_BUDGET_PER_DAY`` — slots one user may score per UTC day (400).

**The defaults are calibrated against a real measurement, not an estimate.** On
2026-08-23 one capped 100-job run through the Phase 1A ledger (run
``37338813872b4197a860e49d380c2813``) cost **$0.979287 — $0.0098/job**, split
$0.00279 per Flash parse and $0.01649 per cached Pro score. That is ~2.2x the
~$0.004/job figure measured in July, because both models now emit billed
thinking tokens (~510 per parse). At $0.0098/job, 200 slots is **~$1.96** and
400/day is **~$3.92**, which is what keeps a fresh signup's first cycle under
the $2 the viability criterion asks for. The earlier 300/1000 defaults would
have been ~$2.94 and ~$9.79.

Re-measure before trusting these: the rate has moved once already, and it moved
under us without any code change on our side.

Exceeding the budget is a normal outcome, not an error: the run scores what it
was granted, logs ``matching.budget_capped`` at info, and the rest of the
backlog waits for the next cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from google.cloud import firestore

from obs.logging import current_run_id, get_logger

log = get_logger("tools.matching")

# The map on users/{uid} holding the counters below.
FIELD = "scoring_budget"

DEFAULT_PER_CYCLE = 200
DEFAULT_PER_DAY = 400


class _CycleDefault(Enum):
    """Sentinel type for :data:`CURRENT_RUN` (an enum so it types cleanly)."""

    CURRENT_RUN = "current_run"


#: Default for every scorer's ``cycle_id``: the ambient ``run_id`` opens the
#: window. ``None`` cannot serve as that default, because ``None`` already
#: means something else — "draw down whatever window is open" — and the two
#: have to be distinguishable.
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

    ``cycle_id`` is the window the grant was drawn from — pass it back to
    :func:`release` so a refund can't credit a *different* cycle's counter.
    It is ``None`` only when the reserving run had no ``run_id`` bound and the
    user had no window open yet.
    """

    granted: int
    capped: bool
    remaining_cycle: int
    remaining_day: int
    cycle_id: str | None


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


def apply_reservation(
    state: dict | None,
    wanted: int,
    *,
    now: datetime,
    cycle_id: str | None,
    limits: Limits,
) -> tuple[dict, Reservation]:
    """Draw ``wanted`` slots from ``state``; return the new map and the grant.

    Pure — every rollover and clamping rule lives here, so the whole matrix
    (day rollover, cycle rollover, partial grant, exhaustion) is testable with
    no Firestore. :func:`reserve` is only the transaction around it.

    A ``None`` ``cycle_id`` means "whatever window is already open": an ad-hoc
    score task draws down the current cycle's remainder rather than opening a
    fresh one, which is what stops the per-cycle cap from being resettable on
    demand by anything that can trigger a scoring run.

    Note that the *cycle* counter has no time-based rollover — only a new
    ``cycle_id`` clears it. So an ad-hoc task asking against an exhausted
    window gets zero slots until the next discovery cycle opens a new one,
    even after the UTC day (and the daily counter) has rolled. That is the
    safe direction, and it is deliberate: the alternative is a window that
    expires on a clock nobody set.
    """
    state = dict(state or {})
    today = now.date().isoformat()
    day_used = _count(state, "jobs_scored_today") if state.get("day") == today else 0

    stored_cycle = state.get("cycle_id")
    cycle = cycle_id if cycle_id is not None else stored_cycle
    cycle_used = _count(state, "jobs_scored_this_cycle") if cycle == stored_cycle else 0

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
        ),
    )


def apply_release(
    state: dict | None, unused: int, *, now: datetime, cycle_id: str | None
) -> dict | None:
    """Give ``unused`` slots back; ``None`` when the refund no longer applies.

    Pure, like :func:`apply_reservation`. Each counter is credited only while
    it still describes the window the slots were taken from — past midnight
    UTC the debit was on yesterday's counter, and a cycle that has since been
    superseded must not have its successor's counter credited.
    """
    if unused <= 0:
        return None
    state = dict(state or {})
    refunded = dict(state)
    changed = False
    if state.get("day") == now.date().isoformat():
        refunded["jobs_scored_today"] = max(
            _count(state, "jobs_scored_today") - unused, 0
        )
        changed = True
    if state.get("cycle_id") == cycle_id:
        refunded["jobs_scored_this_cycle"] = max(
            _count(state, "jobs_scored_this_cycle") - unused, 0
        )
        changed = True
    if not changed:
        return None
    refunded["updated_at"] = now.isoformat()
    return refunded


def summary(reservation: Reservation | None, *, drawn: int | None = None) -> dict:
    """Budget fields for a run's counts dict / ``discovery_state``.

    Empty for an unbudgeted run (``ignore_budget``), so the UI shows nothing
    rather than a fabricated zero.

    ``drawn`` is how many slots the run actually filled. A run that filled
    every slot it was granted reports as capped whether or not the reservation
    itself was trimmed — that is the case this whole phase exists for, where a
    13K backlog meets a one-cycle grant and the ask (a full cycle) was granted
    in full.
    """
    if reservation is None:
        return {}
    return {
        "budget_granted": reservation.granted,
        "budget_remaining_cycle": reservation.remaining_cycle,
        "budget_remaining_day": reservation.remaining_day,
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
    """Transactionally draw slots for one run. ``wanted=None`` asks for a
    full cycle's worth.

    ``cycle_id`` defaults to the ambient run, which *opens* a window; pass
    ``None`` to draw down whatever window is already open instead (see
    :func:`apply_reservation`).

    Deliberately *not* the update-time-precondition + TTL-lease pattern
    ``batch_runs`` uses: that exists for a long-lived cross-process ingest,
    whereas this is a millisecond read-modify-write that Firestore's own
    transaction retries already serialize.

    Errors propagate. A run that can't reserve can't read its jobs either —
    both are the same Firestore — and failing closed is the safe direction for
    a spend cap.
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


async def release(db, user_id: str, unused: int, *, cycle_id: str | None) -> None:
    """Hand back slots a run reserved but never drew on. Never raises.

    A read-modify-write rather than a blind ``Increment(-unused)``: the
    counters are windowed, and crediting a window the slots were not taken
    from would hand a *later* cycle budget it never earned — the one direction
    a spend cap must not fail in.
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
                state, unused, now=datetime.now(UTC), cycle_id=cycle_id
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
