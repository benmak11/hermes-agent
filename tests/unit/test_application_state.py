# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The application lifecycle state machine and its single compare-and-swap.

The headline case is ``test_double_click_submits_once``: two Submit clicks
racing on two API instances, where the second holds a stale read. Before
``tools.applications.state`` both passed the ``SUBMITTABLE`` check and both
scheduled a live ATS submission — a duplicate real job application. Everything
else here pins the properties that make that fix hold: the table is the only
authority on legality, an unrecognised status has no outgoing edges, writes are
``update`` (so the undo path's delete stands), the timeline is appended to
rather than replaced, and no route writes a status field behind the helper's
back.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.transforms import ArrayUnion

import api.routes.applications as applications
import api.routes.jobs as jobs
import api.routes.worker as worker
import tools.ats.sweep as sweep
from api.deps import verify_user
from models.application import ApplicationStatus
from tools.applications import reaper, state
from tools.queues import _DISPATCH_DEADLINE_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_STATUSES = set(ApplicationStatus.__args__)


# --------------------------------------------------------------------------
# Fakes. Modelled on tests/unit/test_batch_runs.py's _FakeRunRef, but this
# suite is *about* the precondition, so the fake honours update_time for real:
# every write bumps a version and a write carrying a stale one raises
# FailedPrecondition exactly as Firestore does.
# --------------------------------------------------------------------------


def _apply(target: dict, fields: dict) -> None:
    """Resolve the sentinels Firestore would resolve server-side."""
    for key, value in fields.items():
        if value is firestore.DELETE_FIELD:
            target.pop(key, None)
        elif isinstance(value, ArrayUnion):
            target[key] = list(target.get(key) or []) + list(value.values)
        else:
            target[key] = value


class _FakeSnap:
    def __init__(self, doc_id, data, update_time):
        self.id = doc_id
        self.update_time = update_time
        self.exists = data is not None
        self._data = None if data is None else dict(data)

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDoc:
    def __init__(self, data=None, doc_id="app-job1"):
        self.id = doc_id
        self._data = None if data is None else dict(data)
        self._version = 1
        self.updates: list[tuple[dict, object]] = []
        self.sets: list[tuple[dict, bool]] = []
        self._pinned: list[_FakeSnap] = []

    # -- test helpers -------------------------------------------------
    def snapshot(self) -> _FakeSnap:
        """A snapshot as of *now* — hold one to simulate a concurrent reader."""
        return _FakeSnap(self.id, self._data, self._version)

    def pin(self, snap: _FakeSnap) -> None:
        """Make the next get() hand back ``snap`` (a stale read in flight)."""
        self._pinned.append(snap)

    @property
    def data(self) -> dict | None:
        return None if self._data is None else dict(self._data)

    # -- DocumentReference surface ------------------------------------
    def get(self):
        if self._pinned:
            return self._pinned.pop(0)
        return self.snapshot()

    def set(self, data, merge=False):
        self.sets.append((data, merge))
        if merge and self._data is not None:
            _apply(self._data, data)
        else:
            self._data = {}
            _apply(self._data, data)
        self._version += 1

    def update(self, fields, option=None):
        self.updates.append((fields, option))
        if self._data is None:
            raise NotFound("no such document")
        if option is not None and option._last_update_time != self._version:
            raise FailedPrecondition("stale last_update_time")
        _apply(self._data, fields)
        self._version += 1

    def delete(self):
        self._data = None
        self._version += 1


class _FakeCollection:
    def __init__(self, doc: _FakeDoc):
        self._doc = doc

    def document(self, doc_id):
        return self._doc


def _ready(**extra) -> _FakeDoc:
    return _FakeDoc(
        {
            "id": "app-job1",
            "user_id": "u1",
            "job_id": "job1",
            "status": "ready_for_review",
            "timeline": [{"at": "2026-08-01T00:00:00+00:00", "status": "tailoring"}],
            **extra,
        }
    )


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def test_table_covers_exactly_the_application_statuses():
    """Every ApplicationStatus has a row; no row invents a status."""
    assert set(state.TRANSITIONS) == ALL_STATUSES
    for status, outgoing in state.TRANSITIONS.items():
        assert outgoing <= ALL_STATUSES, status
        assert status not in outgoing, f"{status} may not transition to itself"


def test_terminal_statuses_have_no_outgoing_edges():
    assert state.TERMINAL_STATUSES == {"responded", "posting_removed"}
    for status in state.TERMINAL_STATUSES:
        assert state.TRANSITIONS[status] == frozenset()
        for target in ALL_STATUSES:
            assert not state.can_transition(status, target)


def test_every_status_except_the_initial_one_is_reachable():
    """No orphan rows — a status nothing can enter is dead weight in the UI."""
    reachable = {state.INITIAL}
    for outgoing in state.TRANSITIONS.values():
        reachable |= outgoing
    assert reachable == ALL_STATUSES


@pytest.mark.parametrize("unknown", ["legacy_status", "TAILORING", "", None])
def test_an_unknown_status_has_no_outgoing_edges(unknown):
    """The backward-compatibility story: no migration, no normalize().

    A document holding a status this build doesn't know about is inert — the
    UI still renders it, but nothing can act on it.
    """
    for target in ALL_STATUSES:
        assert not state.can_transition(unknown, target)


def test_an_unknown_status_is_never_written_over():
    doc = _FakeDoc({"status": "legacy_status", "timeline": []})
    assert state.try_transition(doc, doc.get(), "submitting") is False
    assert doc.data["status"] == "legacy_status"
    assert doc.updates == []


def test_submittable_is_derived_from_the_table():
    """applications.SUBMITTABLE documents the table; it must not drift from it."""
    assert applications.SUBMITTABLE == {"ready_for_review", "failed"}
    assert applications.SUBMITTABLE == {
        s for s, nxt in state.TRANSITIONS.items() if "submitting" in nxt
    }


def test_every_pre_submission_status_can_be_invalidated_by_the_sweep():
    """A posting dying is an external fact, not a step in the flow — every
    non-terminal status must be able to reach posting_removed, and the sweep's
    own allowlist (which spares an in-flight submit) must stay inside that."""
    assert sweep.ACTIVE_APP_STATUSES == {
        "queued",
        "tailoring",
        "ready_for_review",
        "failed",
    }
    for status in sweep.ACTIVE_APP_STATUSES:
        assert state.can_transition(status, "posting_removed"), status
    assert "submitting" not in sweep.ACTIVE_APP_STATUSES
    # ...but the submission path itself may still record it.
    assert state.can_transition("submitting", "posting_removed")


def test_creation_fields_start_queued():
    fields = state.creation_fields()
    assert fields["status"] == state.INITIAL == "queued"
    assert [e["status"] for e in fields["timeline"]] == ["queued"]
    assert "note" not in fields["timeline"][0]
    assert state.INITIAL in ALL_STATUSES


# --------------------------------------------------------------------------
# Leases (shape only — nothing reaps them yet)
# --------------------------------------------------------------------------


def test_every_lease_outlives_the_work_it_guards():
    """**The inequality that makes a lease a lock.** This assertion used to read
    ``<= _DISPATCH_DEADLINE_SECONDS`` and pinned the bug: a 1200s lease against
    1800s of allowed work. Worker A is killed at T+1800 with the browser
    possibly already past the Submit click; the retry arrives at T+1860, finds
    the lease expired, claims it, and files the application a second time. A
    lock whose TTL is shorter than the work is worse than none, because it
    reads as permission."""
    assert set(state.IN_PROGRESS) == {"queued", "tailoring", "submitting"}
    for status, seconds in state.IN_PROGRESS.items():
        assert status in ALL_STATUSES
        assert seconds > _DISPATCH_DEADLINE_SECONDS, status


def test_lease_for_only_covers_in_progress_statuses():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    lease = state.lease_for("submitting", now=now)
    assert lease["status"] == "submitting"
    assert lease["acquired_at"] == "2026-08-26T12:00:00+00:00"
    assert lease["expires_at"] == "2026-08-26T12:31:00+00:00"
    for status in ALL_STATUSES - set(state.IN_PROGRESS):
        assert state.lease_for(status, now=now) is None


def test_a_lease_names_its_owner_but_reads_back_without_one():
    """Owner-less leases exist on documents written before the field did, and
    must still read as valid claims rather than as free documents."""
    assert "owner" not in (state.lease_for("submitting") or {})
    assert state.lease_for("submitting", owner="abc")["owner"] == "abc"
    assert state.lease_owner(
        {"lease": {"expires_at": "2030-01-01T00:00:00+00:00"}}
    ) is (None)
    assert state.lease_is_held({"lease": {"expires_at": "2030-01-01T00:00:00+00:00"}})
    assert state.new_owner() != state.new_owner()


def test_a_lease_is_written_and_cleared_atomically_with_the_status():
    doc = _ready()
    lease = state.lease_for("submitting")
    assert state.try_transition(doc, doc.get(), "submitting", lease=lease) is True
    assert doc.data["lease"] == lease

    assert (
        state.try_transition(doc, doc.get(), "submitted", lease=state.CLEAR_LEASE)
        is True
    )
    assert doc.data is not None and "lease" not in doc.data
    assert doc.data["status"] == "submitted"


def test_no_lease_field_appears_when_none_is_passed():
    """PR A ships the lease shape but must not add a field to live documents."""
    doc = _ready()
    assert state.try_transition(doc, doc.get(), "submitting") is True
    assert doc.data is not None and "lease" not in doc.data


# --------------------------------------------------------------------------
# try_claim_lease: the worker's delivery claim
# --------------------------------------------------------------------------


def _submitting(**extra) -> _FakeDoc:
    doc = _ready()
    state.try_transition(doc, doc.get(), "submitting")
    if extra:
        doc.update(extra)
    return doc


def test_the_lease_claim_is_a_second_compare_and_swap():
    """The status can't be the worker's claim — the API already took it, and
    submitting → submitting is illegal. The lease answers the other question:
    is a process running this *right now*."""
    doc = _submitting()
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is True
    assert doc.data["lease"]["status"] == "submitting"
    assert doc.data["status"] == "submitting"  # the claim changed no status

    # A redelivered Cloud Task, arriving while the first one runs.
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="B") is False


