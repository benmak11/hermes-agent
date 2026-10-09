# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``GET /activity`` — the liveness contract.

The bug this endpoint exists to stop has shipped three times: a UI claiming work
is happening when nothing is. So the tests here are mostly about the *negative*
cases — the states where the honest answer is "nothing is running, and nothing
will" — plus the two properties that make the contract safe to poll:

- ``done``/``total`` are both-or-neither, because that pair is the only licence
  the client has to draw a progress bar;
- the route schedules no background work, because the two endpoints the app
  already polls (``GET /jobs/pending``, ``GET /settings/discovery``) both tick,
  and a tick under QUEUE_MODE can reach a paid Vertex batch.

The fake Firestore below **honours its filters**. A fake that drops them answers
a different question than the one asked, and this suite has shipped that twice.
``test_the_fake_firestore_actually_filters`` pins it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.activity as activity
from api.deps import verify_user
from tools.discovery.budget import week_key as _dw_key
from tools.matching import batch_runs
from tools.matching import budget as matching_budget

NOW = datetime(2026, 9, 27, 12, 30, 0, tzinfo=UTC)
NEXT_HOUR = "2026-09-27T13:00:00+00:00"


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


# --------------------------------------------------------------------------
# A fake Firestore that keeps its filters
# --------------------------------------------------------------------------


class _Snap:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Query:
    """Streams docs, honouring every ``==`` / ``in`` filter it was handed.

    The filters are **kept and applied**, not stored and ignored: ``/activity``
    asks two different questions of ``batch_runs`` (which user, which state) and
    a fake that answers "all of them" would let a ``failed`` run — the exact doc
    that must never read as running — pass the test.
    """

    def __init__(self, docs: dict[str, dict], filters=()):
        self._docs = docs
        self._filters = tuple(filters)

    def where(self, filter=None):
        return _Query(self._docs, self._filters + ((filter,) if filter else ()))

    def _matches(self, data: dict) -> bool:
        for f in self._filters:
            value = data.get(f.field_path)
            if f.op_string == "in":
                if value not in f.value:
                    return False
            elif value != f.value:
                return False
        return True

    async def stream(self):
        for doc_id, data in self._docs.items():
            if self._matches(data):
                yield _Snap(doc_id, data)


class _Collection(_Query):
    def document(self, doc_id: str):
        return _Doc(self._docs, doc_id)


class _Doc:
    def __init__(self, docs: dict[str, dict], doc_id: str):
        self._docs = docs
        self.id = doc_id

    async def get(self):
        return _Snap(self.id, self._docs.get(self.id) or {})

    def set(self, data: dict, merge: bool = False) -> None:
        """Writes really land in the store.

        ``/activity`` must write nothing, and a fake that quietly swallowed a
        ``set`` would make ``test_allowance_never_debits_anything`` assert
        nothing at all — the second-commonest way a test in this repo has
        turned out to be unfalsifiable. ``test_the_fake_firestore_actually_
        writes`` pins it.
        """
        current = dict(self._docs.get(self.id) or {}) if merge else {}
        current.update(data)
        self._docs[self.id] = current

    def collection(self, name: str):
        sub = self._docs.setdefault(f"__sub__{self.id}", {})
        return _Collection(sub.setdefault(name, {}))


class _DB:
    """``users/{uid}`` + its subcollections, and the top-level ``batch_runs``."""

    def __init__(
        self,
        *,
        user: dict | None = None,
        runs: dict | None = None,
        applications: dict | None = None,
        batch: dict | None = None,
        uid: str = "u1",
    ):
        self.users = {uid: user or {}}
        self.users[f"__sub__{uid}"] = {
            "runs": runs or {},
            "applications": applications or {},
        }
        self.batch = batch or {}

    def collection(self, name: str):
        if name == "users":
            return _Collection(self.users)
        if name == batch_runs.COLLECTION:
            return _Collection(self.batch)
        raise AssertionError(f"/activity read an unexpected collection: {name}")


