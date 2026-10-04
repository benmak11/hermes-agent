# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The vetting-decision event log (``users/{uid}/decisions``).

``decide()`` overwrites ``user_decision`` in place, so the job document holds
only the latest answer — no timestamp, no history, no record of the score the
user was looking at, and an undo erases the original choice. None of that can
be used as a training label, which is what these events exist to become.

What is pinned hardest is what could be silently wrong:

- the snapshot fields live under the job document's ``match`` key, not at the
  top level. Reading them off the document yields ``None`` for every job ever
  scored, and nothing fails;
- an *unscored* job must get ``score_snapshot: None``, not a dict of nulls —
  "never scored" and "scored, got null" are different facts;
- ``previous_decision`` has to come from the read taken *before* the update;
  taken afterwards it still produces plausible events and an unreconstructible
  chain.
"""

from __future__ import annotations

import asyncio
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, ServiceUnavailable

import api.routes.jobs as jobs_mod
from api.deps import verify_user
from models.job import Job
from tools import decisions
from tools.matching import score

# --- fakes ------------------------------------------------------------------


class _Snap:
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else dict(data)

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _Doc:
    """One document. ``update`` on an absent document raises ``NotFound``, the
    way Firestore does — that raise is what produces the route's 404."""

    def __init__(self, doc_id: str, data: dict | None = None, fail_get: bool = False):
        self.id = doc_id
        self.data = None if data is None else dict(data)
        self.deleted = False
        self._fail_get = fail_get

    def get(self):
        if self._fail_get:
            # The shape a read retry budget running out takes. Not NotFound:
            # that one means something, and this one must not.
            raise ServiceUnavailable("read retry budget exhausted")
        return _Snap(self.id, self.data)

    def set(self, data, merge=False):
        if merge and self.data is not None:
            self.data.update(data)
        else:
            self.data = dict(data)

    def update(self, fields, option=None):
        if self.data is None:
            raise NotFound("no such document")
        self.data.update(fields)

    def delete(self):
        self.data = None
        self.deleted = True


class _Collection:
    def __init__(self, docs: dict[str, _Doc]):
        self._docs = docs

    def document(self, doc_id: str) -> _Doc:
        return self._docs.setdefault(doc_id, _Doc(doc_id))


class _Decisions:
    """The event sink. ``add`` is the real client's auto-id write."""

    def __init__(self, events: list[dict], fail: bool = False):
        self.events = events
        self._fail = fail

    def add(self, data: dict):
        if self._fail:
            raise RuntimeError("firestore is having a day")
        self.events.append(dict(data))
        return (None, _Doc(f"evt{len(self.events)}", data))


class _User:
    def __init__(self, jobs: dict[str, _Doc], events: list[dict], fail: bool):
        self._jobs = jobs
        self._apps: dict[str, _Doc] = {}
        self.decisions = _Decisions(events, fail)

    def collection(self, name: str):
        if name == "jobs":
            return _Collection(self._jobs)
        if name == "applications":
            return _Collection(self._apps)
        if name == decisions.COLLECTION:
            return self.decisions
        raise AssertionError(f"unexpected collection {name!r}")


class _DB:
    def __init__(self, user: _User):
        self._user = user

    def collection(self, name: str):
        assert name == "users", name
        return _Collection({"u1": self._user})  # type: ignore[arg-type]


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def events() -> list[dict]:
    return []


@pytest.fixture
def world(events, monkeypatch):
    """A client factory plus the one seam approval reaches (tailoring
    dispatch), so nothing in these tests can queue or spend."""
    monkeypatch.setattr(jobs_mod, "dispatch_tailor", lambda *a, **k: True)
    # Skipping schedules a liveness probe as a background task, which
    # ``TestClient`` runs for real. Default it to "live" — the fail-open,
    # no-event answer — so no test reaches the network by accident; the
    # dismissal tests below patch it again and the later patch wins.
    monkeypatch.setattr(jobs_mod, "check_posting", _live)

    def build(
        job_doc: dict | None,
        *,
        fail: bool = False,
        fail_get: bool = False,
        job_id: str = "job1",
    ):
        jobs = {job_id: _Doc(job_id, job_doc, fail_get=fail_get)}
        user = _User(jobs, events, fail)
        monkeypatch.setattr(jobs_mod, "_client", lambda: _DB(user))
        app = FastAPI()
        app.include_router(jobs_mod.router)
        app.dependency_overrides[verify_user] = lambda: "u1"
        return TestClient(app), jobs[job_id], user

    return build


