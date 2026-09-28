# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-user discovery budget: a weekly allowance of searches.

The sibling of :mod:`tools.matching.budget`, and deliberately its mirror —
same reserve-don't-count shape, same pure ``apply_*`` core with the
transaction as a thin shell, same lazy rollover *inside* the transaction, same
fail-closed ``reserve`` / never-raising ``release``. Read that module first;
what follows is only what a *weekly* window changes.

**One counter, not two.** A search has no per-cycle analogue: the unit being
capped is the search itself. So the state is a single ``runs_this_week``
against a ``week`` key, and ``wanted`` is always 1 — ``granted`` is 0 or 1.
The :class:`Reservation` keeps its sibling's shape anyway so the two read
alike at the call sites.

**Charged at dispatch, never inside the cycle**, which is the one structural
difference from scoring. A scoring slot is charged per job attempted by the
scorer itself; a search is charged by whoever *starts* one — ``tick_user``
once its ``_claim_slot`` has won, ``POST /settings/discovery/run``, and the
onboarding kickoff in ``api.routes.profile``. The worker's ``/tasks/discovery*``
handlers must never charge: a Cloud Tasks redelivery of an already-charged
task would take a second run off the allowance for one user-visible search.

The cost of that choice, stated plainly: **this counts dispatches, not
crawls.** A redelivered task runs a second crawl on one charge. That is the
undercount direction, which is the safe one for a counter whose job is to stop
a user being locked out of their own product, and the crawl itself is free —
the money is downstream, behind the scoring budget.

**Refunds are for pre-work outcomes only.** A dispatch that never became work
— ``dispatch_cycle``/``enqueue_cycle`` deduped by the queue, a run refused by
``live_runs_refused()``, a cycle that found a deleted account — hands its run
back. A crawl that *ran* and then failed keeps its charge: retrying it is what
the next scheduled tick is for, and 14/week is itself the retry bound.

Rollover is **lazy, at read time inside the transaction**, exactly as for
scoring: a stored ``week`` that isn't this week reads as zero. No cron.

State lives in one map on the user doc, ``users/{uid}.discovery_budget``::

    week: "2026-W40"         # ISO year-week runs_this_week belongs to
    runs_this_week: int
    updated_at: iso

The same document already holds ``scoring_budget`` and ``discovery_state``, so
a surface that wants both caps reads them in the one ``get`` it already does.

Env contract: ``DISCOVERY_RUNS_PER_WEEK``, default 14, clamped to the 10-20
range the design offers. 14 is two searches a day; at the six-hourly cadence
the Profile card offers, a week wants 28, so the card's copy has to say what a
cadence costs rather than the cap silently truncating it.

Exceeding the allowance is a normal outcome, not an error: the caller logs
``discovery.budget_capped`` at info and answers 429 (a cap is not a price —
402 already means "confirm the spend").
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.discovery")

# The map on users/{uid} holding the counter below.
FIELD = "discovery_budget"

DEFAULT_PER_WEEK = 14

#: The range the design offers on the Profile card. A hand-set env outside it
#: is clamped rather than honoured: below the floor the product stops working,
#: above the ceiling the cap stops being one.
MIN_PER_WEEK = 10
MAX_PER_WEEK = 20

#: The kill switch. ``DISCOVERY_RUNS_PER_WEEK=0`` (or any value at or below
#: zero) turns the cap **off** rather than clamping to :data:`MIN_PER_WEEK` —
#: without this, backing the cap out of a live service would mean reverting
#: code, because every other value in range is still a cap. Off means off at
#: the shell: :func:`reserve` short-circuits before it reads or writes
#: anything, so an uncapped user costs exactly what they cost before this
#: module existed. Deliberately *outside* the clamped range: a value between
#: the floor and the ceiling is a cap, and only an explicit zero is not.
OFF = 0

#: The timezone every call site passes today, as a value rather than a default
#: buried in the signatures. No timezone is stored on a user (``Residence`` is
#: ``{country, state?, city?}``), deriving one needs a dependency and still
#: guesses for multi-timezone countries. Making the boundary a pure function of
#: ``(now, tz)`` from day one means switching to per-user local weeks later is
#: a value change at three call sites, not a refactor.
UTC_TZ = "UTC"