def test_a_lease_claim_requires_the_expected_status():
    doc = _ready()
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is False
    assert doc.updates == []
    assert doc.data is not None and "lease" not in doc.data


def test_a_released_lease_is_not_a_second_chance_to_submit():
    """The terminal write clears the lease. Nothing may read that as free."""
    doc = _submitting()
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is True
    state.try_transition(doc, doc.get(), "submitted", lease=state.CLEAR_LEASE)
    assert doc.data is not None and "lease" not in doc.data
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="B") is False


def test_an_expired_lease_may_be_reclaimed():
    """A worker that died holds nothing forever — the IN_PROGRESS clock is what
    bounds it, and what the reaper will read."""
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    doc = _submitting(lease=state.lease_for("submitting", now=now))
    later = now + timedelta(seconds=state.IN_PROGRESS["submitting"] + 1)

    assert (
        state.try_claim_lease(doc, doc.get(), "submitting", owner="B", now=now) is False
    )
    assert (
        state.try_claim_lease(doc, doc.get(), "submitting", owner="B", now=later)
        is True
    )
    assert doc.data["lease"]["acquired_at"] == later.isoformat()


@pytest.mark.parametrize(
    "lease", [{"status": "submitting"}, {"expires_at": "whenever"}, {}]
)
def test_a_lease_that_cannot_be_read_counts_as_held(lease):
    """Asymmetric on purpose: refusing wedges a document, which is undoable.
    Claiming anyway risks a duplicate real application, which is not."""
    doc = _submitting(lease=lease)
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is False