MATCH = {
    "overall_score": 78.5,
    "breakdown": {"skills": 30, "seniority": 20, "domain": 28.5},
    "recommendation": "apply",
    "reasoning": "close on the platform half",
    "gaps": ["kubernetes"],
    "job_id": "job1",
}


def _job(**extra) -> dict:
    return {
        "id": "job1",
        "user_id": "u1",
        "source": "greenhouse",
        "source_id": "1",
        "company": "acme",
        "title": "Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "jd_raw": "jd",
        "discovered_at": "2026-10-01T00:00:00+00:00",
        **extra,
    }


# --- the chain --------------------------------------------------------------


def test_approve_undo_reject_leaves_three_chained_events(world, events):
    """The headline case. ``previous_decision`` must come from the document as
    it was *before* each update, which is the only place the replaced answer
    still exists — read it from the new decision and the chain shifts by one
    and an undo can no longer be told from a first decision."""
    client, job, _ = world(_job(match=dict(MATCH)))

    for decision in ("approved", "pending", "rejected"):
        resp = client.post("/jobs/job1/decide", json={"decision": decision})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    assert [e["decision"] for e in events] == ["approved", "pending", "rejected"]
    assert [e["previous_decision"] for e in events] == [None, "approved", "pending"]
    assert {e["job_id"] for e in events} == {"job1"}
    assert {e["actor"] for e in events} == {"user"}
    # Ordered in time, and parseable as a UTC ISO timestamp.
    assert [e["decided_at"] for e in events] == sorted(e["decided_at"] for e in events)
    assert all(e["decided_at"].endswith("+00:00") for e in events)
    # The job document still carries the current state, unchanged in shape.
    assert job.data["user_decision"] == "rejected"


def test_the_chain_starts_from_the_decision_the_document_actually_held(world, events):
    """What a real queue looks like: a discovered job is persisted with
    ``user_decision: "pending"``, so the first event's previous value is
    ``pending``, not ``None``. Same read, so the same mutation breaks it — and
    this is the variant that would be indistinguishable from the new decision
    if the chain were taken from the body."""
    client, _, _ = world(_job(match=dict(MATCH), user_decision="pending"))

    for decision in ("starred", "pending", "approved"):
        client.post("/jobs/job1/decide", json={"decision": decision})

    assert [e["previous_decision"] for e in events] == ["pending", "starred", "pending"]
    assert [e["decision"] for e in events] == ["starred", "pending", "approved"]


def test_starring_is_logged_like_any_other_decision(world, events):
    client, _, _ = world(_job(match=dict(MATCH)))

    client.post("/jobs/job1/decide", json={"decision": "starred"})

    assert [(e["decision"], e["actor"]) for e in events] == [("starred", "user")]


# --- the 404 ----------------------------------------------------------------


def test_a_404_writes_no_event(world, events):
    """``NotFound`` is raised by the update, before anything changed, so there
    is no decision to record. An event here would be a label for a job that
    does not exist."""
    client, _, _ = world(None)

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 404
    assert resp.json() == {"detail": "job not found"}
    assert events == []


# --- the snapshot -----------------------------------------------------------


def test_a_scored_jobs_snapshot_is_read_from_the_match_map(world, events):
    """THE TRAP. ``overall_score``/``breakdown``/``recommendation`` are under
    ``match``; read at the top level they come back ``None`` for every job
    ever scored and nothing fails."""
    client, _, _ = world(_job(match=dict(MATCH)))

    client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert events[0]["score_snapshot"] == {
        "overall_score": 78.5,
        "breakdown": {"skills": 30, "seniority": 20, "domain": 28.5},
        "recommendation": "apply",
        # No ``scored_with`` on this document: it was scored before that field
        # existed. ``None`` is the honest answer, and the next two tests pin
        # that it is not quietly replaced by today's constants.
        "scored_with": None,
    }


