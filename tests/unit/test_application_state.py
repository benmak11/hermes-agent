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

import inspect
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
from models.application import Application, ApplicationStatus, Confirmation
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
    """The lease shape must not add a field to documents that pass no lease."""
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


def _extra_via_try_transition(field):
    doc = _ready()
    state.try_transition(doc, doc.get(), "submitting", extra={field: "whatever"})


def _extra_via_try_claim_lease(field):
    doc = _submitting()
    state.try_claim_lease(
        doc, doc.get(), "submitting", owner="A", extra={field: "whatever"}
    )


def _extra_via_append_note(field):
    doc = _ready()
    state.append_note(doc, "submitting", "Attaching resume", extra={field: "whatever"})


#: Every function in ``state`` that takes an ``extra=`` payload. All three ride
#: the same guard, so all three are pinned by the same test: a writer added
#: later with the guard forgotten is the failure mode, and one of these paths —
#: ``append_note`` — has no compare-and-swap and no ``allowed_from`` at all, so
#: its guard is the *only* thing stopping ``extra`` becoming a second, unchecked
#: writer of ``status``.
EXTRA_WRITERS = {
    "try_transition": _extra_via_try_transition,
    "try_claim_lease": _extra_via_try_claim_lease,
    "append_note": _extra_via_append_note,
}


def test_every_extra_taking_writer_is_covered():
    """The list above is the test's scope, so it must not drift from the module."""
    takes_extra = {
        name
        for name, fn in vars(state).items()
        if inspect.isfunction(fn) and "extra" in inspect.signature(fn).parameters
    }
    assert takes_extra == set(EXTRA_WRITERS) | {"_reject_owned", "_payload"}


@pytest.mark.parametrize("writer", sorted(EXTRA_WRITERS))
@pytest.mark.parametrize("field", ["status", "timeline", "lease"])
def test_extra_may_not_smuggle_in_an_owned_field(field, writer):
    with pytest.raises(ValueError, match=field):
        EXTRA_WRITERS[writer](field)


def test_the_owned_field_guard_fires_before_a_claim_is_even_attempted():
    """``try_claim_lease`` validates ``extra`` *before* its loop, deliberately:
    a clash is a programming error, not a race, so it has to raise whether or
    not the attempt ever reaches the network. Here B's claim would be refused
    outright — A holds the lease — so nothing downstream would ever look at the
    payload."""
    doc = _submitting()
    assert state.try_claim_lease(doc, doc.get(), "submitting", owner="A") is True
    with pytest.raises(ValueError, match="status"):
        state.try_claim_lease(
            doc, doc.get(), "submitting", owner="B", extra={"status": "submitted"}
        )


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


def _auto_submit_on(monkeypatch, source: str | None = "greenhouse") -> None:
    """Switch ``AUTO_SUBMIT_ENABLED`` on and give every job ``source``."""
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "1")
    monkeypatch.setattr(
        applications,
        "_job_sources",
        lambda user_id, job_ids: dict.fromkeys(job_ids, source),
    )


@pytest.fixture
def submit_client(monkeypatch):
    """The real submit() route over a fake collection, with run_submission
    replaced by a recorder so 'did we submit twice?' is directly observable.
    Auto-submit is switched on for a greenhouse job, the one case it serves."""
    _auto_submit_on(monkeypatch)
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