def test_a_lease_claim_re_reads_before_it_retries():
    """The genuine race: two deliveries, the second holding a read taken before
    the first claimed. The precondition fails, and the retry sees the winner's
    lease rather than overwriting it."""
    doc = _submitting()
    in_flight = doc.snapshot()
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is True
    won = doc.data["lease"]

    assert state.try_claim_lease(doc, in_flight, "submitting", owner="B") is False
    assert doc.data["lease"] == won  # the winner keeps it, owner and all


def test_a_spurious_precondition_failure_still_claims():
    """_backfill_job_url writes on read, same as for try_transition."""
    doc = _submitting()
    stale = doc.snapshot()
    doc.update({"job_url": "https://boards.greenhouse.io/acme/jobs/1"})
    assert state.try_claim_lease(doc, stale, "submitting", owner="A") is True


def test_a_lease_claim_does_not_resurrect_a_deleted_document():
    doc = _submitting()
    snap = doc.snapshot()
    doc.delete()
    assert state.try_claim_lease(doc, snap, "submitting", owner="A") is False
    assert doc.data is None and doc.sets == []


def test_a_lease_is_released_only_by_the_run_that_holds_it():
    """Without an owner check, an expiry lets two runs believe they hold the
    same document: B claims after A's lease lapses, then A — alive, merely slow
    — finishes and frees the document for a *third* claim while B is working."""
    doc = _submitting()
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is True
    held = doc.data["lease"]

    assert state.release_lease(doc, doc.get(), "B") is False  # A's lease, not B's
    assert doc.data["lease"] == held

    assert state.release_lease(doc, doc.get(), "A") is True
    assert doc.data is not None and "lease" not in doc.data
    # Nothing to release twice.
    assert state.release_lease(doc, doc.get(), "A") is False