@pytest.fixture
def client(monkeypatch):
    """A ``TestClient`` over just the activity router, on a fake Firestore."""

    def build(**db_kwargs):
        db = _DB(**db_kwargs)
        monkeypatch.setattr(activity, "_client", lambda: db)
        monkeypatch.setattr(activity, "_now", lambda: NOW)
        app = FastAPI()
        app.include_router(activity.router)
        app.dependency_overrides[verify_user] = lambda: "u1"
        return TestClient(app), db

    return build


def _items(body: dict) -> dict[str, dict]:
    return {item["kind"]: item for item in body["items"]}


# --------------------------------------------------------------------------
# The fake's own guard
# --------------------------------------------------------------------------


def test_the_fake_firestore_actually_filters(client):
    """If this fails, nothing else in this file is evidence of anything."""
    cl, _ = client(
        batch={
            "mine-running": {"user_id": "u1", "state": "running", "job_name": "b/1"},
            "mine-done": {"user_id": "u1", "state": "done"},
            "someone-elses": {"user_id": "u2", "state": "running", "job_name": "b/2"},
        }
    )
    body = cl.get("/activity").json()
    # Only the one doc matching BOTH filters reached the route.
    assert _items(body)["batch_scoring"]["ref"] == {"batch_run": "mine-running"}


# --------------------------------------------------------------------------
# The state the product keeps getting wrong
# --------------------------------------------------------------------------


def test_the_fake_firestore_actually_writes(client):
    """The other half of the fake's guard: a ``set`` is not a no-op."""
    _cl, db = client(user={"a": 1})
    db.collection("users").document("u1").set({"b": 2}, merge=True)
    assert db.users["u1"] == {"a": 1, "b": 2}
    db.collection("users").document("u1").set({"c": 3})
    assert db.users["u1"] == {"c": 3}


def test_an_unscored_backlog_with_auto_discovery_off_is_idle_unscheduled(client):
    """Jobs are waiting and **nothing will score them**. Not "queued", not
    "running", and nothing that reads as in progress."""
    cl, _ = client(
        user={
            "discovery_settings": {"auto_discovery": False, "liveness_sweep": False},
            "discovery_state": {
                "last_discovery_at": _iso(600),
                "last_discovery": {"unscored_backlog": 412, "scored": 0},
            },
        }
    )
    body = cl.get("/activity").json()
    scoring = _items(body)["scoring"]

    assert scoring["state"] == "idle_unscheduled"
    assert scoring["detail"]["unscored_backlog"] == 412
    # Nothing is coming, so there is no next time to advertise and no bar.
    assert scoring["next_at"] is None
    assert scoring["done"] is None and scoring["total"] is None
    # And the discovery loop itself says the same about its own toggle.
    assert _items(body)["discovery"]["state"] == "idle_unscheduled"
    assert _items(body)["discovery"]["next_at"] is None


def test_the_same_backlog_under_auto_discovery_is_idle_scheduled(client):
    """The one thing that makes a waiting backlog honest: a tick that will
    actually come for it."""
    cl, _ = client(
        user={
            "discovery_settings": {"auto_discovery": True},
            "discovery_state": {
                "last_discovery_at": _iso(600),
                "last_discovery": {"unscored_backlog": 412},
            },
        }
    )
    scoring = _items(cl.get("/activity").json())["scoring"]
    assert scoring["state"] == "idle_scheduled"
    assert scoring["next_at"] is not None


def _trial_user(started_days_ago: float, tier: str = "trial") -> dict:
    return {
        "plan": {
            "tier": tier,
            "trial_started_at": (NOW - timedelta(days=started_days_ago)).isoformat(),
        },
        "discovery_settings": {"auto_discovery": True, "liveness_sweep": True},
        "discovery_state": {
            "last_discovery_at": _iso(600),
            "last_discovery": {"unscored_backlog": 412},
            "last_sweep_at": _iso(600),
        },
    }


