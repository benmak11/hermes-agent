# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The exploration sample: labels from outside the scorer's own belief.

Every decision Hermes has ever collected was made on a job the current scorer
already liked — the queue hides everything under 60 — so the labels only ever
cover the region the scorer put above its own bar. ``EXPLORATION_RATE``
deterministically surfaces a slice of the hidden 21-59 band so some decisions
come from outside it.

Three properties carry the whole feature, and each has its own section below.

**Off is really off.** The default is ``0`` and that is what almost every user
runs, so at rate 0 the job doc and the ``/jobs/pending`` payload have to be
exactly what they were before this existed — same jobs, same order, same two
counts, no new key anywhere.

**The sample is stable across processes, not merely within one.** Python salts
``hash(str)`` per process, so a sample built on ``hash()`` reshuffles on every
cold start while a single-process test passes happily. The subprocess test
below is the one that can actually see that, and the pinned digest values stop
the algorithm being swapped out quietly.

**A sampled job is indistinguishable in the response.** The sample exists to
get an unbiased decision on a job the scorer rated low; anything the UI could
branch on tells the user it does not really count, and the label is worthless.
"""

import asyncio
import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.routes.jobs as jobs_mod
import tools.matching.score as score
from api.deps import verify_user
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown

# --- fixtures ---------------------------------------------------------------


def _job(job_id: str = "job-0", *, parsed: ParsedJD | None = None) -> Job:
    job = Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id=job_id,
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=f"Build things {job_id}.",
        discovered_at=datetime.now(UTC),
    )
    job.jd_parsed = parsed
    return job


def _match(score_value: float) -> JobMatch:
    return JobMatch(
        job_id="j1",
        overall_score=score_value,
        breakdown=ScoreBreakdown(
            role_fit=score_value,
            qualifications_match=score_value,
            seniority_match=score_value,
            comp_alignment=50,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="A fine match.",
        recommendation="apply",
    )


class _KeepingRef:
    """A job doc that survives scoring: ``persist_result`` updates in place."""

    def __init__(self):
        self.updates: list[dict] = []

    async def update(self, fields: dict) -> None:
        self.updates.append(fields)

    @property
    def written(self) -> dict:
        return self.updates[0]


class _DiscardingRef:
    """A job doc that gets tombstoned: ``persist_result`` walks parent.parent."""

    def __init__(self):
        self.stones: list[dict] = []
        outer = self

        class _Doc:
            async def set(self, doc):
                outer.stones.append(doc)

        class _Collection:
            def document(self, job_id):
                return _Doc()

        class _UserRef:
            def collection(self, name):
                return _Collection()

        self.parent = SimpleNamespace(parent=_UserRef())

    async def delete(self) -> None:
        pass

    @property
    def written(self) -> dict:
        return self.stones[0]


@pytest.fixture
def rate(monkeypatch):
    """Set ``EXPLORATION_RATE`` the way deployment does — through the env."""

    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv("EXPLORATION_RATE", raising=False)
        else:
            monkeypatch.setenv("EXPLORATION_RATE", value)

    _set(None)
    return _set


# --- the band ---------------------------------------------------------------


def test_the_band_is_everything_the_queue_hides_and_nothing_else(rate):
    """Below 21 the job is tombstoned and never reaches the queue; at 60 it is
    already shown. Flagging either is at best a no-op write and at worst a
    claim that the scorer hid a job it did not hide."""
    rate("1.0")  # everything that is eligible at all gets picked

    in_band = [
        s
        for s in (score.DISCARD_AT_OR_BELOW, 21, 40, 59, 60, 95)
        if score.should_explore(_job(), _match(s), None)
    ]

    assert in_band == [21, 40, 59]


@pytest.mark.parametrize("score_value", [20.5, 59.35, 59.4, 59.9])
def test_fractional_scores_inside_the_band_are_sampled(rate, score_value):
    """``overall_score`` is a float and the prompt weights five sub-scores at
    0.30/0.25/0.20/0.15/0.10, so fractions are routine. The route hides
    anything ``< 60``, so 59.9 is hidden — a band topping out at "<= 59" would
    leave a hole at the slice nearest the decision boundary, which is the most
    informative part of the band."""
    rate("1.0")

    assert score.should_explore(_job(), _match(score_value), None)


@pytest.mark.parametrize("score_value", [20.0, 60.0, 60.1])
def test_fractional_scores_outside_the_band_are_not_sampled(rate, score_value):
    """Both bounds are strict the other way too: 20.0 is tombstoned and 60.0 is
    already shown."""
    rate("1.0")

    assert not score.should_explore(_job(), _match(score_value), None)


def test_the_band_tracks_the_routes_default_threshold():
    """The hidden band is *defined* by what the queue hides, and the constant
    is a mirror of the route's default because ``tools`` must not import
    ``api``. If the route's default ever moves, the mirror is wrong and the
    sample starts flagging jobs the user can already see."""
    import inspect

    default = inspect.signature(jobs_mod.list_pending_jobs).parameters["min_score"]
    assert score.QUEUE_DEFAULT_MIN_SCORE == default.default


# --- geo ---------------------------------------------------------------------


def test_a_geo_ineligible_job_in_the_band_is_never_sampled(rate):
    """Ben cannot take these, so surfacing one spends a decision on a
    rejection that says nothing about fit — it poisons the labels."""
    rate("1.0")
    gate = {"verdict": "ineligible", "rule": "foreign_country", "pro_capped": False}

    assert not score.should_explore(_job(), _match(45), gate)


@pytest.mark.parametrize("verdict", ["eligible", "abstain"])
def test_the_other_verdicts_do_not_block_the_sample(rate, verdict):
    """``abstain`` is "let Pro decide", and Pro did decide: it scored the job
    above its own geo cap of 20. Treating abstain as a veto would empty the
    sample, since the gate abstains on most jobs."""
    rate("1.0")

    assert score.should_explore(_job(), _match(45), {"verdict": verdict})


def test_a_job_with_no_geo_record_is_not_treated_as_ineligible(rate):
    """No record means no profile or no parse was available, not a rejection.
    Pro caps a geographically ineligible role at exactly 20, so a job that
    reached 21+ already cleared the only judgement that was actually made."""
    rate("1.0")

    assert score.should_explore(_job(), _match(45), None)


# --- off is really off -------------------------------------------------------


def test_the_default_rate_is_zero(rate):
    assert score.exploration_rate() == 0.0


@pytest.mark.parametrize("value", ["banana", "-0.1", "1.5", ""])
def test_an_unusable_rate_falls_back_to_off(rate, value):
    """The failure mode of a typo must be "no sampling", never "surface the
    whole hidden band"."""
    rate(value)
    assert score.exploration_rate() == 0.0