def test_a_lease_with_no_owner_is_never_released_by_ownership():
    """Backward compatibility, biased safe: a lease we cannot prove is ours may
    belong to a live run, and waiting out its TTL costs only time."""
    doc = _submitting(lease=state.lease_for("submitting"))
    assert state.release_lease(doc, doc.get(), "A") is False
    assert doc.data["lease"] is not None


def test_release_does_not_resurrect_a_deleted_document():
    doc = _submitting()
    state.try_claim_lease(doc, doc.get(), "submitting", owner="A")
    snap = doc.snapshot()
    doc.delete()
    assert state.release_lease(doc, snap, "A") is False
    assert doc.data is None and doc.sets == []


def test_claiming_a_status_that_carries_no_lease_is_a_programming_error():
    doc = _ready()
    with pytest.raises(ValueError, match="ready_for_review"):
        state.try_claim_lease(doc, doc.get(), "ready_for_review", owner="A")


# --------------------------------------------------------------------------
# try_transition semantics
# --------------------------------------------------------------------------


def test_illegal_transition_returns_false_without_writing():
    doc = _FakeDoc({"status": "submitted", "timeline": []})
    assert state.try_transition(doc, doc.get(), "submitting") is False
    assert doc.updates == []
    assert doc.data["status"] == "submitted"


def test_the_timeline_is_appended_to_never_replaced():
    doc = _ready()
    before = doc.data["timeline"]
    assert state.try_transition(doc, doc.get(), "submitting") is True
    assert state.try_transition(doc, doc.get(), "failed", note="boom") is True
    timeline = doc.data["timeline"]
    assert timeline[: len(before)] == before
    assert [e["status"] for e in timeline] == ["tailoring", "submitting", "failed"]
    assert timeline[-1]["note"] == "boom"
    # The pre-existing entry shape is preserved exactly: no note key when there
    # is no note, so web/ renders unchanged.
    assert set(timeline[1]) == {"at", "status"}
    assert set(timeline[2]) == {"at", "status", "note"}


def test_try_transition_uses_update_not_set():
    """A document the undo path deleted must never be resurrected."""
    doc = _ready()
    snap = doc.snapshot()  # a read taken before decide("pending") deleted it
    doc.delete()
    assert state.try_transition(doc, snap, "submitting") is False
    assert doc.data is None
    assert doc.sets == []  # nothing recreated it


def test_a_deleted_document_reads_as_missing():
    doc = _FakeDoc(None)
    assert state.try_transition(doc, doc.get(), "submitting") is False
    assert doc.updates == []


def test_extra_fields_land_atomically_with_the_status():
    doc = _ready()
    assert (
        state.try_transition(
            doc,
            doc.get(),
            "submitting",
            extra={"last_submitted_at": "2026-08-26T00:00:00+00:00"},
        )
        is True
    )
    assert doc.data["status"] == "submitting"
    assert doc.data["last_submitted_at"] == "2026-08-26T00:00:00+00:00"
    assert len(doc.updates) == 1  # one write, not two


@pytest.mark.parametrize("field", ["status", "timeline", "lease"])
def test_extra_may_not_smuggle_in_an_owned_field(field):
    doc = _ready()
    with pytest.raises(ValueError, match=field):
        state.try_transition(doc, doc.get(), "submitting", extra={field: "whatever"})


def test_a_spurious_precondition_failure_is_retried_once_and_succeeds():
    """_backfill_job_url writes on read, so a concurrent GET /applications
    bumps update_time without changing the status. One retry absorbs it."""
    doc = _ready()
    stale = doc.snapshot()
    doc.update({"job_url": "https://boards.greenhouse.io/acme/jobs/1"})  # the backfill

    assert state.try_transition(doc, stale, "submitting") is True
    assert len(doc.updates) == 3  # backfill, the losing attempt, the retry
    assert doc.data["status"] == "submitting"
    # Retrying must not double-append.
    assert [e["status"] for e in doc.data["timeline"]] == ["tailoring", "submitting"]


def test_it_retries_only_once():
    doc = _ready()
    contended = []

    def always_stale(fields, option=None):
        contended.append(fields)
        raise FailedPrecondition("someone is always faster")

    doc.update = always_stale
    assert state.try_transition(doc, doc.get(), "submitting") is False
    assert len(contended) == 2  # the attempt and exactly one retry


