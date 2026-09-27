# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What scoring one job actually costs — the *empirical* rate, not the price list.

Deliberately not in ``obs.llm_cost``. That module owns Google's published
per-million-token prices: a fact about Google's pricing page, re-checked when
Google changes it. This is a fact about *our* pipeline — how many tokens a real
job turns into, how often jd_cache absorbs a parse, how much the pre-filter
tombstones for free — and it is re-measured on a completely different cadence,
by running the thing and reading the ledger. Mixing the two is how a stale
prompt change silently becomes a wrong quote.

**The user's own ledger is the primary source.** :func:`observed_rate` divides
what ``users/{uid}/runs`` says was spent by how many jobs those runs scored.
Anything shown to the user is then arithmetic over rows their own account
holds, and they can check it. The constants below are the fallback for an
account with no measured history — a real measurement, named and dated, not a
guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.matching")

#: Blended USD per job scored, measured 2026-08-23 — the first real ledger
#: measurement, and ~2x the July estimates it replaced. Re-measure before
#: trusting it; the note below on :data:`UNCERTAINTY` is why.
MEASURED_COST_PER_JOB_USD = 0.0098
MEASURED_AT = "2026-08-23"
#: The run this came from. Checkable: ``users/{uid}/runs/{MEASURED_RUN_ID}``.
MEASURED_RUN_ID = "37338813872b4197a860e49d380c2813"

#: Vertex batch prediction bills half the interactive rate on every token
#: class. Mirrors ``obs.llm_cost._BATCH_DISCOUNT``; restated rather than
#: imported because that one is Google's price rule and this one is the floor
#: of an estimate — if they ever have to differ, they should be able to.
BATCH_MULTIPLIER = 0.5

#: How far above the measured rate a quote's ceiling sits. Not a confidence
#: interval: between the July estimate and the August measurement the real
#: rate moved ~2.2x with no change on our side (longer JDs, a different model
#: mix). A quote that cannot be exceeded is a quote that will be, so the
#: ceiling carries that move.
UNCERTAINTY = 2.0

#: How the per-job rate divides between the two batch legs, used only to price
#: each leg's committed estimate so the two always sum to one per-job rate.
#: Shares rather than two independent rates, precisely so they cannot drift
#: apart.
#:
#: Both measurements on record:
#:   2026-07-10  parse $0.00125 / score $0.0093   -> parse is 11.8%
#:   2026-08-23  parse $0.00279 / score $0.01649  -> parse is 14.5%
#:
#: 0.13 sits between them. An earlier version said "13-15% both times" (wrong
#: — neither figure is in that range) and used 0.15, which is *above* both:
#: that over-attributes to the cheap parse leg and under-attributes to the
#: expensive score leg, and the score leg is the one submitted hours after
#: the click by ``/tasks/batch/resume``. Under-stating the late charge is the
#: wrong direction for a number whose entire purpose is surfacing it.
PARSE_SHARE = 0.13
SCORE_SHARE = 1.0 - PARSE_SHARE

#: Where a rate came from. ``observed`` is the user's own ledger; the constant
#: names its measurement date so the UI string can't outlive the number.
SOURCE_OBSERVED = "your_last_runs"
SOURCE_MEASURED = f"measured_{MEASURED_AT.replace('-', '_')}"

#: Ledger docs to read when deriving an observed rate. Recent runs only: the
#: rate moves with prompt and model changes, and averaging over a year of them
#: would quote the past rather than the present.
_RECENT_RUNS = 10


@dataclass(frozen=True)
class Rate:
    """A per-job cost, and where it came from."""

    usd_per_job: float
    source: str
    #: Jobs behind the rate. 0 for the constant — which is what tells a caller
    #: (and the user) that this is the fallback and not their own history.
    sample: int

    @property
    def measured(self) -> bool:
        return self.source == SOURCE_OBSERVED


#: The fallback, as a :class:`Rate`.
MEASURED = Rate(MEASURED_COST_PER_JOB_USD, SOURCE_MEASURED, 0)


async def observed_rate(db, user_id: str, *, min_jobs: int = 100) -> Rate:
    """This user's own $/job over their most recent completed runs.

    ``sum(llm.cost_usd) / sum(jobs.scored)``, and below ``min_jobs`` scored in
    total it declines and returns :data:`MEASURED` instead — a two-job run
    divides by a number small enough to quote anything.

    **Docs with ``jobs.scored == 0`` are skipped, not counted as zero.** An
    ingest that raised banks the leg's spend without its outcome counts (see
    ``batch_runs.resume``'s flush), so such a doc is cost with no denominator
    and would drag the rate up. Skipping loses a little real spend from the
    numerator, which errs low; counting it errs high and unboundedly.

    Never raises: this feeds a *quote*, and a Firestore hiccup must degrade to
    the documented constant rather than fail the user's click.
    """
    try:
        query = (
            db.collection("users")
            .document(user_id)
            .collection("runs")
            .order_by("ended_at", direction=firestore.Query.DESCENDING)
            .limit(_RECENT_RUNS)
        )
        cost = 0.0
        scored = 0
        async for snap in query.stream():
            doc = snap.to_dict() or {}
            jobs_scored = int((doc.get("jobs") or {}).get("scored") or 0)
            if jobs_scored <= 0:
                continue
            usd = float((doc.get("llm") or {}).get("cost_usd") or 0.0)
            if usd <= 0:
                continue
            cost += usd
            scored += jobs_scored
        if scored >= min_jobs and cost > 0:
            return Rate(cost / scored, SOURCE_OBSERVED, scored)
    except Exception:
        log.exception("rates.observed_failed", user_id=user_id)
    return MEASURED


def committed_usd(
    requests: int, *, leg: str, rate: Rate = MEASURED
) -> tuple[float, float]:
    """Low/high USD for one batch leg of ``requests`` requests.

    Job-count based on purpose: a per-request token estimator would have to
    model prompt length, and would be a second, differently-wrong answer to a
    question the ledger already answers empirically.

    **Defaults to the measured constant, never** :func:`observed_rate`, so the
    committed figure recorded on a ``batch_runs`` doc can disagree with the
    quote the user consented to — a user whose own history runs cheaper than
    the constant was quoted less than this records. That is accepted: this is
    an internal ops figure answering "what has Google billed us that we have
    not yet priced?", read only by :func:`outstanding_committed`. It never
    reaches an ``Estimate``, a 402 body, the confirm sheet or the run ledger,
    so it cannot mislead anybody about what they agreed to. Taking the
    caller's rate would make it agree with one user's quote and disagree with
    the ops question, which is the wrong trade for a reconciliation number.
    """
    share = PARSE_SHARE if leg == "parse" else SCORE_SHARE
    low = requests * rate.usd_per_job * share * BATCH_MULTIPLIER
    return round(low, 4), round(low * UNCERTAINTY, 4)
