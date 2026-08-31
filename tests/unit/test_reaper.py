# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The reaper: recovering applications whose worker died, without re-applying.

The headline case is ``test_a_clicked_submission_is_never_auto_retried`` and the
mutation evidence beside it. A crash *after* the Submit click may already have
filed a real application at a real company, so the one thing this module must
never do is put such a document back on a queue — that is the same duplicate
real job application ``tools.applications.state`` exists to prevent, arriving
from the other direction.

Everything else pins the properties that make that hold: a live lease is never
touched, an unleased ``submitting`` document is ambiguous rather than dead, the
retry loop is bounded, the dry run writes nothing, and every recovery is a
compare-and-swap that a document moving underneath it defeats.

``_FakeDoc`` is imported from ``test_application_state`` rather than copied: it
honours the update-time precondition for real, and that behaviour is what half
of this suite asserts against. A second copy would drift, and the drift would
silently stop these tests from testing anything.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.cloud import firestore
from test_application_state import _FakeDoc

from models.application import ApplicationStatus
from tools.applications import reaper, state

REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
LEASE = state.IN_PROGRESS["submitting"]
#: Past every lease and every age floor.
LATER = NOW + timedelta(seconds=LEASE + 1)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeQuery:
    """Honours ``limit`` for real — the pass's latency bound depends on it, and
    a fake that ignored it would let an unbounded query test as bounded."""

    def __init__(self, docs, statuses, limit=None, sink=None):
        self._docs = docs
        self._statuses = statuses
        self._limit = limit
        self._sink = sink

    def limit(self, n):
        # Recorded, because the bound has to be pushed to Firestore rather than
        # applied after streaming everything back: slicing in Python would tally
        # identically while still reading the whole collection over the wire,
        # which is the latency this cap exists to prevent.
        if self._sink is not None:
            self._sink.append(n)
        return _FakeQuery(self._docs, self._statuses, n, self._sink)

    def stream(self):
        sent = 0
        for doc in self._docs:
            if self._limit is not None and sent >= self._limit:
                return
            data = doc.data or {}
            if data.get("status") in self._statuses:
                snap = doc.snapshot()
                snap.reference = doc
                sent += 1
                yield snap


class _FakeApps:
    """The applications collection: streams whatever matches the ``in`` filter."""

    def __init__(self, docs):
        self._docs = docs
        self.filters = []
        self.limits: list[int] = []

    def where(self, filter=None):
        self.filters.append((filter.field_path, filter.op_string, filter.value))
        assert filter.op_string == "in", "a composite index would be needed"
        return _FakeQuery(self._docs, set(filter.value), sink=self.limits)


class _FakeUser:
    def __init__(self, apps):
        self._apps = apps

    def collection(self, name):
        assert name == "applications"
        return self._apps


class _Db:
    """``db.collection("users").document(uid).collection("applications")``."""

    def __init__(self, docs):
        self.apps = _FakeApps(docs)

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, user_id):
        return _FakeUser(self.apps)


class _Dispatcher:
    """Records every re-dispatch. ``calls`` being empty is an assertion."""

    def __init__(self, scheduled=True):
        self.calls: list[tuple[str, str]] = []
        self.scheduled = scheduled

    def __call__(self, user_id: str, job_id: str) -> bool:
        self.calls.append((user_id, job_id))
        return self.scheduled


def _app(status, *, lease=None, timeline_age_s=None, doc_id="app-job1", **extra):
    """One application document, aged relative to :data:`NOW`."""
    at = NOW - timedelta(seconds=timeline_age_s if timeline_age_s is not None else 0)
    data = {
        "id": doc_id,
        "user_id": "u1",
        "job_id": "job1",
        "status": status,
        "timeline": [{"at": at.isoformat(), "status": status}],
        **extra,
    }
    if lease is not None:
        data["lease"] = lease
    return _FakeDoc(data, doc_id=doc_id)


def _reap(docs, *, dispatch=None, now=LATER, execute=True, max_attempts=None):
    dispatch = dispatch or _Dispatcher()
    kwargs = {} if max_attempts is None else {"max_attempts": max_attempts}
    tally = reaper.reap_applications(
        "u1",
        dispatch=dispatch,
        db=_Db(docs),
        now=now,
        execute=execute,
        **kwargs,
    )
    return tally, dispatch


def _reap_limited(docs, *, limit, now=LATER):
    dispatch = _Dispatcher()
    db = _Db(docs)
    tally = reaper.reap_applications(
        "u1", dispatch=dispatch, db=db, now=now, max_per_pass=limit
    )
    return tally, dispatch, db


# --------------------------------------------------------------------------
# THE apply fork. A crash after the click may already be a real application.
# --------------------------------------------------------------------------


def test_a_clicked_submission_is_never_auto_retried():
    """**The safety property of this PR, and the mutation evidence for it.**

    ``submit_attempted_at`` means a browser reached ``submit_btn.click()``. The
    application may be sitting in the employer's ATS right now, and nothing can
    withdraw it. So this document is failed, flagged, and told to the user in
    words — and it is *never* handed back to a queue. If this test fails because
    ``dispatch.calls`` is non-empty, the reaper has become the thing that files
    duplicate applications, which ``apply.max_attempts = 1`` exists to prevent.
    """
    doc = _app(
        "submitting",
        lease=state.lease_for("submitting", owner="dead", now=NOW),
        submit_attempted_at=NOW.isoformat(),
    )

    tally, dispatch = _reap([doc])

    assert dispatch.calls == []  # <- the assertion this whole PR is for
    assert tally["release_uncertain"] == 1
    assert doc.data["status"] == "failed"
    assert doc.data[reaper.UNCERTAIN_FIELD] is True
    assert "lease" not in doc.data
    note = doc.data["timeline"][-1]["note"]
    assert "UNKNOWN" in note and "email" in note.lower()