def test_a_retry_clears_the_click_marker_but_not_the_uncertainty(submit_client):
    """``submit_attempted_at`` is per *attempt*; ``submission_uncertain`` is per
    *document*, and they must not be cleared together.

    This is the shape ``reaper.release_uncertain`` leaves behind: a ``failed``
    application that may already be with the employer. The user retries. If the
    marker survives that retry, a run that dies before the browser ever reaches
    the button is reported as uncertain all over again — the fork is blunted on
    exactly the documents most likely to be retried. If the *flag* is cleared,
    the durable record that an earlier submission may have landed is gone.
    """
    client, doc, _submissions = submit_client
    doc.set(
        {
            **doc.data,
            "status": "failed",
            reaper.CLICKED_FIELD: "2026-08-01T00:00:00+00:00",
            reaper.UNCERTAIN_FIELD: True,
        }
    )

    assert client.post("/applications/app-job1/submit").status_code == 200

    assert doc.data["status"] == "submitting"
    assert reaper.CLICKED_FIELD not in doc.data
    assert doc.data[reaper.UNCERTAIN_FIELD] is True
    # In the *same* write as the claim. Cleared beside it, a crash in between
    # leaves a fresh ``submitting`` document wearing the previous attempt's
    # marker, which is the state this test exists to make unreachable.
    claim = next(f for f, _o in doc.updates if f.get("status") == "submitting")
    assert claim[reaper.CLICKED_FIELD] is firestore.DELETE_FIELD

    # What it buys: the reaper's fork now reads this run for what it is.
    now = datetime.now(UTC)
    dead = {
        **doc.data,
        "lease": state.lease_for(
            "submitting",
            owner="A",
            now=now - timedelta(seconds=state.IN_PROGRESS["submitting"] * 2),
        ),
    }
    assert reaper.classify(dead, now=now) == "release_unstarted"


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
    _auto_submit_on(monkeypatch)
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


# --------------------------------------------------------------------------
# Manual apply: the user submits, Hermes records it
# --------------------------------------------------------------------------


def test_submitted_is_reachable_from_review_and_failed_and_submitting_only():
    for status in ALL_STATUSES:
        expected = status in {"ready_for_review", "failed", "submitting"}
        assert state.can_transition(status, "submitted") is expected, status
    assert state.MANUAL_SUBMIT_FROM == {"ready_for_review", "failed"}
    assert state.MANUAL_SUBMIT_FROM == {
        s for s, nxt in state.TRANSITIONS.items() if "submitted" in nxt
    } - {"submitting"}


def test_the_manual_edges_change_no_set_derived_from_the_table():
    """Every consumer of TRANSITIONS keys on an edge other than ``→ submitted``,
    so the new edges must leave each of them exactly where it was."""
    assert applications.SUBMITTABLE == {"ready_for_review", "failed"}
    assert applications.OBJECTIVE_EDITABLE == {"ready_for_review", "failed"}
    assert state.TERMINAL_STATUSES == {"responded", "posting_removed"}
    assert sweep.ACTIVE_APP_STATUSES == {
        "queued",
        "tailoring",
        "ready_for_review",
        "failed",
    }
    assert reaper.REAPABLE == ["queued", "tailoring", "submitting"]
    assert set(state.IN_PROGRESS) == {"queued", "tailoring", "submitting"}


def _manually_submitted() -> _FakeDoc:
    return _ready(
        status="submitted",
        confirmation={"submitted_at": "2026-08-02T00:00:00+00:00", "method": "manual"},
    )


def test_the_sweep_cannot_remove_a_manually_submitted_application():
    """The sweep's own swap, with its own allowlist."""
    doc = _manually_submitted()
    assert not state.try_transition(
        doc,
        doc.get(),
        "posting_removed",
        allowed_from=sweep.ACTIVE_APP_STATUSES,
        lease=state.CLEAR_LEASE,
    )
    assert doc.data["status"] == "submitted"
    assert doc.updates == []


def test_the_reaper_leaves_a_manually_submitted_application_alone():
    now = datetime.now(UTC)
    doc = {**_manually_submitted().data, "timeline": []}
    assert reaper.classify(doc, now=now + timedelta(days=30)) == "alive"


def test_the_auto_path_cannot_complete_a_document_nobody_claimed():
    """``run_submission``'s terminal write names ``submitting``, so the new
    ``ready_for_review → submitted`` edge is not a way in for it."""
    doc = _ready()
    assert not state.try_transition(
        doc, doc.get(), "submitted", allowed_from={"submitting"}
    )
    assert doc.data["status"] == "ready_for_review"


