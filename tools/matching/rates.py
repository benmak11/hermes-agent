# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What scoring one job actually costs — the empirical rate, not the price list.

Deliberately separate from ``obs.llm_cost``, which owns Google's published
per-token prices. This is a fact about our own pipeline, re-measured by running
it and reading the ledger; mixing the two is how a prompt change silently
becomes a wrong quote.

:func:`observed_rate` is the primary source and divides what
``users/{uid}/runs`` spent by how many jobs those runs scored, so anything
shown to the user is arithmetic over their own rows. The constants below are
the fallback for an account with no history — real measurements, named and
dated.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.matching")

#: Blended USD per job scored, measured 2026-08-23 — ~2x the July estimates it
#: replaced. Re-measure before trusting it; see :data:`UNCERTAINTY`.
MEASURED_COST_PER_JOB_USD = 0.0098
MEASURED_AT = "2026-08-23"
#: The run this came from. Checkable: ``users/{uid}/runs/{MEASURED_RUN_ID}``.
MEASURED_RUN_ID = "37338813872b4197a860e49d380c2813"

#: Vertex batch prediction bills half the interactive rate on every token
#: class. Mirrors ``obs.llm_cost._BATCH_DISCOUNT``, restated rather than
#: imported: that one is Google's price rule, this one is the floor of an
#: estimate, and they must be free to differ.
BATCH_MULTIPLIER = 0.5

#: How far above the measured rate a quote's ceiling sits. Not a confidence
#: interval: the real rate moved ~2.2x between the July and August
#: measurements with no change on our side, and the ceiling carries that move.
UNCERTAINTY = 2.0

#: How the per-job rate divides between the two batch legs. Shares rather than
#: two independent rates, so the legs always sum to one per-job rate.
#:
#: 0.13 sits between the two measurements on record (2026-07-10 parse 11.8%,
#: 2026-08-23 parse 14.5%). Do not raise it above both: that under-attributes
#: to the score leg, which is the one charged hours after the click by
#: ``/tasks/batch/resume``, and understating the late charge defeats the
#: number's purpose.
PARSE_SHARE = 0.13
SCORE_SHARE = 1.0 - PARSE_SHARE

#: The same 2026-08-23 measurement's per-leg unit costs. **Not the same number
#: as** :data:`MEASURED_COST_PER_JOB_USD`, which is run cost over every job
#: *attempted*, many of which skip a leg via jd_cache or a free pre-filter
#: reject. These are cost per occurrence of a leg, so their sum is the cost of
#: taking one job all the way to a rated match — and under the 3-slot caps
#: (``tools.matching.budget``) a slot is a rated job, so the budget must price
#: against this sum. The blended average understates a rated job by ~2x.
MEASURED_PARSE_USD = 0.00279
MEASURED_SCORE_USD = 0.01649
MEASURED_RATED_JOB_USD = round(MEASURED_PARSE_USD + MEASURED_SCORE_USD, 6)

#: Where a rate came from. ``observed`` is the user's own ledger; the constant
#: names its measurement date so the UI string cannot outlive the number.
SOURCE_OBSERVED = "your_last_runs"
SOURCE_MEASURED = f"measured_{MEASURED_AT.replace('-', '_')}"
SOURCE_MEASURED_RATED = f"measured_rated_{MEASURED_AT.replace('-', '_')}"

#: Ledger docs to read when deriving an observed rate. Recent runs only: the
#: rate moves with prompt and model changes, so a longer window quotes the
#: past rather than the present. Wide enough that a user scoring at the
#: ``budget`` caps (3/cycle, 3/day) accumulates the jobs
#: :func:`observed_rate` asks for inside the window.
_RECENT_RUNS = 30


@dataclass(frozen=True)
class Rate:
    """A per-job cost, and where it came from."""

    usd_per_job: float
    source: str
    #: Jobs behind the rate. 0 means the fallback constant, not the user's own
    #: history.
    sample: int

    @property
    def measured(self) -> bool:
        return self.source == SOURCE_OBSERVED


#: The blended constant as a :class:`Rate` — cost per job *attempted*. Used
#: only as :func:`committed_usd`'s default, an ops reconciliation figure;
#: :func:`observed_rate` does not fall back to it.
MEASURED = Rate(MEASURED_COST_PER_JOB_USD, SOURCE_MEASURED, 0)

#: What :func:`observed_rate` falls back to: cost per rated job. Under the
#: 3-slot caps a slot is in practice a rated job, so this is the unit a quote's
#: ``units`` counts, and the closer of the two constants to what an account's
#: own observed rate measures.
MEASURED_RATED = Rate(MEASURED_RATED_JOB_USD, SOURCE_MEASURED_RATED, 0)


async def observed_rate(db, user_id: str, *, min_jobs: int = 20) -> Rate:
    """This user's own $/job over their most recent completed runs.

    ``sum(llm.cost_usd) / sum(jobs.scored)``, falling back to
    :data:`MEASURED_RATED` below ``min_jobs`` scored in total — a two-job run
    divides by a number small enough to quote anything. 20 smooths over an
    outlier job while staying reachable: at the 3-jobs-a-day cap in
    ``tools.matching.budget`` a higher threshold could never be met inside the
    :data:`_RECENT_RUNS` window, and the function would always fall back. The
    fallback is the rated constant rather than the blended :data:`MEASURED`,
    which averages in free rejects and so quotes a fresh account too low.

    Docs with ``jobs.scored == 0`` are skipped rather than counted as zero: a
    failed ingest banks a leg's spend without its outcome counts, so such a doc
    is cost with no denominator. Skipping errs low; counting errs high and
    unboundedly.

    Never raises — this feeds a quote, so a Firestore hiccup degrades to the
    constant rather than failing the user's click.
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
    return MEASURED_RATED


def committed_usd(
    requests: int, *, leg: str, rate: Rate = MEASURED
) -> tuple[float, float]:
    """Low/high USD for one batch leg of ``requests`` requests.

    Job-count based on purpose: a per-request token estimator would model
    prompt length and give a second, differently-wrong answer to a question the
    ledger already answers empirically.

    Defaults to the measured constant rather than :func:`observed_rate`, so the
    committed figure on a ``batch_runs`` doc may disagree with the quote the
    user consented to. That is intended: this is an internal ops figure ("what
    has Google billed us that we have not yet priced?") read only by
    ``outstanding_committed``, and it never reaches an ``Estimate``, a 402
    body, the confirm sheet or the run ledger.
    """
    share = PARSE_SHARE if leg == "parse" else SCORE_SHARE
    low = requests * rate.usd_per_job * share * BATCH_MULTIPLIER
    return round(low, 4), round(low * UNCERTAINTY, 4)