@pytest.mark.parametrize("marker", [True, "2026-08-26T12:00:00+00:00", 1])
def test_no_verdict_that_dispatches_is_reachable_with_the_marker_set(marker):
    """Stronger than the case above: for a ``submitting`` document carrying the
    marker there is no lease age, no attempt count and no cap that yields a
    dispatching verdict. The fork is a property of the classifier, not of one
    interleaving."""
    for age in (0, LEASE - 1, LEASE + 1, LEASE * 99):
        for attempts in (0, 1, 2, 3, 99):
            doc = {
                "status": "submitting",
                "job_id": "job1",
                "lease": state.lease_for("submitting", owner="dead", now=NOW),
                reaper.CLICKED_FIELD: marker,
                reaper.ATTEMPTS_FIELD: attempts,
                "timeline": [],
            }
            verdict = reaper.classify(doc, now=NOW + timedelta(seconds=age))
            assert verdict not in reaper.DISPATCHING, (age, attempts)
            assert verdict in ("alive", "release_uncertain")


def test_the_never_clicked_branch_says_nothing_was_submitted():
    """The other half of the fork. No marker means the browser never clicked, so
    the honest thing — and the useful one — is to tell the user it is safe."""
    doc = _app("submitting", lease=state.lease_for("submitting", owner="d", now=NOW))

    tally, dispatch = _reap([doc])

    assert tally["release_unstarted"] == 1
    assert doc.data["status"] == "failed"
    assert reaper.UNCERTAIN_FIELD not in doc.data
    assert "safe to submit again" in doc.data["timeline"][-1]["note"]
    # Still not auto-retried: the user submits again by hand, which is what
    # makes a *new* submit_attempts value and therefore a new apply task name.
    assert dispatch.calls == []


def test_a_marker_that_lands_before_the_claim_is_seen_by_the_re_read():
    """The reason the fork is re-decided *after* the claim rather than from the
    streamed snapshot.

    ``try_claim_lease`` retries against a fresh read, so it can succeed on a
    document one write newer than the one the pass was handed — and that write
    can be the click marker. Deciding from the stale read would write ``failed``
    saying "nothing was submitted" about an application that went out.

    Pinned on the *shape* of the write: the flag has to ride in the same update
    as the status, which is only possible if the re-read happened before it. The
    read-back correction further down would reach the same end state through two
    extra writes, so asserting the end state alone would not test this.
    """
    doc = _app("submitting", lease=state.lease_for("submitting", owner="d", now=NOW))
    real_update = doc.update
    writes = {"n": 0}

    def click_then_update(fields, option=None):
        writes["n"] += 1
        if writes["n"] == 1:  # the reaper's claim, losing its precondition
            real_update({reaper.CLICKED_FIELD: LATER.isoformat()})
        return real_update(fields, option)

    doc.update = click_then_update

    tally, dispatch = _reap([doc])

    assert tally["release_uncertain"] == 1
    assert dispatch.calls == []
    terminal = [f for f, _o in doc.updates if f.get("status") == "failed"]
    assert len(terminal) == 1
    assert terminal[0][reaper.UNCERTAIN_FIELD] is True  # one write, not a repair


def test_a_marker_that_lands_during_the_release_is_corrected_to_uncertain():
    """The interleaving ``try_transition``'s own retry could otherwise swallow.

    The reaper reads a document with no marker, and a zombie run — alive past
    its lease, which is the only way to be here — clicks Submit and writes the
    marker before the reaper's write lands. The precondition fails, the retry
    re-reads and re-checks the *status* but not the marker, so without the
    read-back below the user would be told "nothing was submitted" about an
    application that went out.
    """
    doc = _app("submitting", lease=state.lease_for("submitting", owner="d", now=NOW))
    real_update = doc.update
    writes = {"n": 0}

    def click_then_update(fields, option=None):
        writes["n"] += 1
        # Write 1 is the reaper's lease claim; write 2 is its transition to
        # ``failed``, decided from a re-read that showed no marker. The zombie's
        # progress callback lands in between, which costs that transition its
        # precondition — and try_transition's retry re-checks the status but not
        # the marker, so the retry succeeds with the stale verdict.
        if writes["n"] == 2:
            real_update({reaper.CLICKED_FIELD: LATER.isoformat()})
        return real_update(fields, option)

    doc.update = click_then_update

    tally, dispatch = _reap([doc])

    assert doc.data["status"] == "failed"
    assert doc.data[reaper.UNCERTAIN_FIELD] is True
    assert tally["release_uncertain"] == 1
    assert dispatch.calls == []
    assert any("UNKNOWN" in (e.get("note") or "") for e in doc.data["timeline"])


def test_the_reaper_adds_no_new_application_status():
    """``web/src/lib/types.ts`` is a closed union and the tracking page renders
    an unknown status as "failed — open to retry", which is the exact wrong
    thing to say about a submission that may have gone through. So the fork is
    carried by a boolean beside a status the UI already knows."""
    doc = _app(
        "submitting",
        lease=state.lease_for("submitting", owner="d", now=NOW),
        submit_attempted_at=NOW.isoformat(),
    )
    _reap([doc])
    assert doc.data["status"] in set(ApplicationStatus.__args__)