def test_a_trial_past_its_first_week_advertises_no_scheduled_run(client):
    """The tick runs nothing for a trial after day 7, whatever the toggles
    say, so the panel must not promise a next search, sweep or scoring."""
    cl, _ = client(user=_trial_user(8))
    items = _items(cl.get("/activity").json())

    for kind in ("discovery", "sweep", "scoring"):
        assert items[kind]["state"] == "idle_unscheduled", kind
        assert items[kind]["next_at"] is None, kind


@pytest.mark.parametrize("days, tier", [(3, "trial"), (30, "paid")])
def test_a_trial_in_its_first_week_or_a_paid_plan_stays_scheduled(client, days, tier):
    cl, _ = client(user=_trial_user(days, tier))
    items = _items(cl.get("/activity").json())

    for kind in ("discovery", "sweep", "scoring"):
        assert items[kind]["state"] == "idle_scheduled", kind


def test_the_ratings_allowance_follows_the_plan(client):
    """3 a day on a trial, 10 on paid, read off the document already fetched."""
    cl, _ = client(user={"plan": {"tier": "paid"}})
    assert cl.get("/activity").json()["allowance"]["ratings"]["limit"] == 10
    cl, _ = client(user={})
    assert cl.get("/activity").json()["allowance"]["ratings"]["limit"] == 3


# --------------------------------------------------------------------------
# waiting_external — the batch is with Google and we are doing nothing
# --------------------------------------------------------------------------


def test_a_submitted_parse_batch_is_waiting_external_with_no_progress(client):
    cl, _ = client(
        batch={
            "20260927-a": {
                "user_id": "u1",
                "state": "running",
                "stage": "parse",
                "job_name": "projects/p/batches/1",
                "claimed_at": None,
                "updated_at": _iso(90),
                "job_ids": ["j1", "j2", "j3"],
                "counts": {"scored": 0, "discarded": 0, "failed": 0},
                "committed": {"parse": {"usd_low": 0.11, "usd_high": 0.22}},
            }
        }
    )
    body = cl.get("/activity").json()
    batch = _items(body)["batch_scoring"]

    assert batch["state"] == "waiting_external"
    # The only real next event is the hourly resume tick.
    assert batch["next_at"] == NEXT_HOUR
    assert body["next_tick_at"] == NEXT_HOUR
    # **Never a percent.** Nothing of ours is working, and no parse-leg ingest
    # has ever been observed, so there is no duration to quote either.
    assert batch["done"] is None and batch["total"] is None
    assert batch["since"] == _iso(90)
    assert batch["detail"]["committed_usd_high"] == 0.22
    # The money is owed whether or not anything is running.
    assert body["committed"] == {"usd_low": 0.11, "usd_high": 0.22, "runs": 1}


def test_a_claimed_score_leg_is_running_and_is_the_only_source_of_a_bar(client):
    cl, _ = client(
        batch={
            "20260927-a": {
                "user_id": "u1",
                "state": "running",
                "stage": "score",
                "job_name": "projects/p/batches/2",
                "claimed_at": _iso(3),
                "job_ids": ["j1", "j2", "j3", "j4"],
                "counts": {"scored": 1, "discarded": 1},
            }
        }
    )
    batch = _items(cl.get("/activity").json())["batch_scoring"]
    assert batch["state"] == "running"
    assert (batch["done"], batch["total"]) == (2, 4)


def test_a_score_leg_with_no_job_ids_draws_no_bar(client):
    """A live batch_runs doc from before ``job_ids`` existed. Numerator without
    a denominator is a fabricated percentage, so neither is sent."""
    cl, _ = client(
        batch={
            "legacy": {
                "user_id": "u1",
                "state": "running",
                "stage": "score",
                "job_name": "projects/p/batches/3",
                "claimed_at": _iso(3),
                "counts": {"scored": 7},
            }
        }
    )
    batch = _items(cl.get("/activity").json())["batch_scoring"]
    assert batch["state"] == "running"
    assert batch["done"] is None and batch["total"] is None


