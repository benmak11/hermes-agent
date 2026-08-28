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

from pathlib import Path

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
from tools.applications import state
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


def test_in_progress_leases_are_bounded_by_the_dispatch_deadline():
    assert set(state.IN_PROGRESS) == {"queued", "tailoring", "submitting"}
    for status, seconds in state.IN_PROGRESS.items():
        assert status in ALL_STATUSES
        assert 0 < seconds <= _DISPATCH_DEADLINE_SECONDS


def test_lease_for_only_covers_in_progress_statuses():
    from datetime import UTC, datetime

    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    lease = state.lease_for("submitting", now=now)
    assert lease["status"] == "submitting"
    assert lease["acquired_at"] == "2026-08-26T12:00:00+00:00"
    assert lease["expires_at"] == "2026-08-26T12:20:00+00:00"
    for status in ALL_STATUSES - set(state.IN_PROGRESS):
        assert state.lease_for(status, now=now) is None


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

    submissions: list[tuple[str, str]] = []

    async def fake_run_submission(user_id, app_id):
        submissions.append((user_id, app_id))

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
    assert submissions == [("u1", "app-job1")]
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
    assert submissions == [("u1", "app-job1")]


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


def test_submit_404s_on_a_missing_application(submit_client):
    client, doc, submissions = submit_client
    doc.delete()
    assert client.post("/applications/app-job1/submit").status_code == 404
    assert submissions == []


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
