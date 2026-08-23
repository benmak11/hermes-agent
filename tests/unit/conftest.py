# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared fixtures for the unit suite."""

import pytest

from tools.matching import budget


@pytest.fixture
def unlimited_budget(monkeypatch):
    """Grant every scorer whatever it asks for, without touching Firestore.

    The per-user scoring budget (``tools.matching.budget``) fronts all three
    scoring entry points, so tests *about scoring* have to get past it to
    still be testing scoring. Opt in here; the gate itself is pinned in
    ``test_scoring_budget.py``, which uses the real (pure) implementation.
    """
    granted: list[int] = []

    async def fake_reserve(db, user_id, wanted=None, *, cycle_id=None, limits=None):
        limits = limits or budget.Limits.from_env()
        n = limits.per_cycle if wanted is None else wanted
        granted.append(n)
        return budget.Reservation(
            granted=n,
            capped=False,
            remaining_cycle=limits.per_cycle - n,
            remaining_day=limits.per_day - n,
            cycle_id=cycle_id,
        )

    async def fake_release(db, user_id, unused, *, cycle_id):
        pass

    monkeypatch.setattr(budget, "reserve", fake_reserve)
    monkeypatch.setattr(budget, "release", fake_release)
    return granted
