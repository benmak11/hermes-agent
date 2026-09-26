# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What a paid action would cost, in a form the user can check.

Three rules, each of which was a way of getting this wrong:

1. **Units come from the budget grant, never the backlog.** A fresh account
   has thousands of pending jobs and a 200-job per-cycle grant; quoting the
   backlog would be both terrifying and wrong by an order of magnitude. It
   also means an estimate needs no query over ``jobs`` at all — everything it
   reads is the ``scoring_budget`` map already on the user document.
2. **Always a range, never a figure.** The measured rate moved ~2.2x once with
   no change on our side. A single number invites being held to it.
3. **Always its provenance.** "based on your last 3 runs" and "using the
   2026-08-23 measurement" are different claims and the user is entitled to
   know which one they are being shown.

The estimate covers **both** batch legs. A backlog scored as a batch run pays
Flash at submit and Pro hours later, when ``/tasks/batch/resume`` creates the
score batch — long after the click. Consent captured at the click only covers
that second, later charge because the quote is per *job over the whole grant*
rather than per leg. If this is ever narrowed to one leg, the resume path
starts spending money nobody was asked about.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from tools.matching import budget, rates

#: "run discovery now, and score what it finds" — the manual button.
DISCOVERY_SCAN = "discovery_scan"
#: "score the jobs already sitting pending" — the second click.
SCORE_BACKLOG = "score_backlog"

ACTIONS = frozenset({DISCOVERY_SCAN, SCORE_BACKLOG})

#: Which actions open a fresh per-cycle window rather than drawing on whatever
#: window is already open. A discovery cycle reserves under its own ``run_id``
#: (``budget.CURRENT_RUN``), so its cycle counter starts full; an ad-hoc score
#: task passes ``cycle_id=None`` and draws down the open window's remainder.
#: Quoting the wrong one tells a user with a spent window that 200 jobs will
#: be scored when the answer is zero.
_OPENS_CYCLE = frozenset({DISCOVERY_SCAN})


@dataclass(frozen=True)
class Estimate:
    """A quote: how much work, at what rate, from where, under which cap."""

    action: str
    units: int
    unit: str
    usd_low: float
    usd_high: float
    rate_usd: float
    rate_source: str
    rate_sample: int
    caps: dict

    def as_dict(self) -> dict:
        """JSON shape for the 402 body and the stored consent document."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Estimate:
        return cls(**{field: data[field] for field in cls.__dataclass_fields__})


def available(
    state: dict | None,
    *,
    action: str,
    now: datetime | None = None,
    limits: budget.Limits | None = None,
) -> tuple[int, int]:
    """``(remaining_cycle, remaining_day)`` for ``action``, without reserving.

    Pure, and a *read* of ``budget``'s own counter semantics rather than a
    reimplementation of them — same day-rollover rule, same floor-at-zero on a
    hand-edited document. It deliberately does not call
    :func:`budget.apply_reservation`: that returns the post-debit state, and
    quoting a price must never move a counter.
    """
    now = now or datetime.now(UTC)
    limits = limits or budget.Limits.from_env()
    state = dict(state or {})

    today = now.date().isoformat()
    day_used = _count(state, "jobs_scored_today") if state.get("day") == today else 0
    # A fresh cycle starts the cycle counter over; drawing on the open window
    # sees whatever it has already spent.
    cycle_used = (
        0 if action in _OPENS_CYCLE else _count(state, "jobs_scored_this_cycle")
    )

    return (
        max(limits.per_cycle - cycle_used, 0),
        max(limits.per_day - day_used, 0),
    )


def _count(state: dict, key: str) -> int:
    try:
        return max(int(state.get(key) or 0), 0)
    except (TypeError, ValueError):
        return 0


def quote(
    action: str,
    *,
    remaining_cycle: int,
    remaining_day: int,
    rate: rates.Rate,
    limits: budget.Limits | None = None,
) -> Estimate:
    """Assemble the quote. Pure, so the whole matrix is testable with no I/O."""
    if action not in ACTIONS:
        raise ValueError(f"unknown spend action {action!r}; expected one of {ACTIONS}")
    limits = limits or budget.Limits.from_env()
    units = max(min(remaining_cycle, remaining_day), 0)
    # Floor at the half-price batch path (what a backlog over BATCH_MIN_PENDING
    # actually takes), ceiling at the observed rate drift. Online scoring at
    # the plain rate sits inside the range, which is the point of having one.
    low = units * rate.usd_per_job * rates.BATCH_MULTIPLIER
    high = units * rate.usd_per_job * rates.UNCERTAINTY
    return Estimate(
        action=action,
        units=units,
        unit="job",
        usd_low=round(low, 2),
        usd_high=round(high, 2),
        rate_usd=round(rate.usd_per_job, 6),
        rate_source=rate.source,
        rate_sample=rate.sample,
        caps={
            "per_cycle": limits.per_cycle,
            "per_day": limits.per_day,
            "remaining_cycle": remaining_cycle,
            "remaining_day": remaining_day,
        },
    )


async def build(db, user_id: str, action: str) -> Estimate:
    """The quote for ``action`` on this user: one user read, one ledger read.

    Neither read touches ``jobs`` — see rule 1 in the module docstring.
    """
    state = await _budget_state(db, user_id)
    rate = await rates.observed_rate(db, user_id)
    remaining_cycle, remaining_day = available(state, action=action)
    return quote(
        action,
        remaining_cycle=remaining_cycle,
        remaining_day=remaining_day,
        rate=rate,
    )


async def _budget_state(db, user_id: str) -> dict:
    """The user's ``scoring_budget`` map, or ``{}`` — never an exception.

    A quote that cannot be produced must not become a click that spends
    without one, so a failure here degrades to "full grant, fallback rate":
    the largest honest quote, which is the safe direction for a number whose
    job is to make someone hesitate.
    """
    try:
        snap = await db.collection("users").document(user_id).get()
        return (snap.to_dict() or {}).get(budget.FIELD) or {}
    except Exception:
        return {}