def test_a_batch_claim_older_than_the_ingest_ttl_is_stalled(client):
    cl, _ = client(
        batch={
            "20260927-a": {
                "user_id": "u1",
                "state": "running",
                "stage": "score",
                "job_name": "projects/p/batches/4",
                "claimed_at": _iso(batch_runs._CLAIM_TTL_SECONDS / 60 + 5),
                "job_ids": ["j1"],
                "counts": {"scored": 0},
            }
        }
    )
    batch = _items(cl.get("/activity").json())["batch_scoring"]
    assert batch["state"] == "stalled"
    assert batch["done"] is None and batch["total"] is None


def test_a_failed_batch_run_is_never_liveness_only_unpriced_money(client):
    """It may have been billed and will never be ingested. That is a number,
    not an activity — and not ``committed``, which an ingest will still price.
    """
    cl, _ = client(
        user={"discovery_settings": {"auto_discovery": False}},
        batch={
            "dead": {
                "user_id": "u1",
                "state": "failed",
                "stage": "score",
                "job_name": "projects/p/batches/5",
                "claimed_at": _iso(5),
                "job_ids": ["j1", "j2"],
                "counts": {"scored": 0},
                "committed": {"score": {"usd_low": 1.5, "usd_high": 3.0}},
                "error": "JOB_STATE_FAILED: quota",
            }
        },
    )
    body = cl.get("/activity").json()
    batch = _items(body)["batch_scoring"]

    assert batch["state"] not in ("running", "waiting_external", "queued")
    assert batch["done"] is None and batch["total"] is None
    assert body["committed"] == {"usd_low": 0.0, "usd_high": 0.0, "runs": 0}
    assert body["unpriced"] == {"usd_low": 1.5, "usd_high": 3.0, "runs": 1}


def test_a_failed_leg_vertex_says_processed_nothing_carries_no_money(client):
    cl, _ = client(
        user={
            "discovery_state": {"last_discovery": {"batch_run": "dead"}},
        },
        batch={
            "dead": {
                "user_id": "u1",
                "state": "failed",
                "stage": "parse",
                "committed": {
                    "parse": {"requests": 9, "usd_low": 0.4, "usd_high": 0.8}
                },
                "vertex_completion": {
                    "parse": {"successful": 0, "failed": 0, "incomplete": 9}
                },
                "error": "JOB_STATE_CANCELLED: cancelled",
            }
        },
    )
    body = cl.get("/activity").json()
    batch = _items(body)["batch_scoring"]

    assert batch["state"] == "failed"
    assert batch["detail"]["committed_usd_low"] == 0.0
    assert batch["detail"]["committed_usd_high"] == 0.0
    assert body["unpriced"] == {"usd_low": 0.0, "usd_high": 0.0, "runs": 0}


# --------------------------------------------------------------------------
# The run ledger as the in-flight record
# --------------------------------------------------------------------------


def test_an_open_run_doc_reads_running_then_stalled_past_the_ceiling(client):
    for minutes, expected in ((5, "running"), (31, "stalled")):
        cl, _ = client(
            runs={
                "r1": {
                    "run_id": "r1",
                    "state": "running",
                    "runner": "auto_discovery",
                    "started_at": _iso(minutes),
                }
            }
        )
        item = _items(cl.get("/activity").json())["discovery"]
        assert item["state"] == expected, minutes
        assert item["since"] == _iso(minutes)
        assert item["ref"] == {"run_id": "r1"}


def test_a_closed_run_doc_is_not_liveness(client):
    """Only ``state == "running"`` docs are read — the 67 live closed ones and
    every legacy doc with no ``state`` at all must not read as activity."""
    cl, _ = client(
        user={"discovery_settings": {"auto_discovery": True}},
        runs={
            "closed": {
                "run_id": "closed",
                "state": "done",
                "runner": "auto_discovery",
                "started_at": _iso(4),
            },
            "legacy": {
                "run_id": "legacy",
                "runner": "auto_discovery",
                "started_at": _iso(4),
            },
        },
    )
    item = _items(cl.get("/activity").json())["discovery"]
    assert item["state"] not in ("running", "stalled")


