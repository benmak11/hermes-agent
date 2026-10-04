# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The impression log (``users/{uid}/exposures``) and its join to a decision.

A decision event says what was chosen; only an exposure says what it was
chosen *against* — the position in the list and the jobs that were on screen
and ignored. Without it the exploration sample is uninterpretable, because the
queue sorts sampled jobs below every normally-surfaced one.

What is pinned hardest is what could be silently wrong:

- ``rank`` must be the position in the list **actually returned**. The
  streaming loop's index is a Firestore ordering of the unfiltered collection;
  it survives every obvious test and is not what anybody saw;
- ``exploration`` is popped out of the response, so recording a constant
  ``False`` here looks like working code and destroys the one field the sample
  needs;
- ``request_id`` must be the id the middleware bound. A fresh uuid is valid and
  joins to nothing;
- the decision-side lookup is wrapped: a transient Firestore error on it must
  not fail the decision.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, ServiceUnavailable
from google.cloud import firestore

import api.routes.jobs as jobs_mod
from api.app_utils.middleware import RequestContextMiddleware
from api.deps import verify_user
from obs.logging import current_request_id
from tools import decisions, exposures

# --- fakes ------------------------------------------------------------------


class _Snap:
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self.exists = data is not None
        self._data = None if data is None else dict(data)

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _Doc:
    def __init__(self, doc_id: str, data: dict | None = None):
        self.id = doc_id
        self.data = None if data is None else dict(data)

    def get(self):
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


class _JobsQuery:
    """The pending-jobs query: ``where`` then ``stream``."""

    def __init__(self, docs):
        self._docs = docs

    def where(self, *, filter=None):
        return self

    def stream(self):
        return iter(self._docs)


class _Exposures:
    """The exposure sink. ``add`` is the real client's auto-id write, and
    ``order_by/limit/stream`` is the lookup the decision path takes."""

    def __init__(self, *, fail_write=False, fail_read=False, docs=None):
        self.written: list[dict] = []
        self.order_by_calls: list[tuple] = []
        self._fail_write = fail_write
        self._fail_read = fail_read
        self._docs = list(docs or [])

    def add(self, data: dict):
        if self._fail_write:
            raise ServiceUnavailable("firestore is having a day")
        self.written.append(json.loads(json.dumps(data)))
        return (None, _Doc(f"exp{len(self.written)}", data))

    def order_by(self, field, direction=None):
        self.order_by_calls.append((field, direction))
        self._ordered = sorted(
            self._docs,
            key=lambda s: s.to_dict()["shown_at"],
            reverse=direction == firestore.Query.DESCENDING,
        )
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        if self._fail_read:
            raise ServiceUnavailable("read retry budget exhausted")
        return iter(self._ordered[: self._limit])


class _User:
    def __init__(self, job_docs, exposure_sink, jobs_by_id=None):
        self._job_docs = job_docs
        self._jobs_by_id = jobs_by_id or {}
        self._apps: dict[str, _Doc] = {}
        self.exposures = exposure_sink
        self.events: list[dict] = []

    def collection(self, name: str):
        if name == "jobs":
            return _JobsCollection(self._job_docs, self._jobs_by_id)
        if name == exposures.COLLECTION:
            return self.exposures
        if name == decisions.COLLECTION:
            return _Events(self.events)
        if name == "applications":
            return _JobsCollection([], self._apps)
        raise AssertionError(f"unexpected collection {name!r}")


class _JobsCollection(_JobsQuery):
    def __init__(self, docs, by_id):
        super().__init__(docs)
        self._by_id = by_id

    def document(self, doc_id):
        return self._by_id.setdefault(doc_id, _Doc(doc_id))


class _Events:
    def __init__(self, events):
        self.events = events

    def add(self, data: dict):
        self.events.append(dict(data))
        return (None, _Doc(f"evt{len(self.events)}", data))


class _DB:
    def __init__(self, user):
        self._user = user

    def collection(self, name):
        assert name == "users", name
        return _JobsCollection([], {"u1": self._user})


# --- fixtures ---------------------------------------------------------------


def _snap(doc_id: str, score_value: float, **extra) -> _Snap:
    return _Snap(
        doc_id,
        {
            "user_decision": "pending",
            "company": "Acme",
            "match": {"overall_score": score_value},
            **extra,
        },
    )


