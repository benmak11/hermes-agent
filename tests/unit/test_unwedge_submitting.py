# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The operator lever for applications stranded in ``submitting``.

``submitting`` is the one status with no automatic way out: everything else
correctly refuses to touch a document that might have a browser mid-submit, and
``run_submission``'s own ``except`` never fires if the process dies. The whole
safety of releasing one by hand rests on :func:`is_wedged` — releasing a
submission that is still running is how a user gets told "failed" about an
application that actually went out — so the age arithmetic is pinned here.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cli.unwedge_submitting import (
    DEFAULT_MIN_AGE_MINUTES,
    age_minutes,
    is_wedged,
    started_at,
)
from tools.applications import state

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _doc(status="submitting", *, minutes_ago=None, timeline_minutes_ago=None):
    doc: dict = {"status": status, "timeline": []}
    if minutes_ago is not None:
        doc["last_submitted_at"] = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    if timeline_minutes_ago is not None:
        doc["timeline"] = [
            {"at": (NOW - timedelta(minutes=t)).isoformat(), "status": "submitting"}
            for t in timeline_minutes_ago
        ]
    return doc


def test_default_age_is_the_state_machines_own_lease():
    """Not an independently chosen number — the two must not drift.

    31 minutes, not 20: the lease is now the 1800s dispatch deadline plus a
    minute of grace, because a lock that can expire while its work is still
    running is not a lock. This tool inherits that floor.
    """
    assert DEFAULT_MIN_AGE_MINUTES == state.IN_PROGRESS["submitting"] // 60 == 31


def test_a_live_lease_is_never_wedged_however_old_the_document_looks():
    """The lease is first-hand evidence from the process doing the work; the age
    is inference. Once submission goes through the queue, ``last_submitted_at``
    is stamped when the *request* claimed the status, which can be long before a
    worker picks the task up — so age alone would release a document whose
    browser is still on the first page of the form, write ``failed`` under it,
    and lose the confirmation when the real outcome is refused."""
    doc = _doc(minutes_ago=999)
    doc["lease"] = state.lease_for("submitting", owner="w1", now=NOW)
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is False

    # ...and it stops protecting the document the moment it lapses.
    expired = NOW + timedelta(seconds=state.IN_PROGRESS["submitting"] + 1)
    assert is_wedged(doc, now=expired, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is True


def test_a_document_with_no_lease_still_falls_back_to_the_age():
    """The in-process submission path writes no lease at all, and neither did
    anything before leases existed — absence must not read as 'alive'."""
    doc = _doc(minutes_ago=DEFAULT_MIN_AGE_MINUTES)
    assert "lease" not in doc
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is True


def test_started_at_prefers_last_submitted_at():
    doc = _doc(minutes_ago=30, timeline_minutes_ago=[90])
    assert started_at(doc) == NOW - timedelta(minutes=30)


def test_started_at_falls_back_to_the_newest_timeline_entry():
    doc = _doc(timeline_minutes_ago=[90, 45, 60])
    assert started_at(doc) == NOW - timedelta(minutes=45)


@pytest.mark.parametrize("value", [None, "", "not-a-date", 12345, {"at": "2026-01-01"}])
def test_started_at_survives_unusable_timestamps(value):
    assert started_at({"last_submitted_at": value, "timeline": []}) is None


def test_naive_timestamps_are_read_as_utc():
    doc = {"last_submitted_at": "2026-08-26T11:00:00", "timeline": []}
    assert age_minutes(doc, now=NOW) == 60


@pytest.mark.parametrize(
    "status", ["ready_for_review", "submitted", "failed", "queued"]
)
def test_only_submitting_is_ever_wedged(status):
    """The tool must not touch a document that has a normal way forward."""
    doc = _doc(status, minutes_ago=999)
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is False


def test_a_submission_inside_the_lease_is_left_alone():
    """The dangerous direction: this one may still have a browser running."""
    doc = _doc(minutes_ago=DEFAULT_MIN_AGE_MINUTES - 1)
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is False


def test_a_submission_past_the_lease_is_wedged():
    doc = _doc(minutes_ago=DEFAULT_MIN_AGE_MINUTES)
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is True


def test_a_document_with_no_usable_timestamp_is_wedged():
    """No last_submitted_at and no timeline means it predates both, which means
    no submission of its is still running."""
    doc = _doc()
    assert age_minutes(doc, now=NOW) is None
    assert is_wedged(doc, now=NOW, min_age_minutes=DEFAULT_MIN_AGE_MINUTES) is True


def test_the_release_edge_exists_and_is_not_a_retry():
    """submitting → failed is the only edge this tool uses. It must not be able
    to put an application back into a state that resubmits by itself."""
    assert state.can_transition("submitting", "failed")
    assert not state.can_transition("failed", "submitted")
    # Recovery from failed is a deliberate user action (Submit / regenerate).
    assert state.TRANSITIONS["failed"] == frozenset(
        {"submitting", "queued", "posting_removed"}
    )


def test_the_note_says_the_outcome_is_unknown():
    from cli.unwedge_submitting import NOTE

    assert "UNKNOWN" in NOTE
    assert "email" in NOTE.lower()
