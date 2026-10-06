# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""tools.tailoring.rates: the observed $/application rate, mirroring
tools.matching.rates' own test coverage (see test_spend_consent.py) so the
two domains are held to the same standard — a run with no outcome must not
be averaged in as free, and a thin sample must not overrule the fallback.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.matching.rates import SOURCE_OBSERVED
from tools.tailoring import rates

# --------------------------------------------------------------------------
# Same fake-Firestore harness as test_spend_consent.py — paths in, docs out.
# --------------------------------------------------------------------------


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _Collection(self._store, f"{self._path}/{name}")


class _Collection:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    def document(self, doc_id):
        return _Doc(self._store, f"{self._path}/{doc_id}")

    def order_by(self, field, direction=None):
        # Honoured, not swallowed, exactly as in test_spend_consent.py:
        # ``observed_rate``'s window is a ``limit`` on an ordered stream, and
        # a fake that ignores either one cannot tell a reachable threshold
        # from an unreachable one.
        self._order = (field, str(direction).upper().endswith("DESCENDING"))
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def stream(self):
        docs = []
        for path in sorted(self._store):
            head, _, tail = path.rpartition("/")
            if head == self._path and tail:
                docs.append(self._store[path])
        if self._order is not None:
            field, descending = self._order
            docs.sort(key=lambda d: d.get(field) or "", reverse=descending)
        if self._limit is not None:
            docs = docs[: self._limit]
        for doc in docs:
            yield _Snap(doc)


class _DB:
    def __init__(self, docs=None):
        self.store = dict(docs or {})

    def collection(self, name):
        return _Collection(self.store, name)


@pytest.fixture
def db():
    return _DB()


def _seed(db, n, *, tailored, usd, month=9, prefix="r"):
    """``n`` completed tailoring runs, one per day, oldest first."""
    for i in range(n):
        db.store[f"users/u1/runs/{prefix}{month}-{i:02d}"] = {
            "ended_at": f"2026-{month:02d}-{i + 1:02d}T00:00:00+00:00",
            "llm": {"cost_usd": usd},
            "jobs": {"tailored": tailored},
        }


def test_estimated_fallback_has_no_sample_behind_it():
    """The unmeasured fallback must say so — sample=0 is what a caller (and
    the estimate.py "always show provenance" rule) reads as "not observed"."""
    assert rates.ESTIMATED.sample == 0
    assert rates.ESTIMATED.source == rates.SOURCE_ESTIMATED
    assert rates.ESTIMATED.source != SOURCE_OBSERVED
    assert rates.ESTIMATED.usd_per_job > 0


def test_a_thin_sample_falls_back_to_estimated(db):
    """Below min_tailored, even real spend with a real count must not
    overrule the fallback — same rule as matching.rates' min_jobs."""
    db.store["users/u1/runs/r1"] = {
        "ended_at": "2026-09-01T00:00:00+00:00",
        "llm": {"cost_usd": 0.03},
        "jobs": {"tailored": 3},
    }
    thin = asyncio.run(rates.observed_rate(db, "u1"))
    assert thin is rates.ESTIMATED


def test_enough_tailored_runs_produce_an_observed_rate(db):
    """Two applications per run over the full ``_RECENT_RUNS`` window is the
    cheapest history that reaches ``_MIN_TAILORED`` — see
    ``test_the_recent_runs_window_can_starve_the_threshold`` for why one per
    run cannot, however many runs there are."""
    _seed(db, 10, tailored=2, usd=0.004)
    mine = asyncio.run(rates.observed_rate(db, "u1"))
    assert mine.source == SOURCE_OBSERVED
    assert mine.sample == 20
    assert mine.usd_per_job == pytest.approx(0.002)


def test_the_recent_runs_window_can_starve_the_threshold(db):
    """``_RECENT_RUNS`` is 10 and ``_MIN_TAILORED`` is 20, so an account that
    tailors one application per run never produces an observed rate, however
    long its history: the query reads 10 docs and stops.

    Asserted rather than fixed. Raising the window or lowering the threshold
    is a judgement about how far back a rate may reach, which belongs to
    whoever owns the quote; what must not happen is this going unnoticed
    because a fake ignored ``limit`` and the suite reported an observed rate
    Firestore would never return.
    """
    _seed(db, 20, tailored=1, usd=0.002)
    assert asyncio.run(rates.observed_rate(db, "u1")) is rates.ESTIMATED


def test_the_window_reads_the_newest_runs_not_an_arbitrary_ten(db):
    """The rate moves with prompt and model changes, so a window that drifted
    onto old docs would quote the past as the present."""
    _seed(db, 10, tailored=2, usd=0.02, month=8)  # older, and 5x the price
    _seed(db, 10, tailored=2, usd=0.004, month=9)
    mine = asyncio.run(rates.observed_rate(db, "u1"))
    assert mine.sample == 20
    assert mine.usd_per_job == pytest.approx(0.002)