@pytest.fixture
def world(monkeypatch):
    """A client over the fakes, with the middleware mounted so ``request_id``
    is bound exactly as it is in production, and the scheduler tick stubbed so
    no test can reach discovery."""
    monkeypatch.setattr(jobs_mod, "tick_user", lambda uid: None)
    monkeypatch.setattr(jobs_mod, "dispatch_tailor", lambda *a, **k: True)
    monkeypatch.setattr(jobs_mod, "check_posting", lambda *a, **k: True)

    def build(job_snaps=(), *, exposure_sink=None, jobs_by_id=None):
        sink = exposure_sink if exposure_sink is not None else _Exposures()
        user = _User(list(job_snaps), sink, jobs_by_id)
        monkeypatch.setattr(jobs_mod, "_client", lambda: _DB(user))
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)
        app.include_router(jobs_mod.router)
        app.dependency_overrides[verify_user] = lambda: "u1"
        return TestClient(app), user

    return build


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("LOG_EXPOSURES", raising=False)


def _on(monkeypatch):
    monkeypatch.setenv("LOG_EXPOSURES", "1")


# --- the flag ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on"])
def test_the_flag_is_off_by_default_and_opt_in(monkeypatch, value):
    assert exposures.enabled() is False
    monkeypatch.setenv("LOG_EXPOSURES", value)
    assert exposures.enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "", "yes", "2"])
def test_anything_unrecognised_leaves_the_flag_off(monkeypatch, value):
    """Shipped inert: the failure mode of a typo must be "no logging", never
    "logging on" — the same reading ``GEO_GATE_ENFORCE`` gets."""
    monkeypatch.setenv("LOG_EXPOSURES", value)
    assert exposures.enabled() is False


def test_flag_off_writes_nothing_and_returns_the_same_body(world):
    """Two assertions on purpose, and the first is the one that matters: the
    body is pinned *whole* against the literal it had before this feature
    existed, so a key added to the response — in either branch — fails here.
    Comparing a flag-on run against a flag-off run would cancel exactly that.
    """
    client, user = world([_snap("low", 30), _snap("high", 90), _snap("mid", 70)])

    body = client.get("/jobs/pending").json()

    assert body == {
        "jobs": [
            {
                "id": "high",
                "user_decision": "pending",
                "company": "Acme",
                "match": {"overall_score": 90},
            },
            {
                "id": "mid",
                "user_decision": "pending",
                "company": "Acme",
                "match": {"overall_score": 70},
            },
        ],
        "pending_total": 3,
        "scored_total": 3,
    }
    assert user.exposures.written == []


def test_flag_on_does_not_change_the_response_either(world, monkeypatch):
    """The other half of the inertness claim: turning it on adds a document,
    not a field. Pinned against the same literal as the flag-off case."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90), _snap("hidden", 35, exploration=True)])

    body = client.get("/jobs/pending").json()

    assert body == {
        "jobs": [
            {
                "id": "high",
                "user_decision": "pending",
                "company": "Acme",
                "match": {"overall_score": 90},
            },
            {
                "id": "hidden",
                "user_decision": "pending",
                "company": "Acme",
                "match": {"overall_score": 35},
            },
        ],
        "pending_total": 2,
        "scored_total": 2,
    }
    assert "exploration" not in json.dumps(body)
    assert len(user.exposures.written) == 1


# --- one document per page load, in the returned order ----------------------


def test_one_page_load_writes_one_document_with_items_in_returned_order(
    world, monkeypatch
):
    """The documents stream in an order nobody saw; the response is sorted by
    score descending. The record has to follow the response."""
    _on(monkeypatch)
    client, user = world([_snap("mid", 70), _snap("top", 95), _snap("high", 90)])

    body = client.get("/jobs/pending").json()

    assert len(user.exposures.written) == 1
    doc = user.exposures.written[0]
    assert [i["job_id"] for i in doc["items"]] == ["top", "high", "mid"]
    assert [i["job_id"] for i in doc["items"]] == [j["id"] for j in body["jobs"]]
    assert set(doc) == {"shown_at", "request_id", "min_score", "items"}
    assert doc["shown_at"].endswith("+00:00")


def test_a_repeat_page_load_writes_a_second_document(world, monkeypatch):
    """An exposure is an impression, not a session. The same unchanged list
    shown twice is two impressions, and collapsing them would under-count
    every ignore."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90)])

    client.get("/jobs/pending")
    client.get("/jobs/pending")

    assert len(user.exposures.written) == 2


