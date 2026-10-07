# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The shared Firestore fakes' write semantics, pinned against Firestore's.

A fake that merges wrongly lets a leaf-merge bug pass every test written
against it, so the two ``set`` modes are pinned here directly.
"""

from __future__ import annotations

import asyncio

import pytest
from firestore_fakes import FakeQueryDB, FakeStoreDB, _FakeSyncDoc, apply_set
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

STORED = {
    "state": {
        "metrics": {"run_id": "old", "batch_run": "r-old", "budget_granted": 200},
        "at": "old-at",
        "lease": "held",
        "other": "keep",
    },
    "settings": {"on": True},
}


def test_merge_true_merges_nested_maps_leaf_by_leaf():
    doc = apply_set(STORED, {"state": {"metrics": {"run_id": "new"}}}, merge=True)

    # Firestore's documented leaf merge: the stale siblings survive.
    assert doc["state"]["metrics"] == {
        "run_id": "new",
        "batch_run": "r-old",
        "budget_granted": 200,
    }
    assert doc["state"]["other"] == "keep" and doc["settings"] == {"on": True}


def test_a_field_path_list_replaces_a_listed_map_whole():
    data = {
        "state": {
            "metrics": {"run_id": "new"},
            "at": "new-at",
            "lease": firestore.DELETE_FIELD,
            "other": "not listed, so dropped",
        },
        "settings": {"on": False},
    }
    doc = apply_set(STORED, data, merge=["state.metrics", "state.at", "state.lease"])

    assert doc["state"]["metrics"] == {"run_id": "new"}
    assert doc["state"]["at"] == "new-at"
    assert "lease" not in doc["state"]
    # Keys in ``data`` outside the list are not written.
    assert doc["state"]["other"] == "keep"
    assert doc["settings"] == {"on": True}


def test_delete_field_under_merge_true_removes_only_that_leaf():
    doc = apply_set(STORED, {"state": {"lease": firestore.DELETE_FIELD}}, merge=True)
    assert "lease" not in doc["state"] and doc["state"]["at"] == "old-at"


def test_a_set_without_merge_replaces_the_document_and_refuses_delete_field():
    assert apply_set(STORED, {"x": 1}) == {"x": 1}
    with pytest.raises(ValueError):
        apply_set(STORED, {"x": firestore.DELETE_FIELD})


def test_a_merge_path_missing_from_data_is_refused_like_firestore():
    with pytest.raises(ValueError):
        apply_set(STORED, {"state": {}}, merge=["state.metrics"])


def test_apply_set_never_aliases_its_inputs():
    data = {"state": {"metrics": {"run_id": "new"}}}
    doc = apply_set(STORED, data, merge=["state.metrics"])
    doc["state"]["metrics"]["run_id"] = "mutated"
    assert data["state"]["metrics"]["run_id"] == "new"
    assert STORED["state"]["metrics"]["run_id"] == "old"


def test_the_document_fakes_write_through_apply_set():
    store = {"state": {"a": 1, "b": 2}}
    _FakeSyncDoc(store).set({"state": {"a": 9}}, merge=True)
    assert store == {"state": {"a": 9, "b": 2}}
    _FakeSyncDoc(store).set({"state": {"a": 7}}, merge=["state"])
    assert store == {"state": {"a": 7}}

    db = FakeStoreDB({"c": {"d": {"state": {"a": 1, "b": 2}}}})
    asyncio.run(db.collection("c").document("d").set({"state": {"a": 9}}, merge=True))
    assert db.data["c"]["d"] == {"state": {"a": 9, "b": 2}}


def test_the_query_fake_honours_in_filters():
    db = FakeQueryDB(
        {"runs": {"r1": {"s": "running"}, "r2": {"s": "failed"}, "r3": {"s": "done"}}}
    )

    async def ids():
        q = db.collection("runs").where(
            filter=FieldFilter("s", "in", ["running", "failed"])
        )
        return [snap.id async for snap in q.stream()]

    assert asyncio.run(ids()) == ["r1", "r2"]