def test_an_unknown_runner_is_dropped_rather_than_guessed_at(client):
    cl, _ = client(
        runs={
            "r1": {
                "run_id": "r1",
                "state": "running",
                "runner": "some_future_pipeline",
                "started_at": _iso(1),
            }
        }
    )
    body = cl.get("/activity").json()
    assert all(i["state"] != "running" for i in body["items"])


def test_a_held_slot_lease_is_running_even_before_the_ledger_doc_lands(client):
    cl, _ = client(
        user={
            "discovery_settings": {"auto_discovery": True},
            "discovery_state": {
                "discovery_lease": {
                    "acquired_at": _iso(2),
                    "expires_at": _iso(-29),
                }
            },
        }
    )
    item = _items(cl.get("/activity").json())["discovery"]
    assert item["state"] == "running"
    assert item["since"] == _iso(2)


# --------------------------------------------------------------------------
# The application legs
# --------------------------------------------------------------------------


def _app(status: str, *, lease=None, at: str | None = None) -> dict:
    doc = {"status": status, "timeline": [{"at": at or _iso(4), "status": status}]}
    if lease is not None:
        doc["lease"] = lease
    return doc


def test_a_queued_application_is_queued_not_running(client):
    """Nothing has claimed it. Saying "writing your resume…" would be the
    original bug in miniature."""
    cl, _ = client(applications={"app-1": _app("queued")})
    item = _items(cl.get("/activity").json())["tailoring"]
    assert item["state"] == "queued"
    assert item["ref"] == {"application_id": "app-1"}


def test_a_tailoring_application_whose_lease_lapsed_is_stalled(client):
    cl, _ = client(
        applications={
            "app-1": _app("tailoring", lease={"expires_at": _iso(10), "owner": "x"})
        }
    )
    assert _items(cl.get("/activity").json())["tailoring"]["state"] == "stalled"

    cl, _ = client(
        applications={
            "app-1": _app("tailoring", lease={"expires_at": _iso(-10), "owner": "x"})
        }
    )
    assert _items(cl.get("/activity").json())["tailoring"]["state"] == "running"


def test_no_applications_at_all_is_never_started(client):
    cl, _ = client()
    body = _items(cl.get("/activity").json())
    assert body["tailoring"]["state"] == "never_started"
    assert body["submission"]["state"] == "never_started"
    # A brand-new account: nothing has run and — because both discovery
    # toggles default off — nothing is scheduled either. Every leg says one of
    # those two things, and neither reads as progress.
    assert {i["state"] for i in cl.get("/activity").json()["items"]} == {
        "never_started",
        "idle_unscheduled",
    }


def test_a_submitted_application_finishes_the_submission_leg(client):
    cl, _ = client(
        applications={"app-1": _app("submitted"), "app-2": _app("ready_for_review")}
    )
    body = _items(cl.get("/activity").json())
    assert body["submission"]["state"] == "finished"
    assert body["tailoring"]["state"] == "finished"


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_every_leg_is_always_present_and_well_formed(client):
    body = client()[0].get("/activity").json()
    assert [i["kind"] for i in body["items"]] == list(activity.KINDS)
    assert body["polled_at"] == NOW.isoformat()
    for item in body["items"]:
        assert set(item) == {
            "kind",
            "state",
            "since",
            "next_at",
            "ref",
            "done",
            "total",
            "detail",
        }
        # BOTH or NEITHER, on every item, always.
        assert (item["done"] is None) == (item["total"] is None)


def test_a_half_populated_progress_pair_is_dropped():
    """The rule lives in ``_item`` so no future caller can bypass it."""
    assert activity._item("scoring", "running", done=3)["done"] is None
    assert activity._item("scoring", "running", total=9)["total"] is None
    both = activity._item("scoring", "running", done=3, total=9)
    assert (both["done"], both["total"]) == (3, 9)