def test_an_unscored_job_gets_no_snapshot_rather_than_a_dict_of_nulls(world, events):
    """ "We never scored this" and "we scored it and got null" are different
    facts. A model trained on the second when the first is true is learning
    from noise, so the distinction is in the data, not in a convention."""
    client, _, _ = world(_job())  # discovered, never scored: no ``match`` key

    client.post("/jobs/job1/decide", json={"decision": "rejected"})

    assert events[0]["score_snapshot"] is None


def test_the_snapshot_is_the_score_at_decision_time_not_after(world, events):
    """The undo path is the one that matters: the event must carry what the
    user was looking at, which is the pre-update document."""
    client, job, _ = world(_job(match=dict(MATCH)))

    client.post("/jobs/job1/decide", json={"decision": "approved"})
    job.data["match"]["overall_score"] = 12.0  # a rescore lands afterwards
    client.post("/jobs/job1/decide", json={"decision": "pending"})

    assert events[0]["score_snapshot"]["overall_score"] == 78.5
    assert events[1]["score_snapshot"]["overall_score"] == 12.0


def test_a_job_scored_before_this_pr_gets_a_null_scored_with(world, events):
    """The whole point of the provenance record is that a score can be dated.
    A job scored in October and decided in December carries no ``scored_with``
    — and filling that in from the constants *at decision time* would date it
    to December, asserting a comparability that is exactly backwards. ``None``
    is a fact; a reconstruction is a fabrication that nothing downstream can
    ever catch."""
    client, _, _ = world(_job(match=dict(MATCH)))  # no ``scored_with`` key

    client.post("/jobs/job1/decide", json={"decision": "approved"})

    snap = events[0]["score_snapshot"]
    assert snap is not None  # it *was* scored — that distinction is separate
    assert snap["scored_with"] is None
    # Not a record that merely looks empty, and not one built from the live
    # constants either.
    assert score.PRO_MODEL not in repr(snap)


def test_a_newly_scored_job_carries_its_real_provenance(world, events):
    """The other half: when the document does carry the record, the event must
    carry it through verbatim — a snapshot that drops it leaves the label
    store exactly as unattributable as it was before."""
    provenance = score.scored_with(
        parse_model="gemini-2.5-flash", match_model=score.PRO_MODEL
    )
    client, _, _ = world(_job(match=dict(MATCH), scored_with=provenance))

    client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert events[0]["score_snapshot"]["scored_with"] == provenance
    # Read off the document, not rebuilt: the batch Flash id survives, which
    # it could not if this came from ``tools.llm_models``.
    assert events[0]["score_snapshot"]["scored_with"]["parse_model"] == (
        "gemini-2.5-flash"
    )


def test_score_snapshot_ignores_an_empty_match_map():
    """Defensive, and cheap: a job doc that somehow carries ``match: {}`` has
    not been scored either."""
    assert decisions.score_snapshot({"match": {}}) is None
    assert decisions.score_snapshot(None) is None


def test_the_decision_literal_matches_the_field_it_describes():
    """``DecisionValue`` is a second copy of ``Job.user_decision``'s Literal.
    A copy nothing compares drifts: a new decision value added to the model
    would be type-checked against a stale list here, or quietly accepted."""
    assert set(get_args(decisions.DecisionValue)) == set(
        get_args(Job.model_fields["user_decision"].annotation)
    )


# --- the system's own decision ----------------------------------------------


async def _removed(job, **kwargs):
    return "removed"


async def _live(job, **kwargs):
    return "live"


def test_the_system_dismissal_is_logged_as_the_system(world, events, monkeypatch):
    """A dead posting is not a judgement about fit. Mixing it in with the
    user's skips teaches a model that removed postings are bad matches, so the
    writer is recorded."""
    _, job, user = world(_job(match=dict(MATCH), user_decision="rejected"))
    monkeypatch.setattr(jobs_mod, "check_posting", _removed)

    asyncio.run(jobs_mod.dismiss_skipped_if_posting_removed("u1", "job1"))

    assert job.data["user_decision"] == "dismissed"
    assert job.data["posting_removed_at"]
    assert len(events) == 1
    assert events[0]["decision"] == "dismissed"
    assert events[0]["previous_decision"] == "rejected"
    assert events[0]["actor"] == "system"
    assert events[0]["score_snapshot"]["overall_score"] == 78.5
    assert user.decisions.events is events