def test_rank_is_the_zero_based_position_in_the_returned_list(world, monkeypatch):
    """Stamped over the response list, after the sort. Built from the
    streaming loop's index instead, every rank here is wrong while the list,
    the count and the ids all still look right.

    The documents are streamed in an order that is *not* the response order and
    includes rows the response drops, so a pre-filter index cannot coincide
    with the right answer.
    """
    _on(monkeypatch)
    client, user = world(
        [
            _snap("mid", 70),
            _Snap("unscored", {"user_decision": "pending"}),
            _snap("buried", 25),
            _snap("top", 95),
            _snap("high", 90),
        ]
    )

    body = client.get("/jobs/pending").json()

    items = user.exposures.written[0]["items"]
    assert [(i["rank"], i["job_id"]) for i in items] == [
        (0, "top"),
        (1, "high"),
        (2, "mid"),
    ]
    for rank, job in enumerate(body["jobs"]):
        assert items[rank]["job_id"] == job["id"]
        assert items[rank]["rank"] == rank
        assert items[rank]["overall_score"] == job["match"]["overall_score"]


def test_exploration_is_recorded_even_though_it_is_stripped_from_the_response(
    world, monkeypatch
):
    """The field the sample lives or dies on. It is popped out of the response
    deliberately, so recording a constant ``False`` here is invisible from the
    API and makes the exposure useless for the one analysis it exists for.

    Both values are asserted: all-``False`` and all-``True`` both pass a test
    that only ever shows it one sampled job.
    """
    _on(monkeypatch)
    client, user = world(
        [
            _snap("high", 90),
            _snap("hidden", 35, exploration=True),
            _snap("alsohidden", 30, exploration=True),
        ]
    )

    body = client.get("/jobs/pending").json()

    assert "exploration" not in json.dumps(body)
    items = user.exposures.written[0]["items"]
    assert {i["job_id"]: i["exploration"] for i in items} == {
        "high": False,
        "hidden": True,
        "alsohidden": True,
    }


def test_a_sampled_job_that_later_scored_above_the_bar_keeps_its_flag(
    world, monkeypatch
):
    """``exploration`` records how the job was *selected*, not where it landed.
    A job sampled into the band that then scored 90 would have been surfaced
    anyway — and the analysis has to be able to tell that case apart, so the
    flag is reported as written, not re-derived from the score."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90, exploration=True)])

    client.get("/jobs/pending")

    items = user.exposures.written[0]["items"]
    assert items == [
        {"job_id": "high", "rank": 0, "overall_score": 90, "exploration": True}
    ]


def test_an_empty_queue_still_writes_an_impression(world, monkeypatch):
    """Opening an empty review queue is a real impression of nothing, and the
    row is what distinguishes "never looked" from "looked, had nothing"."""
    _on(monkeypatch)
    client, user = world([])

    client.get("/jobs/pending")

    assert user.exposures.written[0]["items"] == []


def test_the_threshold_that_defined_the_choice_set_is_recorded(world, monkeypatch):
    """``min_score`` is a request parameter, and it is not reconstructible from
    ``items``: the lowest score shown is only a lower bound on it, and an empty
    list says nothing at all. Without it, "this job was not shown because the
    scorer rated it low" and "because the slider was at 90" are the same row —
    and they are opposite conclusions about the scorer.

    Asserted at two different values, including one that changes which jobs are
    in the list, so hardcoding the route's default passes neither.
    """
    _on(monkeypatch)
    client, user = world([_snap("high", 90), _snap("mid", 70)])

    client.get("/jobs/pending")
    client.get("/jobs/pending?min_score=80")

    assert [d["min_score"] for d in user.exposures.written] == [60, 80]
    assert [[i["job_id"] for i in d["items"]] for d in user.exposures.written] == [
        ["high", "mid"],
        ["high"],
    ]


# --- the request id ---------------------------------------------------------


def test_the_exposure_carries_the_request_id_the_middleware_bound(world, monkeypatch):
    """A minted id is a valid id that joins to nothing. The middleware honours
    an inbound ``x-request-id`` and echoes it as ``X-Request-Id``; the exposure
    has to match the echo, or the row cannot be tied to the server logs — or
    the client's own record — for the request that produced it."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90)])

    resp = client.get("/jobs/pending", headers={"x-request-id": "rid-from-client"})

    assert resp.headers["X-Request-Id"] == "rid-from-client"
    assert user.exposures.written[0]["request_id"] == "rid-from-client"