def test_the_retry_re_checks_legality_against_the_new_status():
    """The genuine race loses on the table, not by overwriting the winner."""
    doc = _ready()
    stale = doc.snapshot()
    state.try_transition(doc, doc.get(), "submitting")  # the other click wins

    assert state.try_transition(doc, stale, "submitting") is False
    assert [e["status"] for e in doc.data["timeline"]] == ["tailoring", "submitting"]


def test_allowed_from_narrows_the_table_for_one_call():
    doc = _ready()
    assert (
        state.try_transition(doc, doc.get(), "submitting", allowed_from={"failed"})
        is False
    )
    assert doc.data["status"] == "ready_for_review"
    assert doc.updates == []
    # ...and the same call without it goes through, so the table alone allows it.
    assert state.try_transition(doc, doc.get(), "submitting") is True


def test_allowed_from_is_re_checked_on_the_retry_read():
    """The blocker. A caller whose precondition is narrower than the table must
    have it enforced *inside* the swap, or contention defeats it.

    The sweep reads ready_for_review and passes its allowlist. The user clicks
    Submit — status becomes submitting, a live ATS submission starts. The
    sweep's write loses the precondition and retries. submitting →
    posting_removed is a legal edge, so a table-only re-check would let it
    through and mark the posting removed mid-submission.
    """
    doc = _ready()
    sweep_read = doc.snapshot()
    assert state.try_transition(doc, doc.get(), "submitting") is True  # the click

    assert (
        state.try_transition(
            doc,
            sweep_read,
            "posting_removed",
            allowed_from={"queued", "tailoring", "ready_for_review", "failed"},
        )
        is False
    )
    assert doc.data["status"] == "submitting"
    assert [e["status"] for e in doc.data["timeline"]] == ["tailoring", "submitting"]


def test_without_allowed_from_the_same_interleaving_goes_through():
    """Pins why the parameter is needed rather than merely nice: the retry's
    table-only re-check finds submitting → posting_removed perfectly legal."""
    doc = _ready()
    sweep_read = doc.snapshot()
    state.try_transition(doc, doc.get(), "submitting")

    assert state.try_transition(doc, sweep_read, "posting_removed") is True
    assert doc.data["status"] == "posting_removed"


def test_allowed_from_still_permits_the_uncontended_case():
    doc = _ready()
    assert (
        state.try_transition(
            doc,
            doc.get(),
            "posting_removed",
            allowed_from={"queued", "tailoring", "ready_for_review", "failed"},
        )
        is True
    )
    assert doc.data["status"] == "posting_removed"


def test_append_note_leaves_the_status_alone():
    doc = _ready()
    assert state.append_note(doc, "submitting", "Attaching resume") is True
    assert doc.data["status"] == "ready_for_review"
    assert doc.data["timeline"][-1] == {
        "at": doc.data["timeline"][-1]["at"],
        "status": "submitting",
        "note": "Attaching resume",
    }


def test_append_note_does_not_resurrect_a_deleted_document():
    doc = _FakeDoc(None)
    assert state.append_note(doc, "submitting", "Opening page") is False
    assert doc.data is None
    assert doc.sets == []


# --------------------------------------------------------------------------
# The double-click race, through the real route
# --------------------------------------------------------------------------


@pytest.fixture
def submit_client(monkeypatch):
    """The real submit() route over a fake collection, with run_submission
    replaced by a recorder so 'did we submit twice?' is directly observable."""
    doc = _ready()
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))

    submissions: list[tuple] = []

    async def fake_run_submission(user_id, app_id, *, dry_run=False):
        submissions.append((user_id, app_id, dry_run))

    monkeypatch.setattr(applications, "run_submission", fake_run_submission)

    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), doc, submissions


def test_double_click_submits_once(submit_client):
    """Two clicks, the second holding a read taken before the first landed.

    Exactly one wins, the loser gets 409, and exactly one live submission is
    scheduled. This is the bug the whole module exists for.
    """
    client, doc, submissions = submit_client
    in_flight = doc.snapshot()  # the second request's read, taken concurrently

    first = client.post("/applications/app-job1/submit")
    assert first.status_code == 200

    doc.pin(in_flight)  # the second request reads what it read before
    second = client.post("/applications/app-job1/submit")
    assert second.status_code == 409
    assert "ready_for_review" in second.json()["detail"]

    assert doc.data["status"] == "submitting"
    assert submissions == [("u1", "app-job1", False)]
    assert [e["status"] for e in doc.data["timeline"]] == ["tailoring", "submitting"]


def test_submit_is_rejected_from_a_terminal_status(submit_client):
    client, doc, submissions = submit_client
    doc.set({**doc.data, "status": "submitted"})
    resp = client.post("/applications/app-job1/submit")
    assert resp.status_code == 409
    assert submissions == []


