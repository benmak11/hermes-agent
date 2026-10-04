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

**No dated ledger measurement exists yet for this leg.** Unlike
``matching.rates.MEASURED_COST_PER_JOB_USD`` (a named, checkable run:
``users/{uid}/runs/37338813872b4197a860e49d380c2813``), the fallback below is
computed from ``obs.llm_cost``'s published Flash rates and the objective
prompt's typical token range — a model of the cost, not an observation of it.
Treat :data:`ESTIMATED` as a placeholder to retire the moment
:func:`observed_rate` has enough real runs behind it; it exists so a fresh
account gets *a* number instead of ``None`` propagating into a quote.
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
# OBJECTIVE_PROMPT plus a filled template/title/company/jd_summary rarely
# exceeds ~500 input tokens (the jd_summary fallback truncates the raw JD to
# 1000 chars ~= 250 tokens; the usual case is job.jd_parsed.summary, a few
# sentences, well under that). Output is capped by the prompt's own "2
# sentences, ~40 words" instruction to roughly 60-80 tokens. Thinking is
# capped by _OBJECTIVE_THINKING's thinking_budget=512 but need not spend the
# whole budget — this prices the ceiling, which is the safe direction for a
# number a quote will be built on.
_MODELED_INPUT_TOKENS = 500
_MODELED_OUTPUT_TOKENS = 80
_MODELED_THINKING_TOKENS = 512  # the full thinking_budget ceiling

#: Priced by :func:`obs.llm_cost.compute_cost_usd` rather than by multiplying
#: out a local copy of the per-million rates.
#:
#: ``obs.llm_cost`` is this repo's price list, and it says of itself that
#: Google revises these. That is reason enough for a second copy of
#: $0.30/$2.50 to go stale silently: the day the pricing page changes, the
#: canonical table is updated and a duplicate is not. (It used to be two
#: reasons — the other was ``gemini-flash-latest`` moving under us, which
#: ``OBJECTIVE_MODEL`` being pinned has now closed.) Asking the pricer means
#: this figure inherits its cached-token and long-context handling for free
#: instead of re-deriving a simplified version of the same arithmetic.
#:
#: ``OBJECTIVE_MODEL`` comes from the module that makes the call, so the model
#: priced here cannot drift from the model actually used. A model with no
#: pricing entry returns ``None``; that is a programming error rather than a
#: runtime condition — the pinned id is in the table, and a test pins that it
#: stays there — so it raises here instead of poisoning a quote with a silent
#: zero.
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

#: Where a rate came from, when it isn't the user's own ledger. Distinct from
#: ``matching.rates.SOURCE_MEASURED`` — that name promises a dated, checkable
#: run behind the number, and this one has none yet. Conflating the two would
#: make an estimate look like a measurement to whatever reads ``rate_source``.
SOURCE_ESTIMATED = "estimated_no_ledger_yet"

#: The fallback, as a :class:`Rate`. ``sample=0`` marks it as not-observed,
#: same convention as ``matching.rates.MEASURED``.
ESTIMATED = Rate(ESTIMATED_COST_PER_OBJECTIVE_USD, SOURCE_ESTIMATED, 0)

# Tailoring is usually one job per run (api.routes.applications' approve
# hook), not a batch like matching — so matching's min_jobs=100 would demand
# 100 separate approved-and-tailored applications from one account before
# ever trusting observed history, which most accounts will never reach. 20
# is enough to smooth over one or two outlier JDs without asking for a
# sample size ordinary usage can't realistically produce.
_MIN_TAILORED = 20

_RECENT_RUNS = 10


async def observed_rate(db, user_id: str, *, min_tailored: int = _MIN_TAILORED) -> Rate:
    """This user's own $/tailored-application over their most recent runs.

    Mirrors ``tools.matching.rates.observed_rate`` exactly — same ledger
    collection, same "recent runs only" windowing, same "a doc with zero
    count is skipped, not averaged in as zero" rule (see that function's
    docstring for why: a run that banked spend without an outcome must not
    drag the rate up by being counted as free). The only difference is which
    ``jobs`` leaf it reads: ``tailored`` here, ``scored`` there. Runs from
    other pipelines (matching, discovery) simply have no ``jobs.tailored``
    key and fall out of the ``<= 0`` skip below for free — no ``runner``
    filter is needed.

    Never raises: feeds a quote, and a Firestore hiccup must degrade to
    :data:`ESTIMATED` rather than fail the caller.
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
