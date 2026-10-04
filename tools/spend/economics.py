# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cost of one fully-completed application — the number a price has to beat.

``tools.matching.rates`` prices parse+score and ``tools.tailoring.rates``
prices the objective rewrite; neither alone says what it costs to walk one job
from discovered to ready-for-review, and pricing off one leg understates it.

:func:`observed` answers the *LLM-token* portion only, not all-in unit
economics. Submission is excluded because it is a confirmed zero — the live
Greenhouse path is Playwright with no model call — and discovery is excluded
because its source fetches are a real cost but not a token cost, so it belongs
in a per-active-user infra line rather than blended in here.
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

        Saves the caller comparing both ``source`` strings against
        ``matching_rates.SOURCE_OBSERVED`` to tell two real measurements from
        one propped up by a modeled fallback.
        """
        return (
            self.match.source == matching_rates.SOURCE_OBSERVED
            and self.tailor.source == matching_rates.SOURCE_OBSERVED
        )


async def observed(db, user_id: str) -> FunnelRate:
    """This user's own LLM cost per application: matching's rate + tailoring's.

    Two Firestore reads, each already degrading to its own fallback on a thin
    sample or a read failure, so this never raises. A fresh account's match
    leg is :data:`~tools.matching.rates.MEASURED_RATED` — priced per rated
    job, not the blended per-attempt average.
    """
    match = await matching_rates.observed_rate(db, user_id)
    tailor = await tailoring_rates.observed_rate(db, user_id)
    return FunnelRate(
        match=match,
        tailor=tailor,
        usd_per_application=round(match.usd_per_job + tailor.usd_per_job, 6),
    )