def test_submit_from_failed_is_a_retry(submit_client):
    client, doc, submissions = submit_client
    doc.set({**doc.data, "status": "failed"})
    assert client.post("/applications/app-job1/submit").status_code == 200
    assert doc.data["status"] == "submitting"
    assert doc.data["last_submitted_at"]
    assert submissions == [("u1", "app-job1", False)]


def test_regenerate_requeues_and_rejects_terminal_statuses(monkeypatch):
    doc = _ready()
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))
    scheduled: list[tuple[str, str]] = []

    async def fake_run_tailoring(user_id, job_id):
        scheduled.append((user_id, job_id))

    monkeypatch.setattr(applications, "run_tailoring", fake_run_tailoring)
    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    client = TestClient(app)

    assert client.post("/applications/app-job1/regenerate").status_code == 200
    assert doc.data["status"] == "queued"
    assert doc.data["timeline"][-1]["note"] == "regenerate"

    # Already queued: re-scheduled, not refused — the manual way out of a
    # background task that never fired.
    assert client.post("/applications/app-job1/regenerate").status_code == 200
    assert doc.data["status"] == "queued"

    doc.set({**doc.data, "status": "submitted"})
    resp = client.post("/applications/app-job1/regenerate")
    assert resp.status_code == 409
    assert "submitted" in resp.json()["detail"]
    assert scheduled == [("u1", "job1"), ("u1", "job1")]


def test_the_public_submit_route_cannot_ask_for_a_dry_run(submit_client):
    """``dry_run`` is a worker-only switch (``worker.ApplyTask``): it drives the
    real submission path but stops short of the Submit button, so a user able
    to set it could tell the product they applied when nobody did. The route
    takes no body at all, and run_submission's parameter is keyword-only, so a
    body that asks for one is simply not read."""
    client, _doc, submissions = submit_client
    resp = client.post("/applications/app-job1/submit", json={"dry_run": True})
    assert resp.status_code == 200
    assert submissions == [("u1", "app-job1", False)]


def test_submit_404s_on_a_missing_application(submit_client):
    client, doc, submissions = submit_client
    doc.delete()
    assert client.post("/applications/app-job1/submit").status_code == 404
    assert submissions == []


# --------------------------------------------------------------------------
# Claim first, dispatch second
#
# Once the submission goes to a queue, the claim and the dispatch are two
# separate writes to two separate systems, and the order between them is a
# correctness property rather than a style preference.
# --------------------------------------------------------------------------


@pytest.fixture
def queued_submit(monkeypatch):
    """The real submit() route with QUEUE_MODE on and the enqueue recorded.

    The fake enqueue **reads the document from inside the call**, so every
    recorded task carries what a worker picking it up at that instant would
    see. That snapshot is the ordering assertion.
    """
    monkeypatch.setenv("QUEUE_MODE", "1")
    doc = _ready()
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))

    enqueued: list[dict] = []
    # ``hook`` runs where Cloud Tasks would be, so a test can decide what the
    # rest of the world does while this request is inside the enqueue call.
    outcome = SimpleNamespace(accepted=True, error=None, hook=None)

    def fake_enqueue(queue, path, payload, *, task_id=None):
        enqueued.append(
            {
                "queue": queue,
                "path": path,
                "payload": payload,
                "task_id": task_id,
                "doc_at_enqueue": doc.data,
            }
        )
        if outcome.hook is not None:
            outcome.hook()
        if outcome.error is not None:
            raise outcome.error
        return outcome.accepted

    monkeypatch.setattr(applications.queues, "enqueue", fake_enqueue)

    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(
        client=TestClient(app), doc=doc, enqueued=enqueued, outcome=outcome
    )


def test_submit_commits_the_claim_before_it_enqueues(queued_submit):
    """The enqueue is the *last* thing the request does.

    Cloud Tasks can hand the task to a worker before this request executes its
    next line. Enqueue first and that worker reads a ``ready_for_review``
    application, finds no claim to inherit, and returns having done nothing —
    the submission lost in silence. In this order the worst case is a claim with
    nothing behind it, which the reaper can undo and which never clicked
    anything.
    """
    resp = queued_submit.client.post("/applications/app-job1/submit")

    assert resp.status_code == 200
    (task,) = queued_submit.enqueued
    assert (task["queue"], task["path"]) == ("apply", "/tasks/apply")
    assert task["payload"] == {"user_id": "u1", "app_id": "app-job1"}
    assert task["doc_at_enqueue"]["status"] == "submitting"
    assert task["doc_at_enqueue"]["submit_attempts"] == 1
    assert task["task_id"] == "apply-u1-app-job1-1"