def test_the_query_param_id_is_honoured_too(world, monkeypatch):
    """``?request_id=`` is the SSE path's way in (EventSource cannot set
    headers) and the middleware accepts it, so the exposure must as well."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90)])

    resp = client.get("/jobs/pending?request_id=rid-from-query")

    assert resp.headers["X-Request-Id"] == "rid-from-query"
    assert user.exposures.written[0]["request_id"] == "rid-from-query"


def test_with_no_inbound_id_the_exposure_matches_the_echoed_one(world, monkeypatch):
    """The unforced case: the middleware mints one, and a second mint in the
    route would still produce a plausible hex string in the document that
    matches no log line. Pinned against the echoed header."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90)])

    resp = client.get("/jobs/pending")

    assert user.exposures.written[0]["request_id"] == resp.headers["X-Request-Id"]


def test_the_helper_falls_back_to_the_bound_context():
    """``log_exposure`` is also callable without an explicit id — then it reads
    the same contextvar rather than minting."""
    sink = _Exposures()
    user = _User([], sink)

    assert current_request_id() is None
    exposures.log_exposure(user, items=[], min_score=60)

    assert sink.written[0]["request_id"] is None


# --- failure never costs the request ----------------------------------------


def test_a_failing_exposure_write_does_not_fail_the_request(world, monkeypatch):
    """A lost impression is an inconvenience; a 500 on the main screen is a
    broken product. The write runs as a background task, which ``TestClient``
    executes for real — so an unswallowed exception surfaces here."""
    _on(monkeypatch)
    client, user = world([_snap("high", 90)], exposure_sink=_Exposures(fail_write=True))

    resp = client.get("/jobs/pending")

    assert resp.status_code == 200
    assert [j["id"] for j in resp.json()["jobs"]] == ["high"]
    assert user.exposures.written == []


# --- the join to a decision -------------------------------------------------


def _decided_world(world, shown_ats, *, shown_ids=("job1",), **kw):
    """Exposures whose ``items`` hold ``shown_ids`` — the default contains the
    job every test here decides, so a test that cares about the *absence* of
    the job from the list says so explicitly."""
    sink = _Exposures(
        docs=[
            _Snap(
                f"e{n}",
                {
                    "shown_at": at,
                    "items": [
                        {"job_id": j, "rank": r} for r, j in enumerate(shown_ids)
                    ],
                },
            )
            for n, at in enumerate(shown_ats)
        ],
        **kw,
    )
    job = _Doc("job1", {"user_decision": "pending", "match": {"overall_score": 78.5}})
    client, user = world(exposure_sink=sink, jobs_by_id={"job1": job})
    return client, user


def test_a_decision_after_an_exposure_carries_the_newest_shown_at(world, monkeypatch):
    """State (b): an exposure exists and contains the decided job, so that
    exposure's ``shown_at`` is the answer.

    The most recent impression is the one the decision answered. Read the
    oldest — an ``order_by`` without ``DESCENDING``, or no ``order_by`` at all
    on a collection of random auto-ids — and every label is joined to the
    first list the user ever saw."""
    _on(monkeypatch)
    client, user = _decided_world(
        world,
        [
            "2026-10-01T00:00:00+00:00",
            "2026-10-03T09:00:00+00:00",
            "2026-10-02T00:00:00+00:00",
        ],
    )

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert user.events[0]["shown_at"] == "2026-10-03T09:00:00+00:00"
    assert user.exposures.order_by_calls == [("shown_at", firestore.Query.DESCENDING)]


def test_a_decision_with_no_exposure_gets_no_fabricated_shown_at(world, monkeypatch):
    """State (a): the collection is empty — the flag was never on. The key is
    present and ``None``, following ``score_snapshot``: absence must not be
    mistakable for a measurement. Defaulting to ``now`` would make every
    unexposed decision look like a zero-second decision on a list that was
    never shown."""
    _on(monkeypatch)
    client, user = _decided_world(world, [])

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert "shown_at" in user.events[0]
    assert user.events[0]["shown_at"] is None


