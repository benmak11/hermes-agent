# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What ``run_tailoring`` banks as a *tailored* job, and what it doesn't.

``tools.tailoring.rates.observed_rate`` divides a user's real spend by
``jobs.tailored`` to get $/application. That denominator is only honest if it
counts applications that were actually produced — a run that spent an LLM call
and then discarded the result still cost money, and counting it would make the
rate look *cheaper* than it is. Underpricing is the wrong direction for a
figure a paywall tier is built on.

The reader half of that contract is covered in ``test_tailoring_rates.py``
(a ledger doc with no ``tailored`` count is skipped, not averaged in as zero).
This file covers the writer half: that ``run_tailoring`` only ever banks the
count on the path that really published an application.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_api_profile import _profile_payload

import api.routes.applications as applications

JOB = {
    "id": "job1",
    "user_id": "u1",
    "title": "Senior Software Engineer",
    "company": "Acme",
    "url": "https://boards.greenhouse.io/acme/jobs/1",
    "source": "greenhouse",
    "source_id": "1",
    "location": "Remote, United States",
    "jd_raw": "Build things.",
    "discovered_at": "2026-09-28T00:00:00+00:00",
}


class _Doc:
    def __init__(self, data):
        self.data = dict(data)
        self.exists = True

    def to_dict(self):
        return dict(self.data)

    def get(self):
        return self

    def update(self, patch):
        self.data.update(patch)


class _Col:
    def __init__(self, docs):
        self._docs = docs

    def document(self, doc_id):
        return self._docs.setdefault(doc_id, _Doc({}))


class _UserRef(_Doc):
    def __init__(self, data, cols):
        super().__init__(data)
        self._cols = cols

    def collection(self, name):
        return _Col(self._cols.setdefault(name, {}))


class _DB:
    def __init__(self, user_ref):
        self._user_ref = user_ref

    def collection(self, name):
        assert name == "users"
        return SimpleNamespace(document=lambda _uid: self._user_ref)


@pytest.fixture
def world(monkeypatch):
    """``run_tailoring`` with every seam faked but the ledger call recorded."""
    cols: dict = {}
    user_ref = _UserRef(_profile_payload(), cols)
    cols["jobs"] = {"job1": _Doc({**JOB, "user_decision": "approved"})}
    cols["applications"] = {}
    db = _DB(user_ref)

    banked: list[dict] = []

    async def fake_persist(_client, user_id, run_id, **kw):
        banked.append(kw)

    async def fake_open(*a, **kw):
        return None

    monkeypatch.setattr(applications, "_client", lambda: db)
    monkeypatch.setattr(applications, "persist_run_cost", fake_persist)
    monkeypatch.setattr(applications, "open_run", fake_open)
    monkeypatch.setattr(
        applications, "_dismiss_if_posting_removed", lambda *a, **kw: _false()
    )
    return SimpleNamespace(db=db, cols=cols, banked=banked, monkeypatch=monkeypatch)


async def _false():
    return False


def _run():
    asyncio.run(applications.run_tailoring("u1", "job1"))


def test_a_run_that_never_claimed_banks_no_tailored_count(world):
    """The cheapest exit: another task owns the work, so nothing was spent and
    certainly nothing was produced."""
    world.monkeypatch.setattr(applications, "_transition", lambda *a, **kw: _false())

    _run()

    (kw,) = world.banked
    assert kw.get("jobs") is None, "a run that claimed nothing banked a tailored job"


def test_a_discarded_run_banks_its_spend_but_no_tailored_count(world):
    """**The case the denominator exists for.** The claim won, the LLM call was
    made and paid for, and then the user's undo turned the approval back — so
    the spend is real and the application is not. Counting it would divide real
    money by a job that does not exist."""
    world.monkeypatch.setattr(applications, "_transition", lambda *a, **kw: _true())

    async def fake_tailor(job, profile, *, upload):
        # The undo lands while the model call is on the wire.
        world.cols["jobs"]["job1"].data["user_decision"] = "skipped"
        return SimpleNamespace(model_dump=lambda mode: {}, resume_variant_uri=None)

    world.monkeypatch.setattr(applications, "tailor_application", fake_tailor)

    _run()

    (kw,) = world.banked
    assert kw.get("jobs") is None, "a discarded run counted as a tailored job"
    assert kw.get("state") == applications.DONE


def test_a_run_that_lost_the_publish_race_banks_no_tailored_count(world):
    """The reaper deliberately manufactures overlapping runs: run A's lease
    lapses, the document is requeued, run B claims it. A finishes late, its
    publish swap loses to B, and B publishes. Both spent; one application
    exists. If A also banked a count, two runs' spend would be divided by two
    for one delivered application — the rate halved, the wrong direction."""
    targets: list[str] = []

    async def transition(_ref, target, **kw):
        targets.append(target)
        return target != "ready_for_review"  # claim wins, publish loses

    world.monkeypatch.setattr(applications, "_transition", transition)

    async def fake_tailor(job, profile, *, upload):
        return SimpleNamespace(
            model_dump=lambda mode: {"resume_variant_uri": "gs://b/r.docx"},
            resume_variant_uri="gs://b/r.docx",
        )

    world.monkeypatch.setattr(applications, "tailor_application", fake_tailor)

    _run()

    # The publish was attempted — otherwise this test passes for the wrong reason.
    assert targets == ["tailoring", "ready_for_review"]
    (kw,) = world.banked
    assert kw.get("jobs") is None, "a run that lost the publish counted as tailored"
    assert kw.get("state") == applications.DONE


def test_a_successful_run_banks_exactly_one_tailored_job(world):
    """The positive control. Without it, hard-coding ``jobs=None`` would pass
    every test above and the rate would never see a denominator at all."""
    world.monkeypatch.setattr(applications, "_transition", lambda *a, **kw: _true())

    async def fake_tailor(job, profile, *, upload):
        return SimpleNamespace(
            model_dump=lambda mode: {"resume_variant_uri": "gs://b/r.docx"},
            resume_variant_uri="gs://b/r.docx",
        )

    world.monkeypatch.setattr(applications, "tailor_application", fake_tailor)

    _run()

    (kw,) = world.banked
    assert kw.get("jobs") == {"tailored": 1}


async def _true():
    return True