def test_the_attempt_counter_rides_in_the_same_write_as_the_claim(queued_submit):
    """It names the task, so a claim that loses must not advance it: two claims
    sharing a number would share a task name, and the queue would dedupe the
    second real submission away."""
    doc = queued_submit.doc
    in_flight = doc.snapshot()  # the second click's read, taken concurrently

    assert queued_submit.client.post("/applications/app-job1/submit").status_code == 200

    doc.pin(in_flight)
    assert queued_submit.client.post("/applications/app-job1/submit").status_code == 409

    claim, _option = doc.updates[0]
    assert claim["status"] == "submitting" and claim["submit_attempts"] == 1
    assert doc.data["submit_attempts"] == 1  # the loser advanced nothing
    assert len(queued_submit.enqueued) == 1


def test_a_retry_after_a_failure_gets_a_task_name_of_its_own(queued_submit):
    """Which is what no time granularity can offer: an hour-grained name would
    block a legitimate retry for an hour, and a minute-grained one would still
    swallow a retry issued in the same minute as the submission that failed."""
    doc = queued_submit.doc
    queued_submit.client.post("/applications/app-job1/submit")
    state.try_transition(doc, doc.get(), "failed", note="the ATS said no")

    assert queued_submit.client.post("/applications/app-job1/submit").status_code == 200

    assert [t["task_id"] for t in queued_submit.enqueued] == [
        "apply-u1-app-job1-1",
        "apply-u1-app-job1-2",
    ]
    assert doc.data["submit_attempts"] == 2


def test_a_submission_that_cannot_be_dispatched_gives_the_claim_back(queued_submit):
    """The enqueue is the one step that can fail *after* the claim has landed,
    and a transient Cloud Tasks error is an ordinary event.

    ``submitting`` is the one status a user cannot leave — Submit and Regenerate
    both 409 out of it and the undo path refuses to delete a document in it — so
    a claim with nothing behind it wedges a real application until an operator
    runs ``cli/unwedge_submitting``. Nothing was clicked, so the claim is rolled
    back to ``failed``, which is both true and actionable.
    """
    queued_submit.outcome.error = RuntimeError("Cloud Tasks is unreachable")

    resp = queued_submit.client.post("/applications/app-job1/submit")

    assert resp.status_code == 503
    doc = queued_submit.doc
    assert doc.data["status"] == "failed"
    assert doc.data["timeline"][-1]["note"] == applications.DISPATCH_FAILED_NOTE
    assert "lease" not in doc.data  # the rollback's own claim is handed back
    assert "submitted" not in [e["status"] for e in doc.data["timeline"]]


def test_the_user_can_submit_again_after_a_failed_dispatch(queued_submit):
    """Which is the whole point of the rollback — and the retry also gets a
    *new* task name, so the collision that is one way to fail a dispatch cannot
    repeat itself on the retry."""
    queued_submit.outcome.error = RuntimeError("Cloud Tasks is unreachable")
    assert queued_submit.client.post("/applications/app-job1/submit").status_code == 503

    queued_submit.outcome.error = None
    assert queued_submit.client.post("/applications/app-job1/submit").status_code == 200

    assert queued_submit.doc.data["status"] == "submitting"
    assert [t["task_id"] for t in queued_submit.enqueued] == [
        "apply-u1-app-job1-1",
        "apply-u1-app-job1-2",
    ]


def test_a_deduped_apply_dispatch_gives_the_claim_back_too(queued_submit):
    """A double-click cannot reach here — it lost the swap and got a 409 — so a
    refused task name means one was *reused*. The reachable way: revert deletes
    the application, re-approving recreates it at the same deterministic id with
    the counter gone, and the next submit rebuilds a name whose Cloud Tasks
    tombstone is still alive. Same wedge, same answer."""
    queued_submit.outcome.accepted = False

    resp = queued_submit.client.post("/applications/app-job1/submit")

    assert resp.status_code == 503
    assert queued_submit.doc.data["status"] == "failed"
    assert "lease" not in queued_submit.doc.data