def test_a_shelf_decision_is_not_stamped_with_someone_elses_list(world, monkeypatch):
    """State (c), and the one an empty collection cannot reach: an exposure
    **exists** and does not contain the decided job.

    ``/jobs/{id}/decide`` is also what the starred and skipped shelves POST
    (``web/src/app/app/tracking/page.tsx``), and that page never fetches
    ``/jobs/pending``, so it records no exposure of its own. Without the
    membership test, restoring job1 at 14:00 after a 09:00 review of
    ``[other1, other2]`` stamps job1's event with 09:00 — a list that never
    contained it, and a five-hour time-to-decide that no later query could
    tell from a real one.

    The exposure is deliberately *readable and recent*, so only the membership
    check can produce the ``None``: the read succeeded, the ordering worked,
    and the answer is still "we do not know when this was shown".
    """
    _on(monkeypatch)
    client, user = _decided_world(
        world,
        ["2026-10-03T09:00:00+00:00"],
        shown_ids=("other1", "other2"),
    )

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert user.events[0]["shown_at"] is None
    # The lookup really did run and really did read that document — this is a
    # filtered-out match, not a skipped or failed read.
    assert user.exposures.order_by_calls == [("shown_at", firestore.Query.DESCENDING)]


def test_only_the_latest_exposure_counts_even_if_an_older_one_matches(
    world, monkeypatch
):
    """The question is "was this card in the list the user just acted from",
    so a match in an *older* list is a different impression and must not be
    stamped. Searching back for the newest exposure containing the job would
    pass the test above and silently reintroduce the stale stamp."""
    _on(monkeypatch)
    client, user = _decided_world(world, [], shown_ids=())
    user.exposures._docs = [
        _Snap(
            "old",
            {
                "shown_at": "2026-10-01T09:00:00+00:00",
                "items": [{"job_id": "job1", "rank": 0}],
            },
        ),
        _Snap(
            "new",
            {
                "shown_at": "2026-10-03T09:00:00+00:00",
                "items": [{"job_id": "other", "rank": 0}],
            },
        ),
    ]

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert user.events[0]["shown_at"] is None


def test_with_the_flag_off_the_decision_takes_no_exposure_read(world, monkeypatch):
    """Off means off including the reads. The event still carries the key, as
    ``None`` — a decision made while nothing was recording impressions has no
    impression, and saying so is the honest answer."""
    client, user = _decided_world(world, ["2026-10-03T09:00:00+00:00"])

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert user.events[0]["shown_at"] is None
    assert user.exposures.order_by_calls == []


def test_a_failing_exposure_lookup_does_not_fail_the_decision(world, monkeypatch):
    """An unwrapped read here turns a transient Firestore error into a failed
    approval with no tailoring dispatched. Degraded — an event without
    ``shown_at`` — never absent."""
    _on(monkeypatch)
    client, user = _decided_world(world, ["2026-10-03T09:00:00+00:00"], fail_read=True)

    resp = client.post("/jobs/job1/decide", json={"decision": "approved"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(user.events) == 1
    assert user.events[0]["shown_at"] is None
    # the decision itself still landed, and tailoring still got its document
    assert user._jobs_by_id["job1"].data["user_decision"] == "approved"


def test_the_lookup_itself_swallows_and_returns_none():
    """The unit-level statement of the same contract, so the swallow cannot be
    deleted and left passing by the route's own try block."""
    docs = [_Snap("e", {"shown_at": "x", "items": [{"job_id": "job1"}]})]
    user = _User([], _Exposures(docs=docs, fail_read=True))

    assert exposures.latest_shown_at(user, "job1") is None


def test_a_malformed_items_list_does_not_fail_the_lookup():
    """The membership test is inside the same ``try`` as the read, so a
    document written by an older or buggier version — ``items`` missing, or
    holding something that is not a list of dicts — costs the event a field
    rather than 500ing a decision."""
    for broken in ({"shown_at": "x"}, {"shown_at": "x", "items": "nope"}):
        user = _User([], _Exposures(docs=[_Snap("e", broken)]))

        assert exposures.latest_shown_at(user, "job1") is None


def test_a_system_dismissal_records_no_shown_at(world, monkeypatch):
    """``actor: "system"`` answers no impression. A sweep dismissing a dead
    posting is not a judgement about fit, and stamping it with the user's last
    page load would invent a human looking at a list."""
    _on(monkeypatch)
    sink = _Exposures(docs=[_Snap("e", {"shown_at": "2026-10-03T09:00:00+00:00"})])
    user = _User([], sink)

    decisions.log_decision(
        user,
        job_id="job1",
        decision="dismissed",
        previous_decision="rejected",
        job_doc=None,
        actor="system",
    )

    assert user.events[0]["shown_at"] is None
    assert sink.order_by_calls == []