@dataclass(frozen=True)
class Limits:
    """How many searches one user may start per week."""

    per_week: int

    @property
    def enforced(self) -> bool:
        """Is there a cap at all? ``False`` when the kill switch is set.

        Read at the transaction shells rather than inside
        :func:`apply_reservation`, so an uncapped dispatch does no Firestore
        work whatsoever — the point of a kill switch is that the feature stops
        costing anything, not that it keeps bookkeeping nobody reads.
        """
        return self.per_week > OFF

    @classmethod
    def from_env(cls) -> Limits:
        return cls(per_week=_int_env("DISCOVERY_RUNS_PER_WEEK", DEFAULT_PER_WEEK))


@dataclass(frozen=True)
class Reservation:
    """What one dispatch was granted, and what is left after it.

    ``week_key`` is the window the grant was drawn from — pass it back to
    :func:`release` so a refund cannot credit a *later* week's counter.
    """

    granted: int
    capped: bool
    #: ``None`` when the cap is off — "unlimited", which is not a number and
    #: must not be rendered as one. Typed so a consumer that does arithmetic
    #: on it fails ``ty`` rather than printing a wrong figure.
    remaining_week: int | None
    week_key: str


def _int_env(name: str, default: int) -> int:
    """Clamped int from env; anything unparseable falls back to the default."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("discovery.budget_env_invalid", var=name, value=raw[:40])
        return default
    if value <= OFF:
        # The kill switch, ahead of the clamp on purpose — see ``OFF``.
        return OFF
    return max(min(value, MAX_PER_WEEK), MIN_PER_WEEK)


def _count(state: dict, key: str) -> int:
    """A stored counter, floored at 0 — a hand-edited doc must not grant more."""
    try:
        return max(int(state.get(key) or 0), 0)
    except (TypeError, ValueError):
        return 0


def week_start(now: datetime, tz: str) -> datetime:
    """Midnight on the Monday of ``now``'s week, in ``tz``.

    Built from the *local date* rather than by subtracting a ``timedelta`` from
    a local midnight: the two agree under UTC and disagree by an hour across a
    DST transition, and the version that disagrees is the one that would start
    quietly misfiling runs the day this stops being UTC.
    """
    local = now.astimezone(ZoneInfo(tz))
    monday = local.date() - timedelta(days=local.isoweekday() - 1)
    return datetime.combine(monday, time.min, tzinfo=ZoneInfo(tz))


def week_key(now: datetime, tz: str) -> str:
    """The stored window key for ``now`` — ``"2026-W40"``.

    **The ISO year is part of the key, and that is the whole point of this
    function existing.** ``isocalendar()[1]`` alone collapses week 1 of one
    year onto week 1 of the next, and at a year boundary the two are days
    apart: 2026-12-31 is ISO week 53 of 2026 while 2027-01-01 is week 53 of
    *2026* too, and 2024-12-30 is already week 1 of **2025**. A bare week
    number therefore either refuses a legitimate rollover or grants a free one,
    depending on which side of the boundary the user is on.
    """
    iso_year, iso_week, _ = now.astimezone(ZoneInfo(tz)).isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def resets_at(now: datetime, tz: str) -> str:
    """When the current window rolls, as an **ISO instant** in UTC.

    Never a formatted local time: the client renders it. A server that decides
    what "Monday 9am" looks like has to know the viewer's timezone, and this
    one deliberately does not.
    """
    return (week_start(now, tz) + timedelta(days=7)).astimezone(UTC).isoformat()


def used(state: dict | None, *, now: datetime | None = None, tz: str = UTC_TZ) -> int:
    """Searches already started in the window ``now`` falls in. Pure.

    A stored counter from a *previous* week reads as zero here, which is the
    same lazy rollover :func:`apply_reservation` applies — not a second
    implementation of it, the same one, so a display can never disagree with
    what the next reservation will do.
    """
    now = now or datetime.now(UTC)
    state = dict(state or {})
    return (
        _count(state, "runs_this_week") if state.get("week") == week_key(now, tz) else 0
    )


def remaining(
    state: dict | None,
    *,
    now: datetime | None = None,
    tz: str = UTC_TZ,
    limits: Limits | None = None,
) -> int | None:
    """Searches left this week, **without reserving one**.

    The read-only twin of :func:`apply_reservation`, for screens that must not
    move a counter: the tick's pre-claim screen, and the allowance an activity
    surface shows. It deliberately does not call ``apply_reservation`` — that
    returns the post-debit state, and looking must never cost.
    """
    limits = limits or Limits.from_env()
    if not limits.enforced:
        # Not a big number: ``None`` so a caller must decide what "unlimited"
        # looks like instead of rendering a figure nobody chose.
        return None
    return max(limits.per_week - used(state, now=now, tz=tz), 0)


def apply_reservation(
    state: dict | None,
    wanted: int = 1,
    *,
    now: datetime,
    tz: str = UTC_TZ,
    limits: Limits,
) -> tuple[dict, Reservation]:
    """Draw ``wanted`` runs from ``state``; return the new map and the grant.

    Pure — every rollover and clamping rule lives here, so the whole matrix is
    testable with no Firestore. :func:`reserve` is only the transaction around
    it, and that is what makes "two ticks cannot both take the last run" a
    property of one readable function rather than of a race.
    """
    state = dict(state or {})
    key = week_key(now, tz)
    week_used = _count(state, "runs_this_week") if state.get("week") == key else 0

    remaining_week = max(limits.per_week - week_used, 0)
    granted = max(min(wanted, remaining_week), 0)

    return (
        {
            "week": key,
            "runs_this_week": week_used + granted,
            "updated_at": now.isoformat(),
        },
        Reservation(
            granted=granted,
            capped=granted < wanted,
            remaining_week=remaining_week - granted,
            week_key=key,
        ),
    )


def apply_release(
    state: dict | None, unused: int = 1, *, now: datetime, tz: str = UTC_TZ, week: str
) -> dict | None:
    """Give ``unused`` runs back; ``None`` when the refund no longer applies.

    Pure, like :func:`apply_reservation`. The counter is credited only while it
    still describes the window the run was taken from — a refund that arrives
    after the week rolled was a debit on last week's counter and crediting this
    week's would hand out a run nobody spent.

    **An absent counter refunds nothing, and that is load-bearing beyond
    tidiness.** One caller of :func:`release` is the cycle's deleted-account
    refusal, and ``delete_account`` removes the user document outright; a
    refund that wrote anyway would recreate ``users/{uid}`` *without* its
    ``deleted_at`` tombstone and quietly resurrect the account.
    """
    if unused <= 0:
        return None
    state = dict(state or {})
    if state.get("week") != week:
        return None
    refunded = dict(state)
    refunded["runs_this_week"] = max(_count(state, "runs_this_week") - unused, 0)
    refunded["updated_at"] = now.isoformat()
    return refunded


def unlimited(
    wanted: int = 1, *, now: datetime | None = None, tz: str = UTC_TZ
) -> Reservation:
    """The grant when the cap is off: everything asked for, and no write.

    The twin of :func:`no_document` — both are grants that deliberately leave
    Firestore alone, one because there is nothing to charge and one because
    there is no cap to charge against. ``remaining_week`` is ``None`` rather
    than a large number: "unlimited" is not a quantity, and a display that
    treats it as one would invent a figure.
    """
    now = now or datetime.now(UTC)
    return Reservation(
        granted=max(wanted, 0),
        capped=False,
        remaining_week=None,
        week_key=week_key(now, tz),
    )


def no_document(*, now: datetime | None = None, tz: str = UTC_TZ) -> Reservation:
    """The grant for a user whose document is not there: nothing, and no write.

    **The charge side of the hazard** :func:`apply_release` **already guards.**
    ``reserve`` writes with ``merge=True``, which *creates* a missing document —
    and ``delete_account`` removes ``users/{uid}`` outright. A request racing
    the delete, or arriving on an ID token that has not expired yet (nothing
    verifies ``check_revoked``), would otherwise leave a document holding a
    ``discovery_budget`` and no ``deleted_at``: an account that reads as live
    to every loop that screens on the tombstone.

    Declining is also the right answer on its own terms. No document means no
    profile, no boards and nothing to search for, so a grant would buy a crawl
    that cannot run — and refusing to hand one out is the fail-closed direction
    this whole module is built in.
    """
    return Reservation(
        granted=0,
        capped=True,
        remaining_week=0,
        week_key=week_key(now or datetime.now(UTC), tz),
    )


def summary(reservation: Reservation) -> dict:
    """Allowance fields for a log line."""
    return {
        "granted": reservation.granted,
        "remaining_week": reservation.remaining_week,
        "week": reservation.week_key,
    }


def _log_if_capped(user_id: str, reservation: Reservation) -> Reservation:
    if reservation.capped:
        # Info, not a warning: hitting the cap is the design working.
        log.info("discovery.budget_capped", user_id=user_id, **summary(reservation))
    return reservation


async def reserve(
    db,
    user_id: str,
    wanted: int = 1,
    *,
    tz: str = UTC_TZ,
    limits: Limits | None = None,
) -> Reservation:
    """Transactionally draw one run for one dispatch.

    Errors propagate. A dispatch that cannot reserve must not happen: failing
    closed is the safe direction for a cap, and the same Firestore the
    reservation failed against is the one the cycle would have to read anyway.
    """
    limits = limits or Limits.from_env()
    if not limits.enforced:
        # Ahead of the client, not merely ahead of the write: with the cap off
        # this module must cost nothing at all, not even one read.
        return unlimited(wanted, tz=tz)
    user_ref = db.collection("users").document(user_id)

    @firestore.async_transactional
    async def _reserve(transaction) -> Reservation:
        snap = await user_ref.get(transaction=transaction)
        if not snap.exists:
            return no_document(tz=tz)
        state = (snap.to_dict() or {}).get(FIELD)
        new_state, reservation = apply_reservation(
            state, wanted, now=datetime.now(UTC), tz=tz, limits=limits
        )
        transaction.set(user_ref, {FIELD: new_state}, merge=True)
        return reservation

    return _log_if_capped(user_id, await _reserve(db.transaction()))


def reserve_sync(
    db,
    user_id: str,
    wanted: int = 1,
    *,
    tz: str = UTC_TZ,
    limits: Limits | None = None,
) -> Reservation:
    """:func:`reserve`, for the one charge site that has no event loop.

    The onboarding kickoff (``PUT /profile``) is a synchronous route that
    enqueues **inside the request** on purpose — it is the only thing that ever
    fires for a brand-new user, so it must not depend on the instance still
    having CPU after the response. Charging it through ``asyncio.run`` would
    build (and memoise) an async Firestore client on a loop that dies with the
    call; the sync transaction shell is the honest way round. The pure core
    below is the same one :func:`reserve` drives.
    """
    limits = limits or Limits.from_env()
    if not limits.enforced:
        # Ahead of the client, not merely ahead of the write: with the cap off
        # this module must cost nothing at all, not even one read.
        return unlimited(wanted, tz=tz)
    user_ref = db.collection("users").document(user_id)

    @firestore.transactional
    def _reserve(transaction) -> Reservation:
        snap = user_ref.get(transaction=transaction)
        if not snap.exists:
            return no_document(tz=tz)
        state = (snap.to_dict() or {}).get(FIELD)
        new_state, reservation = apply_reservation(
            state, wanted, now=datetime.now(UTC), tz=tz, limits=limits
        )
        transaction.set(user_ref, {FIELD: new_state}, merge=True)
        return reservation

    return _log_if_capped(user_id, _reserve(db.transaction()))


async def release(
    db, user_id: str, unused: int = 1, *, week: str, tz: str = UTC_TZ
) -> None:
    """Hand back a run that was charged but never became work. Never raises.

    A read-modify-write rather than a blind ``Increment(-1)``: the counter is
    windowed, and crediting a window the run was not taken from would hand a
    *later* week an allowance it never earned — the one direction a cap must
    not fail in.

    **Deliberately not short-circuited on the kill switch**, unlike
    :func:`reserve`. With the cap off nothing is charged, so there is normally
    nothing to credit and :func:`apply_release` writes nothing anyway — but a
    refund for a charge taken *before* the switch was thrown still has to land,
    or toggling the cap off and on again would leave the counter permanently
    over-stated.
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
                state, unused, now=datetime.now(UTC), tz=tz, week=week
            )
            if refunded is None:
                return False
            transaction.set(user_ref, {FIELD: refunded}, merge=True)
            return True

        if await _release(db.transaction()):
            log.info("discovery.budget_released", user_id=user_id, unused=unused)
    except Exception as e:
        # Worst case the user keeps this week's over-charge until the window
        # rolls; losing a refund must never fail the caller that earned it.
        log.warning(
            "discovery.budget_release_failed", user_id=user_id, error=str(e)[:200]
        )


def release_sync(
    db, user_id: str, unused: int = 1, *, week: str, tz: str = UTC_TZ
) -> None:
    """:func:`release`, for the synchronous onboarding charge site."""
    if unused <= 0:
        return
    try:
        user_ref = db.collection("users").document(user_id)

        @firestore.transactional
        def _release(transaction) -> bool:
            snap = user_ref.get(transaction=transaction)
            state = (snap.to_dict() or {}).get(FIELD)
            refunded = apply_release(
                state, unused, now=datetime.now(UTC), tz=tz, week=week
            )
            if refunded is None:
                return False
            transaction.set(user_ref, {FIELD: refunded}, merge=True)
            return True

        if _release(db.transaction()):
            log.info("discovery.budget_released", user_id=user_id, unused=unused)
    except Exception as e:
        log.warning(
            "discovery.budget_release_failed", user_id=user_id, error=str(e)[:200]
        )