def test_submitting_only_ever_leaves_towards_failed():
    """Pins the table rather than the code path: ``submitting →
    ready_for_review`` is not an edge, and the reaper must not need it to be.

    Opening it *used* to be unsafe on top of that: ``run_tailoring`` published
    with a bare ``→ ready_for_review``, so a slow duplicate tailoring run could
    have ridden the new edge to move a live submission back to reviewable and
    clear the submitter's lease. Both of its terminal writes now carry
    ``allowed_from={"tailoring"}``, so what is left is the plain reason —
    "ready to send" is the wrong thing to say about an application that may
    already be at the employer — and the reaper still does not need the edge."""
    assert "ready_for_review" not in state.TRANSITIONS["submitting"]
    assert state.TRANSITIONS["submitting"] == frozenset(
        {"submitted", "failed", "posting_removed"}
    )


# --------------------------------------------------------------------------
# Unleased ``submitting``: ambiguous, not dead
# --------------------------------------------------------------------------


def test_an_unleased_submitting_document_is_never_reaped():
    """``state.IN_PROGRESS`` says why: the submit request writes the status and
    the run writes the lease, so there is a real window in which a submission is
    claimed but not yet leased. Reading absence as "the owner is gone" would
    fail an application that is about to be sent. Deferred to
    ``cli/unwedge_submitting``'s age arithmetic, which is an operator call."""
    doc = _app("submitting", timeline_age_s=LEASE * 99)
    assert "lease" not in doc.data

    tally, dispatch = _reap([doc], now=LATER)

    assert tally["ambiguous"] == 1
    assert tally["recovered"] == 0
    assert doc.updates == []  # not one write, at any age
    assert doc.data["status"] == "submitting"
    assert dispatch.calls == []


def test_the_cli_that_does_adjudicate_those_still_exists():
    """The deferral is only honest if the thing deferred to is real."""
    from cli.unwedge_submitting import DEFAULT_MIN_AGE_MINUTES, is_wedged

    doc = {"status": "submitting", "timeline": []}
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is True


# --------------------------------------------------------------------------
# Staleness: the lease decides
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "tailoring", "submitting"])
def test_a_live_lease_is_never_touched(status):
    """First-hand evidence from the process doing the work beats every
    inference. A run inside its lease is running, however old the document."""
    doc = _app(
        status,
        lease=state.lease_for(status, owner="alive", now=NOW),
        timeline_age_s=LEASE * 99,
    )

    tally, dispatch = _reap([doc], now=NOW + timedelta(seconds=LEASE - 1))

    assert tally["alive"] == 1
    assert doc.updates == []
    assert dispatch.calls == []


@pytest.mark.parametrize("lease", [{"status": "submitting"}, {}, {"expires_at": "x"}])
def test_a_lease_that_cannot_be_read_counts_as_live(lease):
    """``state.lease_is_held`` is asymmetric on purpose, and the reaper inherits
    that: refusing to act wedges a document, which is undoable; acting anyway on
    a submission is not."""
    doc = _app("submitting", lease=lease, timeline_age_s=LEASE * 99)
    assert reaper.classify(doc.data, now=LATER) == "alive"


@pytest.mark.parametrize("lease", ["not-a-dict", 7, ["a"]])
def test_a_lease_field_that_is_not_a_lease_is_not_read_as_an_expired_one(lease):
    """The gap ``in doc`` would leave. ``state.lease_is_held`` reads a non-dict
    as *unheld*, so keying off the field's presence would turn a document
    nothing understands into one safe to fail — on the single status where
    getting that wrong costs a duplicate real application."""
    doc = _app("submitting", lease=lease, timeline_age_s=LEASE * 99)
    assert reaper.classify(doc.data, now=LATER) == "ambiguous"


def test_queued_is_the_one_status_decided_by_age():
    """Nothing claims ``queued`` in the ordinary flow — ``run_tailoring`` claims
    by *leaving* it — so there is no lease to read until the reaper writes one."""
    fresh = _app("queued", timeline_age_s=state.IN_PROGRESS["queued"] - 1)
    stale = _app("queued", timeline_age_s=state.IN_PROGRESS["queued"])

    assert reaper.classify(fresh.data, now=NOW) == "alive"
    assert reaper.classify(stale.data, now=NOW) == "redispatch"


def test_a_document_with_no_usable_timeline_is_stale():
    """No timeline at all means it predates everything this build writes."""
    doc = _FakeDoc({"status": "queued", "job_id": "job1", "timeline": []})
    assert reaper.classify(doc.data, now=NOW) == "redispatch"
    assert reaper.last_activity_at(doc.data) is None


@pytest.mark.parametrize("value", [None, "", "not-a-date", 12345, [1, 2]])
def test_unusable_timestamps_do_not_crash_the_pass(value):
    doc = {"status": "queued", "timeline": [{"at": value}]}
    assert reaper.last_activity_at(doc) is None


def test_naive_timestamps_are_read_as_utc():
    doc = {"status": "queued", "timeline": [{"at": "2026-08-26T11:00:00"}]}
    assert reaper.last_activity_at(doc) == NOW - timedelta(hours=1)


# --------------------------------------------------------------------------
# The recovery table
# --------------------------------------------------------------------------