# --------------------------------------------------------------------------
# Both of these were found by curling the live account, not by reasoning
# --------------------------------------------------------------------------


def test_an_uncountable_backlog_is_never_reported_as_up_to_date(client):
    """``unscored_backlog: null`` means the count failed, not that it is zero.

    Seen live on one account: the last cycle recorded ``unscored_backlog: null`` (``_backlog``
    returns None rather than a fabricated 0 when the query fails) while 9,219
    pending jobs sat unscored — and an earlier version of this endpoint answered
    ``finished``. "Up to date" over a nine-thousand-job backlog is the same bug
    as "Scoring in progress" over an empty queue, pointed the other way.
    """
    cl, _ = client(
        user={
            "discovery_settings": {"auto_discovery": False},
            "discovery_state": {
                "last_discovery_at": _iso(600),
                "last_discovery": {"unscored_backlog": None, "new_jobs": 9219},
            },
        }
    )
    scoring = _items(cl.get("/activity").json())["scoring"]
    assert scoring["state"] == "idle_unscheduled"
    assert scoring["detail"]["unscored_backlog"] is None

    # A *measured* zero is the only thing that licenses "finished".
    cl, _ = client(
        user={
            "discovery_state": {
                "last_discovery_at": _iso(600),
                "last_discovery": {"unscored_backlog": 0, "scored": 12},
            }
        }
    )
    assert _items(cl.get("/activity").json())["scoring"]["state"] == "finished"


@pytest.mark.parametrize(
    ("state", "expected"),
    [("failed", "failed"), ("done", "finished")],
)
def test_a_batch_the_last_cycle_started_is_not_never_started(client, state, expected):
    """It is terminal, which is a different claim from never having run.

    Seen live on one account: ``last_discovery.batch_run`` held a real tag from the day
    before and this endpoint answered ``never_started``.
    """
    cl, _ = client(
        user={
            "discovery_state": {
                "last_discovery": {"batch_run": "20260101-000000-abc123"},
            }
        },
        batch={
            "20260101-000000-abc123": {
                "user_id": "u1",
                "state": state,
                "stage": "score",
                "updated_at": _iso(200),
                "error": "JOB_STATE_FAILED: quota" if state == "failed" else None,
                "committed": {"score": {"usd_low": 0.5, "usd_high": 1.0}},
            }
        },
    )
    item = _items(cl.get("/activity").json())["batch_scoring"]
    assert item["state"] == expected
    assert item["ref"] == {"batch_run": "20260101-000000-abc123"}
    # Still not liveness, whichever way it ended.
    assert item["done"] is None and item["total"] is None


def test_a_recorded_tag_whose_document_is_gone_stays_never_started(client):
    """No document, no claim. Better than inventing an outcome for it."""
    cl, _ = client(
        user={"discovery_state": {"last_discovery": {"batch_run": "vanished"}}}
    )
    assert _items(cl.get("/activity").json())["batch_scoring"]["state"] == (
        "never_started"
    )


# --------------------------------------------------------------------------
# The allowance block — what is left of each cap, and when it rolls
# --------------------------------------------------------------------------

WEDNESDAY = datetime(2026, 9, 30, 12, 30, 0, tzinfo=UTC)


def _week_key(now: datetime) -> str:
    return _dw_key(now, "UTC")


def _allowance(cl) -> dict:
    return cl.get("/activity").json()["allowance"]