def test_nothing_is_sampled_at_rate_zero(rate):
    rate("0")
    assert not any(
        score.should_explore(_job(f"job-{i}"), _match(45), None) for i in range(200)
    )


@pytest.mark.parametrize("job_id", ["job-9", "job-1", "job-0", "job-7"])
def test_rate_zero_writes_no_exploration_field_on_the_job_doc(rate, job_id):
    """The default path. The job doc has to be exactly what it was before this
    feature existed — not ``exploration: False``, which would still change the
    document and therefore the response.

    Parametrized over ids spanning the digest range on purpose, ``job-9``
    included: it draws 0.037, so it is sampled at every rate down to 0.05. A
    single-id version of this test passes against an implementation that
    quietly treats rate 0 as a small positive rate, which makes the test's name
    a claim it does not check — the recurring defect in this repo.
    """
    rate(None)
    ref = _KeepingRef()

    asyncio.run(score.persist_result(ref, _job(job_id), _match(45)))

    assert "exploration" not in ref.written


def test_a_sampled_job_doc_carries_the_flag(rate):
    rate("1.0")
    ref = _KeepingRef()

    asyncio.run(score.persist_result(ref, _job(), _match(45)))

    assert ref.written["exploration"] is True


def test_a_tombstoned_job_is_never_flagged(rate):
    """Sampling is a decision about the review queue, and a tombstone never
    reaches it. A flag there would be a field nothing reads, on 71% of docs."""
    rate("1.0")
    ref = _DiscardingRef()

    asyncio.run(score.persist_result(ref, _job(), _match(10)))

    assert "exploration" not in ref.written


# --- determinism across processes --------------------------------------------

#: Pinned so that changing the hash construction breaks a test rather than
#: silently reshuffling every live sample. Computed from
#: ``sha256(b"exploration:" + job_id)``; see ``score.exploration_sample``.
_EXPECTED_AT_HALF = ["job-0", "job-1", "job-2", "job-3", "job-6", "job-8", "job-9"]

_IDS = [f"job-{i}" for i in range(12)]

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])


def test_the_sample_matches_pinned_expected_ids():
    picked = [i for i in _IDS if score.exploration_sample(i, 0.5)]
    assert picked == _EXPECTED_AT_HALF


def test_the_sample_is_stable_across_separate_processes():
    """The trap this feature is written around.

    ``hash("job-0")`` differs in every interpreter unless ``PYTHONHASHSEED`` is
    pinned, so a sample built on it reshuffles on every worker start and every
    cold start — and a test living in one process cannot see that at all. Two
    subprocesses with deliberately different seeds is the cheapest thing that
    can.
    """
    program = (
        "import json;from tools.matching.score import exploration_sample as s;"
        f"print(json.dumps([i for i in {_IDS!r} if s(i, 0.5)]))"
    )
    runs = []
    for seed in ("0", "1", "12345"):
        out = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
            cwd=_REPO_ROOT,
        )
        runs.append(json.loads(out.stdout))

    assert runs[0] == runs[1] == runs[2] == _EXPECTED_AT_HALF


def test_the_rate_is_honoured_approximately_over_many_ids():
    """A predicate that is always true or always false satisfies determinism
    perfectly and samples nothing useful. 0.05 over 10,000 ids has to land
    somewhere near 500."""
    picked = sum(1 for i in range(10_000) if score.exploration_sample(f"j{i}", 0.05))

    assert 375 < picked < 625, picked