def test_a_stale_queued_application_is_re_dispatched_under_a_claim():
    """The claim is the compare-and-swap — ``queued → queued`` is not an edge,
    so ``try_claim_lease`` is what proves the status is still queued and nobody
    else is working it, and the counter rides inside that same write."""
    doc = _app("queued", timeline_age_s=LEASE * 2)

    tally, dispatch = _reap([doc], now=NOW)

    assert tally["redispatch"] == 1
    assert dispatch.calls == [("u1", "job1")]
    assert doc.data["status"] == "queued"  # nothing moved; the work was re-sent
    assert doc.data[reaper.ATTEMPTS_FIELD] == 1
    assert doc.data["lease"]["status"] == "queued"
    assert len(doc.updates) == 1  # claim and counter in one write


def test_the_claim_a_re_dispatch_leaves_is_the_back_off():
    """Otherwise every hourly tick re-dispatches the same document forever."""
    doc = _app("queued", timeline_age_s=LEASE * 2)
    _reap([doc], now=NOW)

    tally, dispatch = _reap([doc], now=NOW + timedelta(seconds=LEASE - 1))

    assert tally["alive"] == 1
    assert dispatch.calls == []
    assert doc.data[reaper.ATTEMPTS_FIELD] == 1  # not advanced by a pass that skipped


def test_a_stale_tailoring_application_goes_back_to_queued_and_is_re_dispatched():
    """Tailoring is ~$0.002 a run and ``run_tailoring`` claims before it spends
    anything, so a bounded automatic retry is cheap and cannot double-charge."""
    doc = _app("tailoring", lease=state.lease_for("tailoring", owner="dead", now=NOW))

    tally, dispatch = _reap([doc])

    assert tally["requeue"] == 1
    assert doc.data["status"] == "queued"
    assert doc.data[reaper.ATTEMPTS_FIELD] == 1
    assert doc.data["timeline"][-1]["note"] == reaper.REQUEUE_NOTE
    # Lands with a fresh queued lease, so the next pass backs off rather than
    # re-dispatching an hour later.
    assert doc.data["lease"]["status"] == "queued"
    assert dispatch.calls == [("u1", "job1")]


def test_an_unleased_tailoring_document_falls_back_to_age():
    """Unlike ``submitting``, ``tailoring`` writes its status and its lease in
    one write, so an absent lease there means a document older than leases —
    not a run that has not started yet."""
    doc = _app("tailoring", timeline_age_s=LEASE * 2)
    assert reaper.classify(doc.data, now=NOW) == "requeue"

    fresh = _app("tailoring", timeline_age_s=1)
    assert reaper.classify(fresh.data, now=NOW) == "alive"


@pytest.mark.parametrize("status", ["queued", "tailoring"])
def test_the_retry_loop_is_bounded(status):
    """At the cap the application is failed with a note, so a document that can
    never be tailored stops looping and starts being visible to the user."""
    lease = None if status == "queued" else state.lease_for(status, now=NOW)
    doc = _app(
        status,
        lease=lease,
        timeline_age_s=LEASE * 2,
        **{reaper.ATTEMPTS_FIELD: reaper.MAX_ATTEMPTS},
    )

    tally, dispatch = _reap([doc])

    assert tally["give_up"] == 1
    assert doc.data["status"] == "failed"
    assert doc.data["timeline"][-1]["note"] == reaper.GAVE_UP_NOTE
    assert "lease" not in doc.data
    assert dispatch.calls == []


def test_the_cap_counts_recoveries_not_ticks():
    """Three retries, then failed — driven end to end rather than asserted on
    the classifier, because the counter has to survive real writes."""
    doc = _app("queued", timeline_age_s=LEASE * 2)
    seen = []
    now = NOW
    for _ in range(reaper.MAX_ATTEMPTS + 1):
        tally, _dispatch = _reap([doc], now=now)
        seen.append(next(k for k in ("redispatch", "give_up") if tally[k]))
        # Age past the lease this pass just wrote.
        now += timedelta(seconds=LEASE * 2)

    assert seen == ["redispatch"] * reaper.MAX_ATTEMPTS + ["give_up"]
    assert doc.data["status"] == "failed"


@pytest.mark.parametrize("attempts", [-5, "seven", None, {"a": 1}])
def test_a_malformed_counter_reads_as_zero(attempts):
    doc = _app("queued", timeline_age_s=LEASE * 2, **{reaper.ATTEMPTS_FIELD: attempts})
    assert reaper.attempts(doc.data) == 0
    assert reaper.classify(doc.data, now=NOW) == "redispatch"


# --------------------------------------------------------------------------
# Races: every write carries its precondition
# --------------------------------------------------------------------------


def test_two_reapers_recover_a_document_once():
    """The claim is what makes an overlapping tick — a scheduler retry, two
    revisions during a rollout — a no-op rather than a double dispatch."""
    doc = _app("queued", timeline_age_s=LEASE * 2)
    in_flight = doc.snapshot()
    in_flight.reference = doc

    first = _Dispatcher()
    reaper.reap_one(
        doc, doc.get(), doc.data, "redispatch", user_id="u1", dispatch=first, now=NOW
    )
    second = _Dispatcher()
    outcome = reaper.reap_one(
        doc,
        in_flight,
        in_flight.to_dict(),
        "redispatch",
        user_id="u1",
        dispatch=second,
        now=NOW,
    )

    assert first.calls == [("u1", "job1")]
    assert second.calls == []
    assert outcome == "lost_race"
    assert doc.data[reaper.ATTEMPTS_FIELD] == 1  # the loser advanced nothing