def test_allowance_reports_two_independent_resets(client, monkeypatch):
    """Searches roll weekly, ratings nightly. **Different instants.**

    A single shared ``resets_at`` cannot express both, and the cheap way to
    get this wrong is to point ratings at ``discovery_budget.resets_at`` —
    which is right one day in seven and wrong the other six.
    """
    cl, _ = client()
    monkeypatch.setattr(activity, "_now", lambda: WEDNESDAY)
    allowance = _allowance(cl)

    # Wednesday: next Monday 00:00 UTC vs the coming UTC midnight.
    assert allowance["searches"]["resets_at"] == "2026-10-05T00:00:00+00:00"
    assert allowance["ratings"]["resets_at"] == "2026-10-01T00:00:00+00:00"
    assert allowance["searches"]["resets_at"] != allowance["ratings"]["resets_at"]

    # And on a Sunday the two genuinely coincide — which is why the assertion
    # above has to be made on a day that isn't one. ``NOW`` is a Sunday.
    monkeypatch.setattr(activity, "_now", lambda: NOW)
    sunday = _allowance(cl)
    assert sunday["searches"]["resets_at"] == "2026-09-28T00:00:00+00:00"
    assert sunday["ratings"]["resets_at"] == sunday["searches"]["resets_at"]


def test_allowance_is_null_when_a_cap_is_off(client, monkeypatch):
    """``DISCOVERY_RUNS_PER_WEEK=0`` is the kill switch, not a cap of zero.

    ``0`` here would render as "no searches left" when the truth is "no
    limit" — the same reason ``Reservation.remaining_week`` is ``int | None``.
    """
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "0")
    cl, _ = client(
        user={
            "discovery_budget": {
                "week": _week_key(NOW),
                "runs_this_week": 9,
            }
        }
    )
    searches = _allowance(cl)["searches"]

    assert searches["limit"] is None
    assert searches["remaining"] is None
    # Used is still a real count — what has happened is knowable either way.
    assert searches["used"] == 9


def test_allowance_counts_the_current_window_only(client, monkeypatch):
    """Both counters roll lazily, and the display must roll with them."""
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "14")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "3")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "3")
    cl, _ = client(
        user={
            "discovery_budget": {"week": _week_key(NOW), "runs_this_week": 9},
            "scoring_budget": {"day": NOW.date().isoformat(), "jobs_scored_today": 2},
        }
    )
    allowance = _allowance(cl)
    assert allowance["searches"] == {
        "used": 9,
        "limit": 14,
        "remaining": 5,
        "resets_at": "2026-09-28T00:00:00+00:00",
    }
    assert allowance["ratings"] == {
        "used": 2,
        "limit": 3,
        "remaining": 1,
        "remaining_cycle": 3,
        "resets_at": "2026-09-28T00:00:00+00:00",
    }

    # Last week's and yesterday's counters are spent windows, not spent quota.
    stale, _ = client(
        user={
            "discovery_budget": {"week": "2026-W01", "runs_this_week": 14},
            "scoring_budget": {"day": "2026-01-01", "jobs_scored_today": 3},
        }
    )
    allowance = _allowance(stale)
    assert allowance["searches"]["used"] == 0
    assert allowance["searches"]["remaining"] == 14
    assert allowance["ratings"]["used"] == 0
    assert allowance["ratings"]["remaining"] == 3


def test_allowance_never_debits_anything(client, monkeypatch):
    """Looking must not cost. Polling this endpoint cannot burn a search."""
    monkeypatch.setenv("DISCOVERY_RUNS_PER_WEEK", "14")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "3")
    user = {
        "discovery_budget": {"week": _week_key(NOW), "runs_this_week": 9},
        "scoring_budget": {"day": NOW.date().isoformat(), "jobs_scored_today": 2},
    }
    cl, db = client(user=user)
    before = deepcopy(db.users["u1"])

    first = _allowance(cl)
    for _ in range(5):
        assert _allowance(cl) == first

    # Not just "the numbers didn't move": the stored document is untouched.
    # The fake honours ``set``, so a debit written back would show up here.
    assert db.users["u1"] == before