def test_a_still_live_posting_writes_no_event(world, events, monkeypatch):
    """First early return: nothing was dismissed, so no decision changed."""
    _, job, _ = world(_job(match=dict(MATCH), user_decision="rejected"))
    monkeypatch.setattr(jobs_mod, "check_posting", _live)

    asyncio.run(jobs_mod.dismiss_skipped_if_posting_removed("u1", "job1"))

    assert job.data["user_decision"] == "rejected"
    assert events == []


def test_a_job_redecided_during_the_probe_writes_no_event(world, events, monkeypatch):
    """Second early return: the user restored or approved the job while the
    probe ran, so the dismissal is abandoned — and an event written before
    that re-check would record a decision that never happened."""
    _, job, _ = world(_job(match=dict(MATCH), user_decision="rejected"))

    async def restore_then_removed(j, **kwargs):
        job.data["user_decision"] = "approved"  # the user, mid-probe
        return "removed"

    monkeypatch.setattr(jobs_mod, "check_posting", restore_then_removed)

    asyncio.run(jobs_mod.dismiss_skipped_if_posting_removed("u1", "job1"))

    assert job.data["user_decision"] == "approved"
    assert events == []


def test_a_missing_job_document_writes_no_event(world, events, monkeypatch):
    _, _, _ = world(None)
    monkeypatch.setattr(jobs_mod, "check_posting", _removed)

    asyncio.run(jobs_mod.dismiss_skipped_if_posting_removed("u1", "job1"))

    assert events == []


# --- the event write must never fail the request ----------------------------


def test_a_failing_event_write_does_not_fail_the_decision(world, events):
    """A lost label is an inconvenience; a 500 on a decision is a broken
    product. Status, body and the ``user_decision`` write all stay as they
    were before this task."""
    client, job, _ = world(_job(match=dict(MATCH)), fail=True)

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert job.data["user_decision"] == "approved"
    assert events == []


def test_a_failing_event_write_does_not_fail_the_system_dismissal(
    world, events, monkeypatch
):
    _, job, _ = world(_job(user_decision="rejected"), fail=True)
    monkeypatch.setattr(jobs_mod, "check_posting", _removed)

    asyncio.run(jobs_mod.dismiss_skipped_if_posting_removed("u1", "job1"))

    assert job.data["user_decision"] == "dismissed"
    assert events == []


def test_a_failing_pre_read_does_not_fail_the_decision(world, events):
    """The pre-read exists *only* to write a label, and before this task the
    route held no read at all. A ``get`` that raises — the shape of a read
    retry budget running out — must not 500 the decision: that would leave the
    job un-approved with nothing dispatched, over a logging line. A label with
    ``previous_decision: None`` is the documented inconvenience."""
    client, job, _ = world(_job(match=dict(MATCH)), fail_get=True)

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert job.data["user_decision"] == "approved"
    # Degraded, not absent: the decision itself is still a label.
    assert len(events) == 1
    assert events[0]["decision"] == "approved"
    assert events[0]["previous_decision"] is None
    assert events[0]["score_snapshot"] is None


# --- no behaviour change ----------------------------------------------------


def test_approving_still_creates_the_application_and_dispatches(world, monkeypatch):
    """The event log is additive. Approval's side effects are what the funnel
    depends on, so they are pinned alongside it."""
    dispatched: list[tuple] = []
    monkeypatch.setattr(
        jobs_mod,
        "dispatch_tailor",
        lambda uid, jid, background_tasks=None: dispatched.append((uid, jid)) or True,
    )
    client, _, user = world(_job(match=dict(MATCH)))

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert dispatched == [("u1", "job1")]
    app = user.collection("applications").document("app-job1")
    assert app.data["job_id"] == "job1"


def test_reverting_still_discards_an_unsubmitted_application(world):
    client, _, user = world(_job(match=dict(MATCH), user_decision="approved"))
    app = user.collection("applications").document("app-job1")
    app.set({"id": "app-job1", "job_id": "job1", "status": "queued"})

    resp = client.post("/jobs/job1/decide", json={"decision": "pending"})

    assert resp.status_code == 200
    assert app.deleted is True
