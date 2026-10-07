# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Each cycle's ``discovery_state`` metrics map replaces the last one whole.

Written through :func:`firestore_fakes.apply_set`, which models Firestore's
merge semantics: under ``merge=True`` a find-only run's metrics would keep the
previous scoring run's ``batch_run`` / ``budget_*`` keys, and ``GET /activity``
would keep reporting that run's batch.
"""

from __future__ import annotations

import asyncio

from firestore_fakes import FakeQueryDB, _FakeSyncDoc
from test_discovery_fanout import _cycle_fakes

import api.routes.activity as activity
import api.routes.discovery as routes_discovery
from tools.matching import batch_runs

OLD_TAG = "batch-old-1"


def _old_state() -> dict:
    """The live shape: a Sept 26 scoring run's metrics, a held lease, and
    sibling fields the success write must not touch."""
    return {
        "discovery_settings": {"auto_discovery": False, "liveness_sweep": False},
        "discovery_state": {
            "last_discovery_at": "2026-09-26T19:11:19+00:00",
            "last_discovery": {
                "run_id": "run-old-1",
                "trigger": "manual",
                "scored_leg": True,
                "batch_run": OLD_TAG,
                "budget_granted": 200,
                "budget_remaining_day": 0,
                "unscored_backlog": 9219,
            },
            "discovery_lease": "2026-10-07T10:30:00+00:00",
            "last_sweep_at": "2026-10-06T08:00:00+00:00",
            "last_sweep": {"checked": 4, "removed": 1},
            "sweep_lease": "2026-10-07T11:00:00+00:00",
        },
    }


def _user_doc(monkeypatch, store: dict) -> None:
    monkeypatch.setattr(routes_discovery, "_user_ref", lambda uid: _FakeSyncDoc(store))


def test_a_find_only_run_replaces_the_last_runs_metrics_whole(monkeypatch):
    monkeypatch.setenv("QUEUE_MODE", "1")
    _cycle_fakes(monkeypatch, jobs=3)
    store = _old_state()
    _user_doc(monkeypatch, store)

    asyncio.run(
        routes_discovery.run_discovery_cycle("u1", trigger="manual", score=False)
    )

    state = store["discovery_state"]
    metrics = state["last_discovery"]
    assert metrics["run_id"] != "run-old-1" and metrics["scored_leg"] is False
    assert "batch_run" not in metrics
    assert not [k for k in metrics if k.startswith("budget_")]
    # The rest of the write behaves as it always did.
    assert state["last_discovery_at"] != "2026-09-26T19:11:19+00:00"
    assert "discovery_lease" not in state
    assert state["last_sweep_at"] == "2026-10-06T08:00:00+00:00"
    assert state["last_sweep"] == {"checked": 4, "removed": 1}
    assert state["sweep_lease"] == "2026-10-07T11:00:00+00:00"
    assert store["discovery_settings"] == {
        "auto_discovery": False,
        "liveness_sweep": False,
    }


def test_a_scoring_run_then_a_find_only_run_leaves_no_batch_tag(monkeypatch):
    """The sequence that produced the stale tag, end to end through the cycle."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    _cycle_fakes(monkeypatch, jobs=60)

    async def batch_scoring(user_id, **kw):
        return {
            "scored": 0,
            "discarded": 0,
            "failed": 0,
            "pending": 60,
            "batch_run": "r-new",
            "budget_granted": 3,
            "budget_remaining_day": 0,
        }

    monkeypatch.setattr(batch_runs, "score_or_start_run", batch_scoring)
    store: dict = {}
    _user_doc(monkeypatch, store)

    asyncio.run(routes_discovery.run_discovery_cycle("u1", trigger="cron"))
    scored = store["discovery_state"]["last_discovery"]
    assert scored["batch_run"] == "r-new" and scored["budget_granted"] == 3

    asyncio.run(
        routes_discovery.run_discovery_cycle("u1", trigger="manual", score=False)
    )
    found = store["discovery_state"]["last_discovery"]
    assert "batch_run" not in found
    assert not [k for k in found if k.startswith("budget_")]


def _activity_batch_item(monkeypatch, user_doc: dict) -> dict:
    db = FakeQueryDB(
        {
            "users": {"u1": user_doc},
            batch_runs.COLLECTION: {
                OLD_TAG: {
                    "user_id": "u1",
                    "state": "failed",
                    "stage": "parse",
                    "error": "cancelled by operator 2026-09-26",
                    "origin_run_id": "run-old-1",
                    "cost_banked_at": None,
                }
            },
        }
    )
    monkeypatch.setattr(activity, "_client", lambda: db)
    body = asyncio.run(activity.get_activity("u1"))
    return {item["kind"]: item for item in body["items"]}["batch_scoring"]


def test_the_panel_stops_reporting_the_old_batch_after_the_next_search(monkeypatch):
    store = _old_state()
    # Before: the stale tag points the panel at the failed run.
    assert _activity_batch_item(monkeypatch, store)["state"] == "failed"

    monkeypatch.setenv("QUEUE_MODE", "1")
    _cycle_fakes(monkeypatch, jobs=3)
    _user_doc(monkeypatch, store)
    asyncio.run(
        routes_discovery.run_discovery_cycle("u1", trigger="manual", score=False)
    )

    assert _activity_batch_item(monkeypatch, store)["state"] == "never_started"


def test_a_sweep_replaces_its_last_counts_whole(monkeypatch):
    async def not_deleted(user_id):
        return False

    async def fake_sweep(user_id):
        return {"checked": 2, "removed": 0, "boards_failed": 0}

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(routes_discovery, "_account_deleted", not_deleted)
    monkeypatch.setattr(routes_discovery, "sweep_postings", fake_sweep)
    monkeypatch.setattr(routes_discovery, "persist_run_cost", noop)
    monkeypatch.setattr(routes_discovery, "_extend_slot", lambda *a, **kw: True)
    monkeypatch.setattr(routes_discovery, "_release_slot", lambda *a, **kw: True)
    store = _old_state()
    store["discovery_state"]["last_sweep"]["legacy_key"] = "stale"
    _user_doc(monkeypatch, store)

    asyncio.run(routes_discovery.run_sweep_cycle("u1", trigger="cron"))

    state = store["discovery_state"]
    assert "legacy_key" not in state["last_sweep"]
    assert state["last_sweep"]["checked"] == 2 and state["last_sweep"]["run_id"]
    assert "sweep_lease" not in state
    assert state["last_sweep_at"] != "2026-10-06T08:00:00+00:00"
    # The discovery half of the map is untouched.
    assert state["last_discovery"]["batch_run"] == OLD_TAG
    assert state["discovery_lease"] == "2026-10-07T10:30:00+00:00"
