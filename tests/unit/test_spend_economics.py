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
        self._order = None
        self._limit = None

    def document(self, doc_id):
        return _Doc(self._store, f"{self._path}/{doc_id}")

    def order_by(self, field, direction=None):
        # Honoured, not swallowed: both observed_rate windows are a ``limit``
        # on an ordered stream, and a fake that ignores either one cannot tell
        # a reachable threshold from an unreachable one.
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
    # Matching alone clears its own min_jobs; tailoring has no runs at all.
    rate = asyncio.run(economics.observed(db, "u1"))
    assert rate.match.source == matching_rates.SOURCE_OBSERVED
    assert rate.tailor is tailoring_rates.ESTIMATED
    assert not rate.fully_observed

    # Dated after the matching runs, because both legs read the *same* runs
    # collection and each other's docs consume the window: 20 tailoring runs
    # interleaved with these 20 matching ones would put only 15 of each inside
    # a 30-doc window, and 15 is under _MIN_TAILORED.
    for i in range(20):
        db.store[f"users/u1/runs/tailor{i}"] = {
            "ended_at": f"2026-10-{i + 1:02d}T00:00:00+00:00",
            "llm": {"cost_usd": 0.002},
            "jobs": {"tailored": 1},
        }
    rate = asyncio.run(economics.observed(db, "u1"))
    assert rate.tailor.source == matching_rates.SOURCE_OBSERVED
    assert rate.fully_observed


def test_the_two_legs_compete_for_one_window(db):
    """Both rates read ``users/{uid}/runs`` and take the newest N of *all*
    runs, so a matching run consumes a slot tailoring could have used.

    A user who tailors one job per run needs 20 tailoring runs inside the
    window to be quoted their own rate; interleaved matching runs push them
    out. Widening the window helps but does not remove this — only filtering
    the query to runs that carry a ``tailored`` count would, and that needs a
    composite index. Asserted so the limitation is visible rather than
    surfacing as an unexplained fallback.
    """
    for i in range(20):
        day = f"2026-09-{i + 1:02d}T00:00:00+00:00"
        db.store[f"users/u1/runs/match{i}"] = {
            "ended_at": day,
            "llm": {"cost_usd": 2.00},
            "jobs": {"scored": 200},
        }
        db.store[f"users/u1/runs/tailor{i}"] = {
            "ended_at": day,
            "llm": {"cost_usd": 0.002},
            "jobs": {"tailored": 1},
        }

    rate = asyncio.run(economics.observed(db, "u1"))
    assert rate.match.source == matching_rates.SOURCE_OBSERVED
    assert rate.tailor is tailoring_rates.ESTIMATED, (
        "20 tailoring runs exist, but matching runs took half the window"
    )
    assert not rate.fully_observed


def test_a_firestore_outage_still_returns_a_summable_rate(db):
    class _Boom:
        def collection(self, *a, **kw):
            raise RuntimeError("firestore is down")

    rate = asyncio.run(economics.observed(_Boom(), "u1"))
    assert rate.usd_per_application == pytest.approx(
        matching_rates.MEASURED_RATED.usd_per_job
        + tailoring_rates.ESTIMATED.usd_per_job
    )
