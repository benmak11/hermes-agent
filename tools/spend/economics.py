# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cost of one fully-completed application — the number a price has to beat.

``tools.matching.rates`` prices parse+score; ``tools.tailoring.rates`` prices
the objective rewrite. Neither alone answers "what does it cost us to walk one
job from discovered to ready-for-review", and a pricing decision built on only
one leg understates the true cost — which is the wrong direction to be wrong
in for a number that decides whether a tier is profitable.

**What this deliberately excludes, and why that's not "left out" so much as
out of scope for this module:**

- *Submission.* Confirmed zero LLM cost — the live path (Greenhouse) is
  Playwright automation with no model call in it; the "Computer Use" browser
  path for other ATSes is still deferred per the README, and would need its
  own ``record_llm_call`` call site and its own rate module the day it ships.
  Zero is a fact here, not a gap.
- *Discovery / Google Jobs fetch.* A real cost, but not an LLM-token cost —
  ``obs.llm_cost`` prices ``generate_content`` calls, and discovery's source
  fetches never call one. It belongs in a per-active-user infra cost line
  (API quota/query pricing), not blended into a $/application figure whose
  whole point is "priced the same way, from the same ledger."

So :func:`observed` answers *the LLM-token portion* of one application's cost,
precisely and with each leg's own provenance attached — not "all-in" unit
economics, which still needs a discovery infra number added on top.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.matching import rates as matching_rates
from tools.tailoring import rates as tailoring_rates


@dataclass(frozen=True)
class FunnelRate:
    """One application's LLM cost, broken out by leg and summed.

    Each leg keeps its own :class:`~tools.matching.rates.Rate` — its own
    ``source`` and ``sample`` — rather than blending into one opaque number,
    because the two legs can have very different provenance at once (a
    heavily-used account's matching rate is observed from hundreds of scored
    jobs while its tailoring rate is still the modeled fallback, since far
    fewer jobs get approved than get scored). Collapsing that into a single
    ``source`` string would misrepresent whichever leg is weaker.
    """

    match: matching_rates.Rate
    tailor: tailoring_rates.Rate
    usd_per_application: float

    @property
    def fully_observed(self) -> bool:
        """True only when *both* legs are the account's own measured history.

        A pricing decision built on this figure should know whether it is
        looking at two real measurements or one real measurement propped up
        by a modeled fallback — this is that flag, rather than making the
        caller compare both ``source`` strings against
        ``matching_rates.SOURCE_OBSERVED`` itself.
        """
        return (
            self.match.source == matching_rates.SOURCE_OBSERVED
            and self.tailor.source == matching_rates.SOURCE_OBSERVED
        )


async def observed(db, user_id: str) -> FunnelRate:
    """This user's own LLM cost per application: matching's rate + tailoring's.

    Two independent reads (each already degrades to its own fallback on a
    thin sample or a Firestore hiccup — see both modules' ``observed_rate``),
    so this never raises either.

    ``matching_rates.observed_rate``'s own fallback
    (:data:`~tools.matching.rates.MEASURED`) is the blended average over every
    job a run *attempted*, free rejects included — so a fresh account with no
    history yet gets :data:`~tools.matching.rates.MEASURED_RATED` here
    instead, the fallback priced per rated job. A real account's observed
    rate already divides by ``jobs.scored`` and needs no swap.
    """
    match = await matching_rates.observed_rate(db, user_id)
    if match is matching_rates.MEASURED:
        match = matching_rates.MEASURED_RATED
    tailor = await tailoring_rates.observed_rate(db, user_id)
    return FunnelRate(
        match=match,
        tailor=tailor,
        usd_per_application=round(match.usd_per_job + tailor.usd_per_job, 6),
    )
