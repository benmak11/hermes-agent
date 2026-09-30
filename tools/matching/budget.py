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
- ``SCORING_BUDGET_PER_CYCLE`` — slots one cycle may score (default 3).
- ``SCORING_BUDGET_PER_DAY`` — slots one user may score per UTC day (3).

**The defaults are calibrated against a real measurement, and the basis is a
fully rated job.** On 2026-08-23 one capped 100-job run through the Phase 1A
ledger (run ``37338813872b4197a860e49d380c2813``) cost $0.979287, split
**$0.00279 per Flash parse and $0.01649 per cached Pro score**. The $0.0098/job
that number averages to is an average over every job *attempted*, including the
ones rejected locally for free — so it describes a population that no longer
exists once only shortlisted jobs are rated. **Budget at $0.0193/job**: parse
plus score, which is what every rated job now takes.

At $0.0193/job, 3 a day is **~$0.058/day, ~$1.74/month** per user at the
ceiling — against ~$117.60/month for the 400/day this replaced, and still
~$3.82/month if the rate moves 2.2x again, which it has done once already
without any code change on our side. The per-cycle cap stops mattering at this
size and is held equal to the daily one so a fresh signup's first cycle is 3
good matches rather than 200 average ones.

**Changing these defaults does not change a running service.** ``_int_env``
prefers the env value and ``SCORING_BUDGET_PER_CYCLE``/``_PER_DAY`` are
hand-set on Cloud Run, represented in no terraform CI check — so the ops step
is a separate, deliberate one. The default still has to be right: a service
recreated from terraform silently restores whatever it says.

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


def used(state: dict | None, *, now: datetime) -> int:
    """Jobs rated on the UTC day ``now`` falls in. Pure, and read-only.

    The read-only twin of the day counter inside :func:`apply_reservation`,
    and the exact shape ``tools.discovery.budget.used`` already has. A stored
    counter from a *previous* day reads as zero here, which is the same lazy
    rollover the next reservation will apply.

    **It reports the counter, not the cap.** Deriving it as
    ``per_day - remaining`` clamps at the limit, which is a lie in one real
    situation: ``SCORING_BUDGET_PER_DAY`` was cut from 400 to 3 in #92, so an
    account that had rated 46 jobs when the new value took effect would read
    "3 of 3" instead of "46 of 3". What happened is knowable, and a surface
    that rounds it down to the cap is understating spend — the direction this
    program exists to stop.
    """
    state = dict(state or {})
    if state.get("day") != now.date().isoformat():
        return 0
    return _count(state, "jobs_scored_today")


def resets_at(now: datetime) -> str:
    """When ``jobs_scored_today`` rolls, as an **ISO instant** in UTC.

    The daily counter's window is keyed by ``now.date()`` in
    :func:`apply_reservation`, so the instant it rolls is midnight at the start
    of the *next* such date — derived from the same ``now`` rather than
    assumed to be UTC midnight, so this can never disagree with the key the
    next reservation will compute. Every caller passes an aware UTC ``now``
    today, which makes that next UTC midnight.

    Never a formatted local time: the client renders it, exactly as
    ``tools.discovery.budget.resets_at`` does for the weekly window. Note the
    two are **different instants** — searches roll on Monday, ratings nightly —
    and a surface showing both must show both.

    The *cycle* counter deliberately has no reset instant, because it has no
    time-based rollover at all: only a new ``cycle_id`` clears it (see
    :func:`apply_reservation`). Inventing a clock for it here would be the
    fabricated-progress bug in a different hat.
    """
    tz = now.tzinfo or UTC
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=tz)
    return tomorrow.astimezone(UTC).isoformat()


def summary(reservation: Reservation | None, *, drawn: int | None = None) -> dict:
    """Budget fields for a run's counts dict / ``discovery_state``.

    Empty for an unbudgeted run (``ignore_budget``), so the UI shows nothing
    rather than a fabricated zero.

    ``drawn`` is how many slots the run actually filled, and it does two jobs.

    First, capping: a run that filled every slot it was granted reports as
    capped whether or not the reservation itself was trimmed — that is the case
    this whole phase exists for, where a 13K backlog meets a one-cycle grant and
    the ask (a full cycle) was granted in full.

    Second, **the remaining counts, which without it are simply wrong.** A
    ``Reservation`` is built at reserve time, when ``granted`` slots have just
    been debited; the run then draws on ``drawn`` of them and its caller's
    ``finally`` hands ``granted - drawn`` back. Reporting the reservation's own
    figures skips that refund entirely: a user with 12 pending jobs reserves
    200, scores 12, refunds 188 — and the Profile card, which renders
    ``budget_remaining_day`` verbatim as "N of today's scoring budget left",
    tells them 188 fewer than they have. It never *over*-states, and each cycle
    re-reads the real counters from Firestore, so this was a reporting bug and
    not an enforcement one; it is fixed here because here is where the two
    numbers meet.

    The refund is derived rather than passed because every call site already
    computes it the same way — ``budget.release(..., reservation.granted -
    attempted)`` with ``drawn == attempted``. The one place those two could
    disagree is a caller that reports ``drawn`` for a run it never charged for,
    which is why ``batch_runs``'s ``min_pending`` short-circuit passes 0.

    Residual imprecision, all of it in the understating direction and all of it
    display-only: ``release`` swallows its own failures, and ``apply_release``
    declines a refund whose window has rolled, so a losing refund leaves this
    number optimistic by at most one grant until the next cycle recomputes it.
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