def test_a_recovery_whose_document_moved_writes_nothing_and_hands_the_claim_back():
    """The user clicked Submit between the reaper's read and its write. The
    ``allowed_from`` inside the swap refuses, and the claim the reaper took on
    the way in is handed back rather than left sitting on someone else's
    status for a full lease."""
    doc = _app("tailoring", lease=state.lease_for("tailoring", owner="dead", now=NOW))
    stale = doc.snapshot()
    stale.reference = doc
    # A regenerate lands first: tailoring -> queued, so the verdict is void.
    state.try_transition(doc, doc.get(), "queued", lease=state.CLEAR_LEASE)

    dispatch = _Dispatcher()
    outcome = reaper.reap_one(
        doc,
        stale,
        stale.to_dict(),
        "requeue",
        user_id="u1",
        dispatch=dispatch,
        now=LATER,
    )

    assert outcome == "lost_race"
    assert dispatch.calls == []
    assert doc.data["status"] == "queued"
    assert "lease" not in doc.data  # the reaper's own claim did not stay behind


def test_a_release_that_loses_leaves_the_submission_alone():
    """The dangerous direction: the real submission was merely slow and reported
    back between the read and the write. It wins; the reaper writes nothing."""
    doc = _app("submitting", lease=state.lease_for("submitting", owner="d", now=NOW))
    stale = doc.snapshot()
    stale.reference = doc
    state.try_transition(doc, doc.get(), "submitted", lease=state.CLEAR_LEASE)

    outcome = reaper.reap_one(
        doc,
        stale,
        stale.to_dict(),
        "release_unstarted",
        user_id="u1",
        dispatch=_Dispatcher(),
        now=LATER,
    )

    assert outcome == "lost_race"
    assert doc.data["status"] == "submitted"
    assert reaper.UNCERTAIN_FIELD not in doc.data


def test_a_failed_dispatch_is_never_rolled_back():
    """PR C's lesson, and it bites harder here: an enqueue can report failure
    and still have created the task, so undoing the claim would clear the lease
    of a run that may already be going. The document keeps its claim, the
    counter is already advanced, and the next pass tries again."""
    doc = _app("queued", timeline_age_s=LEASE * 2)

    def explode(user_id, job_id):
        raise RuntimeError("Cloud Tasks is unreachable")

    tally, _ = _reap([doc], dispatch=explode, now=NOW)

    assert tally["not_dispatched"] == 1
    assert doc.data["status"] == "queued"
    assert doc.data[reaper.ATTEMPTS_FIELD] == 1
    assert doc.data["lease"]["status"] == "queued"


def test_a_deduped_dispatch_is_reported_not_retried_in_place():
    doc = _app("queued", timeline_age_s=LEASE * 2)
    tally, _ = _reap([doc], dispatch=_Dispatcher(scheduled=False), now=NOW)
    assert tally["not_dispatched"] == 1
    assert tally["recovered"] == 0


def test_a_deleted_document_is_not_resurrected():
    doc = _app("queued", timeline_age_s=LEASE * 2)
    stale = doc.snapshot()
    stale.reference = doc
    doc.delete()

    outcome = reaper.reap_one(
        doc,
        stale,
        stale.to_dict(),
        "redispatch",
        user_id="u1",
        dispatch=_Dispatcher(),
        now=NOW,
    )

    assert outcome == "lost_race"
    assert doc.data is None and doc.sets == []


def test_one_bad_document_does_not_abandon_the_pass():
    """The rest of this user's stuck applications are still stuck."""
    bad = _app("queued", timeline_age_s=LEASE * 2, doc_id="app-bad")

    def explode(fields, option=None):
        raise RuntimeError("Firestore said no")

    bad.update = explode
    good = _app("queued", timeline_age_s=LEASE * 2, doc_id="app-good")

    tally, dispatch = _reap([bad, good], now=NOW)

    assert tally["errors"] == 1
    assert tally["redispatch"] == 1
    assert dispatch.calls == [("u1", "job1")]


# --------------------------------------------------------------------------
# The dry run
# --------------------------------------------------------------------------


def test_the_dry_run_takes_no_lease_writes_nothing_and_dispatches_nothing():
    """A dry run that acts is how PR B shipped a bug. The whole read path, none
    of the write path — including the claim, which is a write."""
    docs = [
        _app("queued", timeline_age_s=LEASE * 2, doc_id="a1"),
        _app("tailoring", lease=state.lease_for("tailoring", now=NOW), doc_id="a2"),
        _app(
            "submitting",
            lease=state.lease_for("submitting", now=NOW),
            submit_attempted_at=NOW.isoformat(),
            doc_id="a3",
        ),
    ]

    tally, dispatch = _reap(docs, execute=False)

    assert [d.updates for d in docs] == [[], [], []]
    assert dispatch.calls == []
    assert tally["redispatch"] == tally["requeue"] == tally["release_uncertain"] == 1
    assert tally["recovered"] == 3  # what an --execute would have moved


def test_the_dry_run_reports_the_same_verdicts_execute_acts_on():
    """Otherwise the report is not a preview of anything."""
    docs = [
        _app("queued", timeline_age_s=LEASE * 2, doc_id="a1"),
        _app("submitting", timeline_age_s=LEASE * 2, doc_id="a2"),
    ]
    preview, _ = _reap(docs, execute=False, now=LATER)
    applied, _ = _reap(docs, execute=True, now=LATER)

    assert preview["redispatch"] == applied["redispatch"] == 1
    assert preview["ambiguous"] == applied["ambiguous"] == 1