def test_a_higher_rate_is_a_superset_of_a_lower_one():
    """The id's draw is fixed; only the threshold moves. So raising the rate
    adds jobs to the sample and never swaps the existing ones out — which is
    what makes "stable across runs" survive a rate change."""
    small = {i for i in _IDS if score.exploration_sample(i, 0.2)}
    large = {i for i in _IDS if score.exploration_sample(i, 0.6)}

    assert small < large


def test_the_exploration_sample_is_not_the_geo_holdout():
    """Two samplers over the same ids at the same rate must be independent
    draws. Identical ones would make every explored job a hold-out job, so the
    exploration labels would describe the hold-out population, not the band."""
    from tools.matching.pipeline import geo_holdout

    explored = {i for i in _IDS if score.exploration_sample(i, 0.5)}
    held_out = {i for i in _IDS if geo_holdout(i, 0.5)}

    assert explored != held_out


# --- the pending route -------------------------------------------------------


class _JobSnap:
    def __init__(self, doc_id: str, data: dict):
        self.id, self._data = doc_id, data

    def to_dict(self):
        return dict(self._data)


class _JobQuery:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *, filter=None):
        return self

    def stream(self):
        return iter(self._docs)


class _JobsClient:
    def __init__(self, docs):
        self._docs = docs

    def collection(self, name):
        if name == "jobs":
            return _JobQuery(self._docs)
        return self

    def document(self, uid):
        return self


def _client(docs, monkeypatch) -> TestClient:
    monkeypatch.setattr(jobs_mod, "_client", lambda: _JobsClient(docs))
    monkeypatch.setattr(jobs_mod, "tick_user", lambda uid: None)
    app = FastAPI()
    app.include_router(jobs_mod.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app)


def _doc(doc_id: str, score_value: int, **extra) -> _JobSnap:
    return _JobSnap(
        doc_id,
        {
            "user_decision": "pending",
            "company": "Acme",
            "match": {"overall_score": score_value},
            **extra,
        },
    )


def test_with_no_sampled_jobs_the_payload_is_what_it_always_was(monkeypatch):
    """The rate-0 contract, asserted on the whole body rather than a field:
    same jobs, same order, same two counts. Nothing about this response may
    move because the feature exists."""
    docs = [_doc("low", 30), _doc("high", 90), _doc("mid", 70), _JobSnap("new", {})]
    client = _client(docs, monkeypatch)

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
        "pending_total": 4,
        "scored_total": 3,
    }


def test_a_sampled_job_is_returned_from_under_the_threshold(monkeypatch):
    docs = [_doc("hidden", 35, exploration=True), _doc("high", 90)]
    client = _client(docs, monkeypatch)

    body = client.get("/jobs/pending").json()

    assert [j["id"] for j in body["jobs"]] == ["high", "hidden"]


def test_a_sampled_job_is_indistinguishable_from_a_normal_one(monkeypatch):
    """A badge, an extra key, any difference at all lets the UI say "this one
    doesn't count" — and an unbiased decision is the entire product of the
    sample. The two bodies must differ only by score and id."""
    sampled = _client([_doc("hidden", 35, exploration=True)], monkeypatch)
    body = sampled.get("/jobs/pending").json()

    assert body["jobs"] == [
        {
            "id": "hidden",
            "user_decision": "pending",
            "company": "Acme",
            "match": {"overall_score": 35},
        }
    ]
    assert "exploration" not in json.dumps(body)


def test_a_sampled_job_above_the_threshold_is_returned_once(monkeypatch):
    """A stale flag on a job that later scored above the bar must not
    duplicate it into the list."""
    docs = [_doc("high", 90, exploration=True)]
    client = _client(docs, monkeypatch)

    body = client.get("/jobs/pending").json()

    assert [j["id"] for j in body["jobs"]] == ["high"]


def test_an_unscored_job_is_never_surfaced_by_the_flag(monkeypatch):
    """``exploration`` cannot smuggle a job with no score past the "not scored
    yet" guard — the card has nothing to render and the counts would lie."""
    docs = [_JobSnap("new", {"user_decision": "pending", "exploration": True})]
    client = _client(docs, monkeypatch)

    body = client.get("/jobs/pending").json()

    assert body == {"jobs": [], "pending_total": 1, "scored_total": 0}


# --- the decided shelves -----------------------------------------------------


def test_the_decided_shelves_also_strip_the_flag(monkeypatch):
    """``/jobs/decided`` documents its own shape as "same shape as
    /jobs/pending so the web app can reuse its card rendering" — two routes
    feeding one renderer with different shapes is how an unmeant key becomes
    visible. And a decision here is not final: the starred/skipped shelves
    offer restore and approve, so a re-decided job is a fresh judgement that a
    marker would bias exactly as it would in the queue."""
    docs = [_doc("hidden", 35, exploration=True, user_decision="rejected")]
    client = _client(docs, monkeypatch)

    body = client.get("/jobs/decided?decision=rejected").json()

    assert body["jobs"] == [
        {
            "id": "hidden",
            "user_decision": "rejected",
            "company": "Acme",
            "match": {"overall_score": 35},
        }
    ]
    assert "exploration" not in json.dumps(body)