@pytest.fixture
def manual_client(monkeypatch):
    """The real mark-applied and submit routes over one fake document, with
    run_submission and the queue both recorded so any dispatch is visible."""
    monkeypatch.delenv("AUTO_SUBMIT_ENABLED", raising=False)
    doc = _ready(job_url="https://boards.example.com/acme/1")
    monkeypatch.setattr(applications, "_apps", lambda user_id: _FakeCollection(doc))
    dispatched: list[tuple] = []

    async def fake_run_submission(user_id, app_id, *, dry_run=False):
        dispatched.append(("background", app_id, dry_run))

    def fake_enqueue(queue, path, payload, *, task_id=None):
        dispatched.append(("queue", path, task_id))
        return True

    monkeypatch.setattr(applications, "run_submission", fake_run_submission)
    monkeypatch.setattr(applications.queues, "enqueue", fake_enqueue)
    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(client=TestClient(app), doc=doc, dispatched=dispatched)


def _mark(client):
    return client.post("/applications/app-job1/mark-applied")


@pytest.mark.parametrize("start", ["ready_for_review", "failed"])
@pytest.mark.parametrize("flag", [None, "1"])
def test_mark_applied_records_a_manual_submission(
    manual_client, monkeypatch, start, flag
):
    """Works whatever ``AUTO_SUBMIT_ENABLED`` says: applying yourself is the
    default path, not the fallback."""
    if flag:
        monkeypatch.setenv("AUTO_SUBMIT_ENABLED", flag)
    doc = manual_client.doc
    doc.set({**doc.data, "status": start, "submit_attempts": 2})

    resp = _mark(manual_client.client)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": True}
    data = doc.data
    assert data["status"] == "submitted"
    assert data["confirmation"]["method"] == "manual"
    submitted_at = datetime.fromisoformat(data["confirmation"]["submitted_at"])
    assert submitted_at.tzinfo is not None
    assert abs(datetime.now(UTC) - submitted_at) < timedelta(minutes=1)
    assert data["timeline"][-1]["status"] == "submitted"
    assert data["timeline"][-1]["note"] == applications.MANUAL_APPLY_NOTE
    # Nothing about the automated path moves: no claim, no counter, no task.
    assert data["submit_attempts"] == 2
    assert "last_submitted_at" not in data and "lease" not in data
    assert manual_client.dispatched == []
    # One write, and it is the compare-and-swap.
    ((fields, option),) = doc.updates
    assert option is not None and fields["status"] == "submitted"
    parsed = Application.model_validate(data)
    assert parsed.confirmation is not None
    assert parsed.confirmation.method == "manual"


@pytest.mark.parametrize(
    "start", ["queued", "tailoring", "submitting", "responded", "posting_removed"]
)
def test_mark_applied_is_refused_from_every_other_status(manual_client, start):
    doc = manual_client.doc
    doc.set({**doc.data, "status": start})
    writes = len(doc.updates)

    resp = _mark(manual_client.client)

    assert resp.status_code == 409
    assert resp.json()["detail"] == {"reason": "cannot_mark_applied", "current": start}
    assert doc.data["status"] == start
    assert "confirmation" not in doc.data
    assert len(doc.updates) == writes


def test_mark_applied_twice_changes_nothing_the_second_time(manual_client):
    client, doc = manual_client.client, manual_client.doc
    assert _mark(client).json() == {"ok": True, "changed": True}
    before = doc.data
    writes = len(doc.updates)

    resp = _mark(client)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": False}
    assert doc.data == before
    assert len(doc.updates) == writes


@pytest.mark.parametrize(
    "confirmation",
    [
        # Written before ``method`` existed: auto by definition.
        {"submitted_at": "2026-08-02T00:00:00+00:00", "screenshot_uri": "gs://b/c.png"},
        {"submitted_at": "2026-08-02T00:00:00+00:00", "method": "auto"},
    ],
)
def test_mark_applied_will_not_relabel_an_automated_submission(
    manual_client, confirmation
):
    doc = manual_client.doc
    doc.set({**doc.data, "status": "submitted", "confirmation": confirmation})
    before = doc.data
    writes = len(doc.updates)

    resp = _mark(manual_client.client)

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "reason": "already_submitted",
        "current": "submitted",
    }
    assert doc.data == before
    assert len(doc.updates) == writes


