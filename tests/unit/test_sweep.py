# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the liveness-sweep helpers and discovery settings model.

Also covers ``sweep_postings``' application branch, which had no test at all —
and which is where a status write raced a live submission. See
``test_sweep_never_invalidates_an_application_that_started_submitting``.
"""

from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.transforms import ArrayUnion
from pydantic import ValidationError

import tools.ats.sweep as sweep
from models.job import Job
from models.settings import DiscoverySettings
from tools.applications import state
from tools.ats.sweep import BOARD_URLS, live_ids


def test_live_ids_greenhouse_and_ashby_wrap_jobs() -> None:
    data = {"jobs": [{"id": 123}, {"id": "abc"}, {"title": "no id"}]}
    assert live_ids("greenhouse", data) == {"123", "abc"}
    assert live_ids("ashby", data) == {"123", "abc"}


def test_live_ids_lever_bare_array() -> None:
    assert live_ids("lever", [{"id": "x1"}, {"id": "x2"}]) == {"x1", "x2"}


def test_live_ids_empty_or_missing() -> None:
    assert live_ids("greenhouse", {}) == set()
    assert live_ids("greenhouse", None) == set()
    assert live_ids("lever", None) == set()


def test_board_urls_cover_board_platforms() -> None:
    assert BOARD_URLS["greenhouse"]("acme").endswith("/acme/jobs")
    assert "acme?mode=json" in BOARD_URLS["lever"]("acme")
    assert BOARD_URLS["ashby"]("acme").endswith("/acme")


def test_discovery_settings_defaults_off() -> None:
    s = DiscoverySettings()
    assert s.auto_discovery is False
    assert s.liveness_sweep is False
    assert s.discovery_interval_hours == 24


def test_discovery_settings_rejects_arbitrary_interval() -> None:
    with pytest.raises(ValidationError):
        DiscoverySettings(discovery_interval_hours=5)


# --------------------------------------------------------------------------
# sweep_postings' application branch
#
# A minimal Firestore that honours update_time preconditions, so the sweep's
# write races the way the real one does. Only the surface sweep_postings
# actually touches is implemented.
# --------------------------------------------------------------------------


def _apply(target: dict, fields: dict) -> None:
    for key, value in fields.items():
        if value is firestore.DELETE_FIELD:
            target.pop(key, None)
        elif isinstance(value, ArrayUnion):
            target[key] = list(target.get(key) or []) + list(value.values)
        else:
            target[key] = value


class _Snap:
    def __init__(self, doc):
        self.id = doc.id
        self.reference = doc
        self.update_time = doc.version
        self.exists = doc.stored is not None
        self._data = None if doc.stored is None else dict(doc.stored)

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _Doc:
    def __init__(self, doc_id, stored=None):
        self.id = doc_id
        self.stored = None if stored is None else dict(stored)
        self.version = 1
        self.on_read = None  # hook: fires once, to interleave a concurrent write

    def get(self):
        snap = _Snap(self)
        if self.on_read is not None:
            hook, self.on_read = self.on_read, None
            hook()
        return snap

    def update(self, fields, option=None):
        if self.stored is None:
            raise NotFound("no such document")
        if option is not None and option._last_update_time != self.version:
            raise FailedPrecondition("stale last_update_time")
        _apply(self.stored, fields)
        self.version += 1

    def set(self, data, merge=False):  # pragma: no cover - sweep must not call it
        raise AssertionError("sweep must never set() an application")


class _Collection:
    def __init__(self, docs=None):
        self.docs = docs or {}

    def document(self, doc_id):
        return self.docs.setdefault(doc_id, _Doc(doc_id))

    def stream(self):
        return [_Snap(d) for d in self.docs.values()]


class _UserRef:
    def __init__(self, collections):
        self._collections = collections

    def collection(self, name):
        return self._collections[name]


class _DB:
    def __init__(self, user_ref):
        self._user_ref = user_ref

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, user_id):
        return self._user_ref


def _job_doc(job_id="j1") -> dict:
    return Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id="999",
        company="Acme",
        title="Staff Software Engineer",
        url="https://boards.greenhouse.io/acme/jobs/999",
        jd_raw="Build things.",
        discovered_at=datetime.now(UTC),
        user_decision="approved",
    ).model_dump(mode="json")


def _application(status: str) -> dict:
    return {
        "id": "app-j1",
        "user_id": "u1",
        "job_id": "j1",
        "status": status,
        "timeline": [{"at": "2026-08-01T00:00:00+00:00", "status": "tailoring"}],
    }


@pytest.fixture
def swept(monkeypatch):
    """sweep_postings over one approved job whose posting is gone."""

    def build(app_status: str):
        jobs = _Collection({"j1": _Doc("j1", _job_doc())})
        apps = _Collection({"app-j1": _Doc("app-j1", _application(app_status))})
        db = _DB(_UserRef({"jobs": jobs, "applications": apps}))
        monkeypatch.setattr(sweep.firestore, "Client", lambda: db)

        # Board fetch succeeds but lists no jobs → the posting is gone.
        async def fake_fetch(platform, slug, url):
            return {"jobs": []}

        monkeypatch.setattr(sweep, "fetch_board_json", fake_fetch)
        return jobs, apps

    return build


@pytest.mark.asyncio
async def test_sweep_invalidates_a_pre_submission_application(swept):
    jobs, apps = swept("ready_for_review")
    counts = await sweep.sweep_postings("u1")

    assert counts["removed"] == 1
    assert jobs.docs["j1"].stored["user_decision"] == "dismissed"
    app = apps.docs["app-j1"].stored
    assert app["status"] == "posting_removed"
    assert [e["status"] for e in app["timeline"]] == ["tailoring", "posting_removed"]
    assert "liveness sweep" in app["timeline"][-1]["note"]


@pytest.mark.asyncio
async def test_sweep_leaves_a_submitting_application_alone(swept):
    """The plain case: mid-submission documents are not the sweep's business."""
    jobs, apps = swept("submitting")
    await sweep.sweep_postings("u1")

    assert jobs.docs["j1"].stored["user_decision"] == "dismissed"  # job still dismissed
    assert apps.docs["app-j1"].stored["status"] == "submitting"