def test_allowance_is_whole_even_for_a_user_with_no_budget_state(client):
    """A fresh account has neither map. It has its full allowance, not zero."""
    cl, _ = client(user={})
    allowance = _allowance(cl)
    for block in ("searches", "ratings"):
        assert allowance[block]["used"] == 0
        assert allowance[block]["resets_at"].endswith("+00:00")
    assert allowance["searches"]["remaining"] == allowance["searches"]["limit"]
    assert allowance["ratings"]["remaining"] == allowance["ratings"]["limit"]


def test_allowance_reports_the_grant_the_next_reservation_would_make(
    client, monkeypatch
):
    """A window from a previous UTC day rolls over, here exactly as in the
    reservation. This is the stored state that froze a real account: a
    200-slot window never used or released, read days later under a cap of 3.
    The allowance must report the 3 the next reservation will grant."""
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "3")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "3")
    later = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
    frozen = {
        "day": "2026-09-26",
        "jobs_scored_today": 200,
        "cycle_id": "cycle-stale",
        "jobs_scored_this_cycle": 200,
        "updated_at": "2026-09-26T19:11:19+00:00",
    }
    cl, _ = client(user={"scoring_budget": dict(frozen)})
    monkeypatch.setattr(activity, "_now", lambda: later)
    ratings = _allowance(cl)["ratings"]

    assert ratings["used"] == 0
    assert ratings["remaining"] == 3
    assert ratings["remaining_cycle"] == 3

    # Exactly what a real reservation would grant, asked the way every ad-hoc
    # scorer asks (``cycle_id=None`` — draw on the open window).
    _state, res = matching_budget.apply_reservation(
        dict(frozen),
        3,
        now=later,
        cycle_id=None,
        limits=matching_budget.Limits(3, 3),
    )
    assert res.granted == ratings["remaining"] == 3


def test_allowance_reports_zero_for_a_spent_window_on_the_same_day(client, monkeypatch):
    """The cycle can still bind within a day: the day has room, the window
    does not, and ``remaining`` says what a request would get."""
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "10")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "3")
    cl, _ = client(
        user={
            "scoring_budget": {
                "day": NOW.date().isoformat(),
                "jobs_scored_today": 3,
                "cycle_id": "c1",
                "jobs_scored_this_cycle": 3,
            }
        }
    )
    ratings = _allowance(cl)["ratings"]
    assert ratings["used"] == 3
    assert ratings["remaining"] == 0
    assert ratings["remaining_cycle"] == 0


def test_allowance_remaining_is_the_day_when_the_day_is_what_binds(client, monkeypatch):
    """The mirror case, so the fix cannot be "always report zero"."""
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "3")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "3")
    cl, _ = client(
        user={
            "scoring_budget": {
                "day": NOW.date().isoformat(),
                "jobs_scored_today": 2,
                "cycle_id": "c1",
                "jobs_scored_this_cycle": 0,
            }
        }
    )
    ratings = _allowance(cl)["ratings"]
    assert ratings["used"] == 2
    assert ratings["remaining"] == 1  # the day, not the cycle's untouched 3
    assert ratings["remaining_cycle"] == 3


def test_allowance_reports_ratings_actually_used_not_the_cap(client, monkeypatch):
    """A mid-day cap cut (400 -> 3) must not rewrite history: an account
    holding 46 rated jobs under the old cap reads 46, not the new limit.

    ``limit - remaining`` clamps, and clamping here understates what was
    spent, which is the direction this whole program exists to stop.
    """
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "3")
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "3")
    cl, _ = client(
        user={
            "scoring_budget": {
                "day": NOW.date().isoformat(),
                "jobs_scored_today": 46,
                "jobs_scored_this_cycle": 46,
            }
        }
    )
    ratings = _allowance(cl)["ratings"]
    assert ratings["used"] == 46
    assert ratings["limit"] == 3
    assert ratings["remaining"] == 0
    # And searches read the same way — the real counter, over the cap.
    over, _ = client(
        user={"discovery_budget": {"week": _week_key(NOW), "runs_this_week": 40}}
    )
    searches = _allowance(over)["searches"]
    assert searches["used"] == 40
    assert searches["remaining"] == 0