# --------------------------------------------------------------------------
# The pass itself
# --------------------------------------------------------------------------


def test_the_query_is_single_field_so_no_composite_index_is_needed():
    docs = [_app("submitted", doc_id="done"), _app("queued", doc_id="a1")]
    db = _Db(docs)
    reaper.reap_applications("u1", dispatch=_Dispatcher(), db=db, now=NOW)

    assert db.apps.filters == [("status", "in", reaper.REAPABLE)]
    assert set(reaper.REAPABLE) == set(state.IN_PROGRESS)
    assert docs[0].updates == []  # a submitted application is not even scanned


def test_the_tally_accounts_for_every_scanned_document():
    """A document this pass declines to act on stays stuck, so nothing may be
    counted nowhere."""
    docs = [
        _app("queued", timeline_age_s=LEASE * 2, doc_id="a1"),  # redispatch
        _app("queued", timeline_age_s=1, doc_id="a2"),  # alive
        _app("submitting", timeline_age_s=LEASE * 2, doc_id="a3"),  # ambiguous
    ]
    tally, _ = _reap(docs, now=NOW)

    counted = sum(
        tally[k]
        for k in (
            "alive",
            "ambiguous",
            "redispatch",
            "requeue",
            "give_up",
            "release_unstarted",
            "release_uncertain",
            "lost_race",
            "not_dispatched",
            "errors",
        )
    )
    assert tally["scanned"] == counted == 3


# --------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------


def test_the_reaper_writes_no_status_field_directly():
    """Same scan as ``test_application_state``: the state machine is only a
    guarantee if it stays the sole writer of ``status``."""
    source = (REPO_ROOT / "tools" / "applications" / "reaper.py").read_text()
    assert '"status":' not in source
    assert "STATUS_FIELD" in source  # it goes through the module's own constant


def test_no_recovery_ever_writes_the_click_marker():
    """Only the code standing next to ``submit_btn.click()`` may claim a browser
    clicked. A reaper that could write this field could talk itself *out* of the
    uncertain branch — so drive the whole recovery table and check none of them
    invents one."""
    docs = [
        _app("queued", timeline_age_s=LEASE * 2, doc_id="a1"),
        _app("tailoring", lease=state.lease_for("tailoring", now=NOW), doc_id="a2"),
        _app("submitting", lease=state.lease_for("submitting", now=NOW), doc_id="a3"),
        _app(
            "queued",
            timeline_age_s=LEASE * 2,
            doc_id="a4",
            **{reaper.ATTEMPTS_FIELD: reaper.MAX_ATTEMPTS},
        ),
    ]

    _reap(docs)

    for doc in docs:
        assert reaper.CLICKED_FIELD not in (doc.data or {}), doc.id


