# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""tools.spend.economics: the combined match+tailor rate a price is built on."""

from __future__ import annotations

import asyncio

import pytest

from tools.matching import rates as matching_rates
from tools.spend import economics
from tools.tailoring import rates as tailoring_rates


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

    def document(self, doc_id):
        return _Doc(self._store, f"{self._path}/{doc_id}")

    def order_by(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    async def stream(self):
        for path in sorted(self._store):
            head, _, tail = path.rpartition("/")
            if head == self._path and tail:
                yield _Snap(self._store[path])


class _DB:
    def __init__(self, docs=None):
        self.store = dict(docs or {})

    def collection(self, name):
        return _Collection(self.store, name)


@pytest.fixture
def db():
    return _DB()


def test_with_no_history_both_legs_fall_back_and_still_sum(db):
    """The match leg's fallback must be MEASURED_RATED (cost of one delivered
    match), not the blended MEASURED constant observed_rate itself falls back
    to (cost of attempting N jobs off the backlog — a different question,
    answered for tools.matching.budget/tools.spend.estimate)."""
    rate = asyncio.run(economics.observed(db, "u1"))
    assert rate.match is matching_rates.MEASURED_RATED
    assert rate.tailor is tailoring_rates.ESTIMATED
    assert rate.usd_per_application == pytest.approx(
        matching_rates.MEASURED_RATED.usd_per_job
        + tailoring_rates.ESTIMATED.usd_per_job
    )
    assert not rate.fully_observed


def test_fully_observed_only_when_both_legs_are_the_users_own_history(db):
    for i in range(20):
        db.store[f"users/u1/runs/match{i}"] = {
            "ended_at": f"2026-09-{i + 1:02d}T00:00:00+00:00",
            "llm": {"cost_usd": 2.00},
            "jobs": {"scored": 200},
        }
    # Matching alone clears its own min_jobs (100 scored is enough with one
    # doc); tailoring is still below its own threshold.
    rate = asyncio.run(economics.observed(db, "u1"))
    assert rate.match.source == matching_rates.SOURCE_OBSERVED
    assert rate.tailor is tailoring_rates.ESTIMATED
    assert not rate.fully_observed

    for i in range(20):
        db.store[f"users/u1/runs/tailor{i}"] = {
            "ended_at": f"2026-09-{i + 1:02d}T00:00:00+00:00",
            "llm": {"cost_usd": 0.002},
            "jobs": {"tailored": 1},
        }
    rate = asyncio.run(economics.observed(db, "u1"))
    assert rate.tailor.source == matching_rates.SOURCE_OBSERVED
    assert rate.fully_observed


def test_a_firestore_outage_still_returns_a_summable_rate(db):
    class _Boom:
        def collection(self, *a, **kw):
            raise RuntimeError("firestore is down")

    rate = asyncio.run(economics.observed(_Boom(), "u1"))
    assert rate.usd_per_application == pytest.approx(
        matching_rates.MEASURED_RATED.usd_per_job
        + tailoring_rates.ESTIMATED.usd_per_job
    )
