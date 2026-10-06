# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What tailoring one application actually costs — the empirical rate.

Same split as ``tools.matching.rates`` and for the same reason: this is a fact
about *our* pipeline (token counts a real objective-rewrite turns into), not
about Google's price list, and it moves on a different cadence than either.
``Rate``/``SOURCE_OBSERVED`` are imported from there rather than redefined —
it is a generic (value, provenance, sample-size) triple with nothing
matching-specific about its shape, and ``tools.spend.estimate`` already
imports it from that module for the same reason.

No dated ledger measurement exists yet for this leg: unlike
``matching.rates.MEASURED_COST_PER_JOB_USD``, :data:`ESTIMATED` is computed
from ``obs.llm_cost``'s published Flash rates and the objective prompt's
typical token range — a model of the cost, not an observation. It is a
placeholder so a fresh account gets a number instead of ``None`` propagating
into a quote, and should be retired once :func:`observed_rate` has real runs
behind it.
"""

from __future__ import annotations

from google.cloud import firestore

from obs.llm_cost import compute_cost_usd
from obs.logging import get_logger
from tools.matching.rates import SOURCE_OBSERVED, Rate
from tools.tailoring.objective import OBJECTIVE_MODEL

log = get_logger("tools.tailoring")

# ---------------------------------------------------------------------------
# The fallback: modeled from obs.llm_cost's published rates for the model
# tools.tailoring.objective actually calls, and that module's own prompt
# shape — a model of the cost, not an observation of one.
# ---------------------------------------------------------------------------
#
# OBJECTIVE_PROMPT plus a filled template rarely exceeds ~500 input tokens,
# output is capped by the prompt's own "2 sentences, ~40 words" instruction to
# roughly 60-80, and thinking is capped by _OBJECTIVE_THINKING. This prices
# the ceiling, which is the safe direction for a number a quote is built on.
_MODELED_INPUT_TOKENS = 500
_MODELED_OUTPUT_TOKENS = 80
_MODELED_THINKING_TOKENS = 512  # the full thinking_budget ceiling

#: Priced by :func:`obs.llm_cost.compute_cost_usd` rather than by multiplying
#: out a local copy of the per-million rates.
#:
#: ``obs.llm_cost`` is this repo's price list and Google revises those rates,
#: so a second local copy would go stale silently the day the canonical table
#: is updated. Asking the pricer also inherits its cached-token and
#: long-context handling for free.
#:
#: ``OBJECTIVE_MODEL`` comes from the module that makes the call, so the model
#: priced here cannot drift from the one used. A model with no pricing entry
#: returns ``None``, which raises here rather than poisoning a quote with a
#: silent zero.
_ESTIMATED = compute_cost_usd(
    model=OBJECTIVE_MODEL,
    input_tokens=_MODELED_INPUT_TOKENS,
    output_tokens=_MODELED_OUTPUT_TOKENS,
    thinking_tokens=_MODELED_THINKING_TOKENS,
    cached_tokens=0,
)
# Guarded by test_the_objective_model_is_priced_by_the_canonical_table.
if _ESTIMATED is None:  # pragma: no cover
    raise RuntimeError(
        f"obs.llm_cost has no pricing entry for {OBJECTIVE_MODEL!r}; "
        "the tailoring rate cannot be modeled without one"
    )
ESTIMATED_COST_PER_OBJECTIVE_USD = _ESTIMATED

#: Where a rate came from, when it is not the user's own ledger. Distinct from
#: ``matching.rates.SOURCE_MEASURED``, which promises a dated, checkable run
#: behind the number; conflating them would make an estimate look like a
#: measurement to whatever reads ``rate_source``.
SOURCE_ESTIMATED = "estimated_no_ledger_yet"

#: The fallback, as a :class:`Rate`. ``sample=0`` marks it as not-observed,
#: same convention as ``matching.rates.MEASURED``.
ESTIMATED = Rate(ESTIMATED_COST_PER_OBJECTIVE_USD, SOURCE_ESTIMATED, 0)

# 20 smooths over an outlier JD without asking for a sample ordinary usage
# cannot produce.
_MIN_TAILORED = 20

# Must be wide enough that one tailored job per run — the ordinary case —
# reaches _MIN_TAILORED. At 10 it was not, so observed_rate could never fire
# and every quote fell back to ESTIMATED. Kept equal to
# matching.rates._RECENT_RUNS, which is what the docstring below claims.
#
# Not a complete fix: this window counts runs of *every* pipeline, so matching
# runs crowd tailoring ones out of it. Filtering to runs carrying a `tailored`
# count would need a composite index.
_RECENT_RUNS = 30


async def observed_rate(db, user_id: str, *, min_tailored: int = _MIN_TAILORED) -> Rate:
    """This user's own $/tailored-application over their most recent runs.

    Mirrors ``tools.matching.rates.observed_rate`` — same ledger collection,
    same recent-runs window, same rule that a doc with a zero count is
    skipped rather than averaged in as free. The only difference is reading
    the ``tailored`` leaf instead of ``scored``, which also means runs from
    other pipelines fall out of the skip for free.

    Never raises: a Firestore failure degrades to :data:`ESTIMATED` rather
    than failing the quote.
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
        tailored = 0
        async for snap in query.stream():
            doc = snap.to_dict() or {}
            jobs_tailored = int((doc.get("jobs") or {}).get("tailored") or 0)
            if jobs_tailored <= 0:
                continue
            usd = float((doc.get("llm") or {}).get("cost_usd") or 0.0)
            if usd <= 0:
                continue
            cost += usd
            tailored += jobs_tailored
        if tailored >= min_tailored and cost > 0:
            return Rate(cost / tailored, SOURCE_OBSERVED, tailored)
    except Exception:
        log.exception("tailoring.rates.observed_failed", user_id=user_id)
    return ESTIMATED
