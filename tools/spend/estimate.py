# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What a paid action would cost, in a form the user can check.

Three rules, each of which was a way of getting this wrong:

1. Units come from the budget grant, never the backlog. A fresh account has
   thousands of pending jobs against a 200-job per-cycle grant, so quoting the
   backlog is wrong by an order of magnitude — and reading the grant means an
   estimate needs no query over ``jobs`` at all.
2. Always a range, never a figure. The measured rate moved ~2.2x once with no
   change on our side.
3. Always its provenance: "based on your last 3 runs" and "using the
   2026-08-23 measurement" are different claims.

The estimate covers both batch legs, because a backlog scored as a batch pays
Flash at submit and Pro hours later when ``/tasks/batch/resume`` creates the
score batch. The quote is per job over the whole grant rather than per leg, so
consent at the click covers that later charge; narrowed to one leg, the resume
path would spend money nobody was asked about.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from tools.account import plan
from tools.matching import batch_runs, budget, rates

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

    A read through :func:`budget.remaining`, so the quote applies exactly the
    rollover the reservation will. Never :func:`budget.apply_reservation`: that
    returns the post-debit state, and quoting a price must never move a counter.
    """
    return budget.remaining(
        state,
        now=now or datetime.now(UTC),
        limits=limits or budget.Limits.from_env(),
        opens_cycle=action in _OPENS_CYCLE,
    )


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
    # Floor at the half-price batch path, ceiling at the observed rate drift —
    # **but only where the batch path is reachable.** ``score_or_start_run``
    # sends a run to Vertex batch only at ``BATCH_MIN_PENDING`` or more, and the
    # grant is what it sees (the reservation is taken first), so under a small
    # per-cycle cap every rating takes the online path at full price. Quoting
    # the batch rate anyway advertises a price the product structurally cannot
    # deliver, and the low end of a range is the number people remember.
    floor = rates.BATCH_MULTIPLIER if units >= batch_runs.BATCH_MIN_PENDING else 1.0
    low = units * rate.usd_per_job * floor
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
    state, limits = await budget_state(db, user_id)
    rate = await rates.observed_rate(db, user_id)
    remaining_cycle, remaining_day = available(state, action=action, limits=limits)
    return quote(
        action,
        remaining_cycle=remaining_cycle,
        remaining_day=remaining_day,
        rate=rate,
        limits=limits,
    )


async def budget_state(db, user_id: str) -> tuple[dict, budget.Limits]:
    """The user's ``scoring_budget`` map and their plan's caps, from one read.

    Never raises. A quote that cannot be produced must not become a click that
    spends without one, so a failure degrades to "full grant on the paid caps,
    fallback rate": the largest honest quote, which is the safe direction for a
    number whose job is to make someone hesitate.
    """
    try:
        snap = await db.collection("users").document(user_id).get()
        doc = snap.to_dict() or {}
    except Exception:
        return {}, budget.Limits.from_env(plan.PAID)
    return doc.get(budget.FIELD) or {}, budget.Limits.for_doc(doc)