# --------------------------------------------------------------------------
# cli.reap_applications: the operator's copy, and its --execute gate
# --------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """``cli.reap_applications.main`` over the fakes, with argv and the client
    swapped out. The dispatcher is stubbed because the real one reaches
    ``api.routes.applications`` and, without a queue, would only log."""
    import cli.reap_applications as tool

    def run(docs, *argv):
        dispatched: list[tuple] = []
        monkeypatch.setattr(tool.firestore, "Client", lambda: _Db(docs))
        monkeypatch.setattr(
            tool, "dispatch", lambda u, j: dispatched.append((u, j)) or True
        )
        # The module-level name, not datetime.datetime itself, which is
        # immutable. Freezing it keeps the verdicts independent of the wall
        # clock the fixtures are aged against.
        monkeypatch.setattr(
            tool, "datetime", SimpleNamespace(now=lambda tz=None: LATER)
        )
        monkeypatch.setattr("sys.argv", ["reap_applications", *argv])
        tool.main()
        return dispatched

    return run


def test_the_cli_is_dry_run_by_default(cli):
    """Like ``reset_user``, ``geo_resurrect`` and ``unwedge_submitting`` — and
    unlike ``purge_discarded``, which is the one in that directory not to copy.
    Without ``--execute`` an operator inspecting a user's stuck applications
    must not move any of them."""
    docs = [
        _app("queued", timeline_age_s=LEASE * 2, doc_id="a1"),
        _app(
            "submitting",
            lease=state.lease_for("submitting", owner="d", now=NOW),
            submit_attempted_at=NOW.isoformat(),
            doc_id="a2",
        ),
    ]

    dispatched = cli(docs, "--user-id", "u1")

    assert [d.updates for d in docs] == [[], []]
    assert dispatched == []
    assert docs[0].data["status"] == "queued"
    assert docs[1].data["status"] == "submitting"


def test_the_cli_acts_only_with_execute(cli):
    """Positive control: the gate above is a gate, not a broken tool."""
    docs = [_app("queued", timeline_age_s=LEASE * 2, doc_id="a1")]

    dispatched = cli(docs, "--user-id", "u1", "--execute")

    assert dispatched == [("u1", "job1")]
    assert docs[0].data[reaper.ATTEMPTS_FIELD] == 1


def test_the_cli_cannot_re_submit_a_clicked_application_either(cli):
    """The operator path reaches the same fork through the same code. A targeted
    ``--execute`` must not be a way around the one rule that matters."""
    docs = [
        _app(
            "submitting",
            lease=state.lease_for("submitting", owner="d", now=NOW),
            submit_attempted_at=NOW.isoformat(),
            doc_id="a1",
        )
    ]

    dispatched = cli(docs, "--user-id", "u1", "--execute")

    assert dispatched == []
    assert docs[0].data["status"] == "failed"
    assert docs[0].data[reaper.UNCERTAIN_FIELD] is True


# --------------------------------------------------------------------------
# The allowed_from guards.
#
# Both recoveries below take TWO writes: a claim, then a status transition. The
# tests further up ("...whose document moved...", "...leaves the submission
# alone") lose at the *claim* and never reach the transition, so they pin the
# claim and nothing else. These two get past the claim and then move the
# document, which is the only interleaving the guard exists for — and the only
# one that fails if it is deleted.
# --------------------------------------------------------------------------


def _move_between_claim_and_transition(doc, move):
    """Let the claim land, then run ``move`` before the next write goes out."""
    real_update = doc.update
    writes = {"n": 0}

    def hooked(fields, option=None):
        writes["n"] += 1
        if writes["n"] == 2:  # the transition; write 1 was the claim
            move()
        return real_update(fields, option)

    doc.update = hooked
    return writes


def test_give_up_will_not_fail_a_regenerate_the_user_just_asked_for():
    """**The guard at the ``failed`` write, and a real regression without it.**

    A ``tailoring`` document at the attempt cap. The reaper claims it, and in
    the round trip before the terminal write the user clicks Regenerate, which
    CASes ``tailoring → queued`` and enqueues a fresh run. The reaper's re-read
    now returns ``queued`` — and ``queued → failed`` is a perfectly legal edge,
    so only ``allowed_from={status}`` refuses it. Without the guard the reaper
    marks the run the user just requested ``failed``.
    """
    doc = _app(
        "tailoring",
        lease=state.lease_for("tailoring", owner="dead", now=NOW),
        **{reaper.ATTEMPTS_FIELD: reaper.MAX_ATTEMPTS},
    )
    assert reaper.classify(doc.data, now=LATER) == "give_up"

    def user_clicks_regenerate():
        state.try_transition(doc, doc.get(), "queued", note="regenerate")

    _move_between_claim_and_transition(doc, user_clicks_regenerate)

    tally, _ = _reap([doc])

    assert tally["give_up"] == 0
    assert tally["lost_race"] == 1
    assert doc.data["status"] == "queued"  # the user's regenerate stands
    assert reaper.GAVE_UP_NOTE not in [e.get("note") for e in doc.data["timeline"]]
    assert "lease" not in doc.data  # and the reaper's claim went back


def test_a_release_will_not_fail_a_document_that_left_submitting():
    """Same guard, the branch that matters most: ``submitting`` is claimed, and
    the real submission reports ``submitted`` before the reaper's write lands.
    ``submitted → failed`` is illegal, so the table alone would refuse — which is
    why this asserts on ``posting_removed`` instead, an edge that *is* legal from
    both ``submitting`` and the status the reaper thinks it holds."""
    doc = _app("submitting", lease=state.lease_for("submitting", owner="d", now=NOW))

    def the_posting_dies_first():
        state.try_transition(doc, doc.get(), "posting_removed")

    _move_between_claim_and_transition(doc, the_posting_dies_first)

    tally, dispatch = _reap([doc])

    assert tally["lost_race"] == 1
    assert doc.data["status"] == "posting_removed"
    assert reaper.UNCERTAIN_FIELD not in doc.data
    assert dispatch.calls == []


def test_requeue_will_not_throw_away_a_result_that_arrived_late():
    """**The guard at the ``→ queued`` write.**

    The interleaving has to land on a status the *table* still allows, or it
    tests the table instead of the guard — ``submitting → queued`` is not an
    edge at all, so a race into ``submitting`` would be refused either way.
    ``ready_for_review → queued`` **is** legal (it is what Regenerate uses), and
    it is the reachable case: the tailoring run was not dead, only slower than
    its lease, and it publishes while the reaper holds a claim.

    Without ``allowed_from={"tailoring"}`` the reaper drags a finished,
    reviewable application back into the queue and re-dispatches it — throwing
    away the result the user is looking at and paying for another run.
    """
    doc = _app("tailoring", lease=state.lease_for("tailoring", owner="slow", now=NOW))

    def the_slow_run_publishes():
        state.try_transition(
            doc, doc.get(), "ready_for_review", lease=state.CLEAR_LEASE
        )

    _move_between_claim_and_transition(doc, the_slow_run_publishes)

    tally, dispatch = _reap([doc])

    assert tally["requeue"] == 0
    assert tally["lost_race"] == 1
    assert doc.data["status"] == "ready_for_review"  # the result stands
    assert reaper.REQUEUE_NOTE not in [e.get("note") for e in doc.data["timeline"]]
    assert dispatch.calls == []  # and nothing was re-tailored over the top of it
    assert "lease" not in doc.data  # the reaper's claim went back


# --------------------------------------------------------------------------
# The counter's epoch. Without one the cap is a lifetime total, and the loop it
# closes has a wrong instruction inside it.
# --------------------------------------------------------------------------


def test_a_successful_tailoring_clears_the_recovery_budget():
    """The epoch that matters most, because it needs no user action.

    ``run_tailoring``'s publish is the event that proves the pipeline works for
    this document, so the count of *consecutive* failures resets there — in the
    same swap, so a publish that loses resets nothing.
    """
    doc = _app("tailoring", **{reaper.ATTEMPTS_FIELD: reaper.MAX_ATTEMPTS})

    assert (
        state.try_transition(
            doc,
            doc.get(),
            "ready_for_review",
            lease=state.CLEAR_LEASE,
            extra={reaper.ATTEMPTS_FIELD: firestore.DELETE_FIELD},
        )
        is True
    )

    assert doc.data is not None and reaper.ATTEMPTS_FIELD not in doc.data
    assert reaper.attempts(doc.data) == 0


def test_the_publish_that_loses_its_race_resets_nothing():
    """Riding the swap is the point: the content write beside it has no
    precondition, and a reset there could land on a document a regenerate had
    already moved on."""
    doc = _app("tailoring", **{reaper.ATTEMPTS_FIELD: 2})
    stale = doc.snapshot()
    state.try_transition(doc, doc.get(), "queued", note="regenerate")  # the user

    assert (
        state.try_transition(
            doc,
            stale,
            "ready_for_review",
            extra={reaper.ATTEMPTS_FIELD: firestore.DELETE_FIELD},
        )
        is False
    )
    assert doc.data[reaper.ATTEMPTS_FIELD] == 2


def test_an_exhausted_budget_does_not_survive_a_successful_run(monkeypatch):
    """**The closed loop, driven end to end.**

    Three recoveries during a queue outage, then the tailoring finally lands.
    Without an epoch the document sits permanently one stale tick from
    ``give_up`` — and that give_up dispatches nothing while telling the user to
    press Regenerate, which is the one button that cannot help.
    """
    doc = _app("queued", timeline_age_s=LEASE * 2)
    now = NOW
    for _ in range(reaper.MAX_ATTEMPTS):
        _reap([doc], now=now)
        now += timedelta(seconds=LEASE * 2)
    assert doc.data[reaper.ATTEMPTS_FIELD] == reaper.MAX_ATTEMPTS
    assert reaper.classify(doc.data, now=now) == "give_up"  # the trap, armed

    # ...and then a tailoring run succeeds, exactly as run_tailoring publishes.
    state.try_transition(
        doc,
        doc.get(),
        "tailoring",
        lease=state.lease_for("tailoring", owner="w", now=now),
    )
    state.try_transition(
        doc,
        doc.get(),
        "ready_for_review",
        lease=state.CLEAR_LEASE,
        extra={reaper.ATTEMPTS_FIELD: firestore.DELETE_FIELD},
    )

    # A later regenerate leaves it queued and stale again — and the reaper is
    # willing to help rather than failing it on sight.
    state.try_transition(doc, doc.get(), "queued", note="regenerate")
    # Anchored to the document, not to NOW: state.timeline_event stamps the real
    # clock, so the regenerate entry above is "now" whatever the fixtures say.
    last = reaper.last_activity_at(doc.data)
    assert last is not None
    much_later = last + timedelta(seconds=LEASE * 2)
    assert reaper.classify(doc.data, now=much_later) == "redispatch"

    tally, dispatch = _reap([doc], now=much_later)
    assert tally["redispatch"] == 1
    assert dispatch.calls == [("u1", "job1")]


# --------------------------------------------------------------------------
# The per-pass budget.
# --------------------------------------------------------------------------


def test_the_pass_is_bounded_and_says_when_it_ran_out():
    """This runs in-request, serialised behind every other user's, inside a tick
    Cloud Scheduler gives ~180s. The first pass after deploy sees every document
    accumulated since the funnel existed go stale at once — unbounded, that
    overruns the deadline and the scheduler restarts a tick that finished
    nothing. Bounded, it drips.

    Truncation is reported because a pass that ran out of budget looks exactly
    like a pass with nothing to do."""
    docs = [_app("queued", timeline_age_s=LEASE * 2, doc_id=f"a{i}") for i in range(10)]

    tally, dispatch, db = _reap_limited(docs, limit=4, now=NOW)

    # Pushed to the query, not applied after reading the collection back.
    assert db.apps.limits == [5]
    assert tally["scanned"] == 4
    assert tally["truncated"] == 1
    assert len(dispatch.calls) == 4
    # The six it never reached are untouched, not failed.
    assert [d.updates for d in docs[4:]] == [[]] * 6


def test_the_document_past_the_budget_is_read_but_never_acted_on():
    """The limit is ``N + 1`` so "there is more" is read rather than inferred
    from ``scanned == N``, which cannot tell a full pass from an exactly full
    one. That extra document must not be recovered."""
    docs = [_app("queued", timeline_age_s=LEASE * 2, doc_id=f"a{i}") for i in range(5)]

    tally, dispatch, db = _reap_limited(docs, limit=4, now=NOW)

    assert db.apps.limits == [5]  # N + 1: the "there is more" probe
    assert tally["scanned"] == 4 and tally["truncated"] == 1
    assert docs[4].updates == []
    assert ("u1", "job1") in dispatch.calls and len(dispatch.calls) == 4


def test_an_exactly_full_pass_is_not_reported_as_truncated():
    """The case ``scanned == N`` alone would get wrong."""
    docs = [_app("queued", timeline_age_s=LEASE * 2, doc_id=f"a{i}") for i in range(4)]

    tally, _dispatch, _db = _reap_limited(docs, limit=4, now=NOW)

    assert tally["scanned"] == 4
    assert tally["truncated"] == 0


def test_the_default_budget_is_sized_against_the_scheduler_deadline():
    """A bound nobody can justify is a bound that gets raised until it is not
    one. ~200ms per recovered document, ~180s for the whole fan-out."""
    assert reaper.MAX_PER_PASS == 25
    assert reaper.MAX_PER_PASS * 0.2 < 10  # seconds, worst case, per user