@pytest.mark.asyncio
async def test_sweep_never_invalidates_an_application_that_started_submitting(swept):
    """The race the allowed_from parameter exists for.

    The sweep reads ``ready_for_review`` and passes its allowlist. Between that
    read and its write, the user clicks Submit: the status becomes
    ``submitting`` and a live ATS submission starts. The sweep's write loses the
    precondition and retries — and ``submitting -> posting_removed`` *is* a
    legal edge, so a table-only re-check would let it through and mark the
    posting removed while a browser was mid-submit, discarding the confirmation
    evidence for an application the user really sent.
    """
    _jobs, apps = swept("ready_for_review")
    app_doc = apps.docs["app-j1"]

    def user_clicks_submit():
        assert state.try_transition(app_doc, _Snap(app_doc), "submitting") is True

    app_doc.on_read = user_clicks_submit  # interleaves on the sweep's read

    await sweep.sweep_postings("u1")

    assert app_doc.stored["status"] == "submitting"
    assert [e["status"] for e in app_doc.stored["timeline"]] == [
        "tailoring",
        "submitting",
    ]
    # The edge itself is legal — only the caller's allowlist stops it, and it
    # only stops it because the swap re-checks it on the retry.
    assert state.can_transition("submitting", "posting_removed")


@pytest.mark.asyncio
async def test_sweep_does_not_recreate_a_deleted_application(swept):
    _jobs, apps = swept("ready_for_review")
    apps.docs["app-j1"].stored = None
    await sweep.sweep_postings("u1")
    assert apps.docs["app-j1"].stored is None


def test_sweep_allowlist_stays_inside_the_transition_table() -> None:
    assert sweep.ACTIVE_APP_STATUSES == {
        "queued",
        "tailoring",
        "ready_for_review",
        "failed",
    }
    for status in sweep.ACTIVE_APP_STATUSES:
        assert state.can_transition(status, "posting_removed"), status
    assert "submitting" not in sweep.ACTIVE_APP_STATUSES