def test_mark_applied_loses_to_a_submit_that_claimed_first(manual_client):
    """The user clicks Submit and, on another tab, "I applied". The mark holds
    a read taken before the claim landed; it must not complete a document a
    browser is now driving, even though ``submitting → submitted`` is legal."""
    doc = manual_client.doc
    in_flight = doc.snapshot()
    assert state.try_transition(doc, doc.get(), "submitting")
    doc.pin(in_flight)

    resp = _mark(manual_client.client)

    assert resp.status_code == 409
    assert resp.json()["detail"]["current"] == "submitting"
    assert doc.data["status"] == "submitting"
    assert "confirmation" not in doc.data


def test_mark_applied_loses_to_the_sweep(manual_client):
    doc = manual_client.doc
    in_flight = doc.snapshot()
    state.try_transition(
        doc, doc.get(), "posting_removed", allowed_from=sweep.ACTIVE_APP_STATUSES
    )
    doc.pin(in_flight)

    resp = _mark(manual_client.client)

    assert resp.status_code == 409
    assert doc.data["status"] == "posting_removed"
    assert "confirmation" not in doc.data


def test_two_racing_marks_record_one_submission(manual_client):
    client, doc = manual_client.client, manual_client.doc
    in_flight = doc.snapshot()
    assert _mark(client).json()["changed"] is True
    doc.pin(in_flight)

    resp = _mark(client)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "changed": False}
    assert [e["status"] for e in doc.data["timeline"]].count("submitted") == 1


def test_mark_applied_404s_on_a_missing_application(manual_client):
    manual_client.doc.delete()
    assert _mark(manual_client.client).status_code == 404


# ---------------------------------------------------------------- the flag


def test_a_confirmation_without_a_method_is_an_automated_one():
    old = {"submitted_at": "2026-08-02T00:00:00+00:00", "screenshot_uri": "gs://b/c"}
    assert Confirmation.model_validate(old).method == "auto"
    app = Application.model_validate(
        {
            "id": "app-job1",
            "user_id": "u1",
            "job_id": "job1",
            "status": "submitted",
            "confirmation": old,
        }
    )
    assert app.confirmation is not None and app.confirmation.method == "auto"
    with pytest.raises(ValueError):
        Confirmation.model_validate({**old, "method": "carrier_pigeon"})


@pytest.mark.parametrize(
    ("raw", "on"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("ON", True),
        (" on ", True),
    ],
)
def test_auto_submit_is_off_unless_switched_on(monkeypatch, raw, on):
    from tools.submitters import router

    if raw is None:
        monkeypatch.delenv("AUTO_SUBMIT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AUTO_SUBMIT_ENABLED", raw)
    assert router.auto_submit_enabled() is on
    assert router.auto_submit_available("greenhouse") is on
    assert router.auto_submit_available("lever") is False
    assert router.auto_submit_available(None) is False


@pytest.mark.parametrize("queue_mode", [False, True])
def test_submit_is_refused_by_default_before_anything_happens(
    manual_client, monkeypatch, queue_mode
):
    """Flag unset: 409 with nothing claimed, written or dispatched, and the
    job's source never even read."""
    if queue_mode:
        monkeypatch.setenv("QUEUE_MODE", "1")
    reads: list = []
    monkeypatch.setattr(
        applications, "_job_sources", lambda u, ids: reads.append(ids) or {}
    )
    doc = manual_client.doc
    before = doc.data

    resp = manual_client.client.post("/applications/app-job1/submit")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "auto_submit_unavailable"
    assert doc.data == before
    assert doc.updates == []
    assert manual_client.dispatched == []
    assert reads == []


@pytest.mark.parametrize("source", ["lever", "ashby", "workable", "google_jobs", None])
def test_submit_is_refused_for_a_source_with_no_submitter(
    manual_client, monkeypatch, source
):
    _auto_submit_on(monkeypatch, source)
    resp = manual_client.client.post("/applications/app-job1/submit")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "auto_submit_unavailable"
    assert manual_client.doc.updates == []
    assert manual_client.dispatched == []