def test_a_run_that_spent_without_completing_is_skipped_not_averaged_in(db):
    """A discarded-mid-run or post-generate-failure tailoring run banks real
    spend with no ``jobs.tailored`` (api.routes.applications never sets
    ``tailored`` on those paths) — counting it as zero would divide real
    dollars by nothing, same trap matching.rates guards against."""
    _seed(db, 12, tailored=3, usd=0.006, prefix="good")
    db.store["users/u1/runs/discarded"] = {
        "ended_at": "2026-09-30T00:00:00+00:00",
        "llm": {"cost_usd": 0.05},
        "jobs": {},
    }
    mine = asyncio.run(rates.observed_rate(db, "u1"))
    # The discarded doc has jobs={} (tailored never set on that outcome path
    # — see api.routes.applications), so it falls out on jobs_tailored <= 0
    # regardless of how recent it is; its cost must not appear.
    assert mine.usd_per_job == pytest.approx(0.002)


def test_matching_runs_do_not_pollute_the_tailoring_rate(db):
    """A matching-run doc has jobs.scored, not jobs.tailored — it must fall
    out of the <= 0 skip for free, with no runner filter needed."""
    _seed(db, 12, tailored=3, usd=0.006, prefix="tailor")
    db.store["users/u1/runs/matching9"] = {
        "ended_at": "2026-09-30T00:00:00+00:00",
        "llm": {"cost_usd": 2.00},
        "jobs": {"scored": 200},
    }
    mine = asyncio.run(rates.observed_rate(db, "u1"))
    assert mine.usd_per_job == pytest.approx(0.002)


def test_a_firestore_failure_degrades_to_estimated_not_an_exception(db):
    class _Boom:
        def collection(self, *a, **kw):
            raise RuntimeError("firestore is down")

    result = asyncio.run(rates.observed_rate(_Boom(), "u1"))
    assert result is rates.ESTIMATED


def test_the_objective_model_is_priced_by_the_canonical_table():
    """The fallback asks ``obs.llm_cost`` for the price of the model
    ``tools.tailoring.objective`` actually calls, instead of carrying its own
    copy of $0.30/$2.50. That only works while the table has an entry for it,
    and the consequence of it not having one is not a bad number — ``rates``
    raises at **import**, so an unpriced model id takes the service down at
    startup rather than quietly poisoning a quote. This is the test that stands
    between a one-character model-id edit and that outage.
    """
    from obs.llm_cost import compute_cost_usd
    from tools.tailoring.objective import OBJECTIVE_MODEL

    priced = compute_cost_usd(
        model=OBJECTIVE_MODEL,
        input_tokens=1000,
        output_tokens=0,
        thinking_tokens=0,
        cached_tokens=0,
    )
    assert priced is not None, f"{OBJECTIVE_MODEL} has no entry in obs.llm_cost"
    assert rates.ESTIMATED_COST_PER_OBJECTIVE_USD > 0


def test_the_objective_model_is_a_pinned_id_not_a_moving_alias():
    """Tailoring declares its own model id — deliberately, so it can move
    without retuning the scorer — which also means a pin applied to
    ``llm_models.FLASH_MODEL`` does not reach it. It was left on
    ``gemini-flash-latest`` once; pinned here too, to the same concrete id.

    The literal is checked rather than equality with ``FLASH_MODEL``, because
    the two being equal is a fact about today and not a requirement: moving
    tailoring to a different model on purpose should mean editing this literal,
    while leaving it on a ``-latest`` alias is the thing this test refuses.
    """
    from tools.tailoring.objective import OBJECTIVE_MODEL

    assert OBJECTIVE_MODEL == "gemini-2.5-flash"
    assert not OBJECTIVE_MODEL.endswith("-latest")


def test_the_modeled_rate_is_the_canonical_price_not_a_local_copy():
    """Re-derives the fallback through the pricer. A hand-maintained duplicate
    of the per-million rates would drift from this the moment Google's pricing
    page changes — which the price table's own comment says to expect."""
    from obs.llm_cost import compute_cost_usd
    from tools.tailoring.objective import OBJECTIVE_MODEL

    assert rates.ESTIMATED_COST_PER_OBJECTIVE_USD == compute_cost_usd(
        model=OBJECTIVE_MODEL,
        input_tokens=rates._MODELED_INPUT_TOKENS,
        output_tokens=rates._MODELED_OUTPUT_TOKENS,
        thinking_tokens=rates._MODELED_THINKING_TOKENS,
        cached_tokens=0,
    )