def test_the_rollback_will_not_touch_a_document_someone_is_running(queued_submit):
    """**The rollback is itself a claim, and that is not a formality.**

    An enqueue can report failure and still have created the task (a deadline
    that expires after the server committed), and ``AlreadyExists`` can name a
    task that is still pending. Either way a worker may already be driving a
    browser at this document — and writing ``failed`` with CLEAR_LEASE
    underneath it would clear a live run's claim and throw away the confirmation
    evidence for an application that really was sent.
    """
    doc = queued_submit.doc
    live = state.new_owner()

    def the_task_landed_anyway(*args, **kwargs):
        # The worker got the task, claimed the document, and is mid-submit when
        # our own enqueue call finally reports its deadline.
        state.try_claim_lease(doc, doc.get(), "submitting", owner=live)
        raise RuntimeError("DEADLINE_EXCEEDED")

    queued_submit.outcome.hook = the_task_landed_anyway

    resp = queued_submit.client.post("/applications/app-job1/submit")

    assert resp.status_code == 503
    # Left exactly as the live run has it: still claimed, still that run's lease.
    assert doc.data["status"] == "submitting"
    assert doc.data["lease"]["owner"] == live
    assert "failed" not in [e["status"] for e in doc.data["timeline"]]


def test_regenerate_commits_before_it_enqueues(queued_submit):
    """Same ordering at the other end of the funnel: ``run_tailoring`` claims
    the application out of ``queued``, so the task must not be able to arrive
    while the document is still ``ready_for_review``."""
    resp = queued_submit.client.post("/applications/app-job1/regenerate")

    assert resp.status_code == 200
    (task,) = queued_submit.enqueued
    assert (task["queue"], task["path"]) == ("tailor", "/tasks/tailor")
    assert task["payload"] == {"user_id": "u1", "job_id": "job1"}
    assert task["doc_at_enqueue"]["status"] == state.INITIAL


# --------------------------------------------------------------------------
# Enforcement: nothing writes a status behind the helper's back
# --------------------------------------------------------------------------


def test_no_route_module_writes_a_status_field_directly():
    """The state machine is only a guarantee if it is the sole writer.

    Same spirit as test_worker_routes.test_score_task_cannot_turn_off_the_budget:
    a property that a future edit could quietly break, pinned by reading the
    source rather than by exercising a path.

    ``tools.ats.sweep`` is in here because it turned out to be a fourth writer
    of application status — a background job with the same read-then-blind-write
    race as the routes.
    """
    for module in (applications, jobs, worker, sweep):
        source = Path(module.__file__).read_text()
        assert '"status":' not in source, (
            f"{module.__name__} writes a status field directly — every status "
            "write must go through tools.applications.state"
        )


def test_the_state_module_is_the_one_that_writes_status():
    """Positive control: the scan above would pass on an empty file too."""
    source = (REPO_ROOT / "tools" / "applications" / "state.py").read_text()
    assert 'STATUS_FIELD = "status"' in source
    assert "payload[STATUS_FIELD] = to" in source


def test_regenerate_clears_the_reapers_recovery_budget(monkeypatch):
    """A user asking again is the second epoch for ``reaper.reap_attempts``.

    Without it, an application that exhausted the automatic-recovery cap gets
    exactly one manual retry before the reaper starts failing it on sight — while
    the note the reaper wrote says to press this very button. Inside the swap, so
    a regenerate that loses its race resets nothing.
    """
    doc = _ready(**{reaper.ATTEMPTS_FIELD: reaper.MAX_ATTEMPTS})
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))

    async def fake_run_tailoring(user_id, job_id):
        pass

    monkeypatch.setattr(applications, "run_tailoring", fake_run_tailoring)
    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"

    assert TestClient(app).post("/applications/app-job1/regenerate").status_code == 200

    assert doc.data is not None and doc.data["status"] == "queued"
    assert reaper.ATTEMPTS_FIELD not in doc.data
    # One write, carrying both — the swap is what proves the reset was earned.
    requeue = next(f for f, _o in doc.updates if f.get("status") == "queued")
    assert requeue[reaper.ATTEMPTS_FIELD] is firestore.DELETE_FIELD


def test_a_regenerate_that_loses_its_race_resets_nothing(monkeypatch):
    """Positive control for the swap: two clicks, the second holding a stale
    read. Only the winner's reset lands, and the loser cannot hand a document
    someone else now owns a fresh recovery budget."""
    doc = _ready(**{reaper.ATTEMPTS_FIELD: 2})
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))
    in_flight = doc.snapshot()

    state.try_transition(doc, doc.get(), "submitting")  # the user clicked Submit

    doc.pin(in_flight)
    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"

    assert TestClient(app).post("/applications/app-job1/regenerate").status_code == 409
    assert doc.data["status"] == "submitting"
    assert doc.data[reaper.ATTEMPTS_FIELD] == 2