def test_submit_still_works_for_greenhouse_with_the_flag_on(manual_client, monkeypatch):
    _auto_submit_on(monkeypatch, "greenhouse")
    resp = manual_client.client.post("/applications/app-job1/submit")
    assert resp.status_code == 200
    assert manual_client.doc.data["status"] == "submitting"
    assert manual_client.dispatched == [("background", "app-job1", False)]


# ------------------------------------------------------- capability field


class _ListCollection:
    """``_apps()`` over several fake documents, with ``stream()``."""

    def __init__(self, docs: dict[str, _FakeDoc]):
        self._docs = docs

    def document(self, doc_id):
        return self._docs[doc_id]

    def stream(self):
        return [d.get() for d in self._docs.values()]


def _app_doc(app_id: str, job_id: str) -> _FakeDoc:
    return _FakeDoc(
        {
            "id": app_id,
            "user_id": "u1",
            "job_id": job_id,
            "job_url": f"https://boards.example.com/acme/{job_id}",
            "status": "ready_for_review",
            "timeline": [{"at": "2026-08-01T00:00:00+00:00", "status": "tailoring"}],
        },
        doc_id=app_id,
    )


@pytest.fixture
def capability_api(monkeypatch):
    docs = {
        "app-gh": _app_doc("app-gh", "gh"),
        "app-lever": _app_doc("app-lever", "lv"),
        "app-gone": _app_doc("app-gone", "missing"),
    }
    sources = {"gh": "greenhouse", "lv": "lever"}
    reads: list[set] = []

    def fake_sources(user_id, job_ids):
        reads.append(set(job_ids))
        return {j: sources.get(j) for j in job_ids}

    monkeypatch.setattr(applications, "_apps", lambda user_id: _ListCollection(docs))
    monkeypatch.setattr(applications, "_job_sources", fake_sources)
    app = FastAPI()
    app.include_router(applications.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return SimpleNamespace(client=TestClient(app), docs=docs, reads=reads)


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        (None, {"app-gh": False, "app-lever": False, "app-gone": False}),
        ("1", {"app-gh": True, "app-lever": False, "app-gone": False}),
    ],
)
def test_the_capability_field_on_get_and_list(
    capability_api, monkeypatch, flag, expected
):
    if flag is None:
        monkeypatch.delenv("AUTO_SUBMIT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AUTO_SUBMIT_ENABLED", flag)
    client = capability_api.client

    for app_id, want in expected.items():
        body = client.get(f"/applications/{app_id}").json()
        assert body["auto_submit"] is want, app_id

    listed = client.get("/applications").json()["applications"]
    assert {a["id"]: a["auto_submit"] for a in listed} == expected
    # Response-only: never written back to the document.
    for doc in capability_api.docs.values():
        assert "auto_submit" not in doc.data
        assert doc.updates == []
    if flag is None:
        assert capability_api.reads == []  # off costs no reads
    else:
        # The list reads every job's source in one batch, not one per row.
        assert capability_api.reads[-1] == {"gh", "lv", "missing"}


class _JobRef:
    def __init__(self, job_id):
        self.id = job_id


class _JobsClient:
    """Just enough ``firestore.Client`` for ``_job_sources``."""

    def __init__(self, jobs: dict[str, dict]):
        self._jobs = jobs
        self.batches: list[list[str]] = []

    def collection(self, name):
        return self

    def document(self, doc_id):
        return self if doc_id == "u1" else _JobRef(doc_id)

    def get_all(self, refs):
        refs = list(refs)
        self.batches.append([r.id for r in refs])
        return [_FakeSnap(r.id, self._jobs.get(r.id), 1) for r in refs]


def test_job_sources_is_one_batched_read(monkeypatch):
    db = _JobsClient({"gh": {"source": "greenhouse"}, "lv": {"source": "lever"}})
    monkeypatch.setattr(applications, "_client", lambda: db)

    assert applications._job_sources("u1", ["gh", "lv", "missing"]) == {
        "gh": "greenhouse",
        "lv": "lever",
        "missing": None,
    }
    assert db.batches == [["gh", "lv", "missing"]]
    assert applications._job_sources("u1", []) == {}
    assert len(db.batches) == 1
