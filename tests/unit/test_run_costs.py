# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-run cost measurement: in-process accumulation → Firestore run ledger.

The ``llm.call`` → BigQuery sink only exists on Cloud Run, so "what did that
run cost?" is answered by an in-process accumulator keyed on the bound
``run_id`` and flushed to ``users/{uid}/runs/{run_id}``. These tests pin the
three things that make that number trustworthy: calls land under the run that
made them (and nowhere at all outside a run), every numeric leaf is written as
an Increment so a batch ingest arriving hours later adds to the originating
cycle, and a Firestore failure never propagates into the pipeline.
"""

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import structlog
from google.cloud.firestore_v1.transforms import Increment
from google.genai import types

import api.routes.discovery as discovery
from models.job import Job
from models.match import JobMatch, ScoreBreakdown
from obs import llm_cost
from obs.llm_cost import record_llm_call, reset_run_cost, run_cost_snapshot
from tools.matching import score
from tools.run_costs import persist_run_cost


@pytest.fixture(autouse=True)
def clean_slate():
    """No accumulator or bound context leaks between tests."""
    llm_cost._ACCUMULATORS.clear()
    structlog.contextvars.clear_contextvars()
    yield
    llm_cost._ACCUMULATORS.clear()
    structlog.contextvars.clear_contextvars()


def _response(
    *,
    model_version: str = "gemini-3.1-pro-preview",
    prompt_token_count: int = 1000,
    candidates_token_count: int = 200,
    thoughts_token_count: int = 300,
    cached_content_token_count: int = 0,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        model_version=model_version,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_token_count,
            candidates_token_count=candidates_token_count,
            thoughts_token_count=thoughts_token_count,
            cached_content_token_count=cached_content_token_count,
            total_token_count=1500,
        ),
    )


class _FakeDoc:
    def __init__(self, sink: dict, path: tuple):
        self._sink = sink
        self._path = path

    def collection(self, name: str) -> "_FakeCollection":
        return _FakeCollection(self._sink, (*self._path, name))

    async def set(self, doc: dict, merge: bool = False) -> None:
        self._sink.update(path=self._path, doc=doc, merge=merge)


class _FakeCollection:
    def __init__(self, sink: dict, path: tuple):
        self._sink = sink
        self._path = path

    def document(self, doc_id: str) -> _FakeDoc:
        return _FakeDoc(self._sink, (*self._path, doc_id))


class _FakeDB:
    """Just enough Firestore to capture one nested set(..., merge=True)."""

    def __init__(self):
        self.written: dict = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self.written, (name,))


# --------------------------------------------------- accumulate / snapshot


def test_accumulates_under_the_bound_run_id():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.parse_jd", response=_response())
        record_llm_call(step="matching.score", response=_response())

    totals = run_cost_snapshot("r1")
    assert totals["calls"] == 2
    assert totals["input_tokens"] == 2000
    assert totals["output_tokens"] == 400
    assert totals["thinking_tokens"] == 600
    one_call = (1000 * 2.00 + 500 * 12.00) / 1_000_000
    assert totals["cost_usd"] == pytest.approx(round(2 * one_call, 6))


def test_by_step_aggregates_calls_and_cost_per_call_site():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.parse_jd", response=_response())
        record_llm_call(step="matching.score", response=_response())
        record_llm_call(step="matching.score", response=_response())

    by_step = run_cost_snapshot("r1")["by_step"]
    assert by_step["matching.parse_jd"]["calls"] == 1
    assert by_step["matching.score"]["calls"] == 2
    assert by_step["matching.score"]["cost_usd"] == pytest.approx(
        2 * by_step["matching.parse_jd"]["cost_usd"]
    )


def test_runs_do_not_bleed_into_each_other():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.score", response=_response())
    with structlog.contextvars.bound_contextvars(run_id="r2"):
        record_llm_call(step="matching.score", response=_response())

    assert run_cost_snapshot("r1")["calls"] == 1
    assert run_cost_snapshot("r2")["calls"] == 1


def test_no_run_bound_is_a_silent_noop():
    # An API-request call site has no run_id; it must not raise, and must not
    # pool every unattributed call under a None key.
    record_llm_call(step="profile.extract", response=_response())
    assert llm_cost._ACCUMULATORS == {}


def test_unpriced_model_still_counts_calls_and_tokens():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(
            step="matching.score", response=_response(model_version="gemini-mystery")
        )

    totals = run_cost_snapshot("r1")
    assert totals["calls"] == 1 and totals["input_tokens"] == 1000
    assert totals["cost_usd"] == 0.0  # the pricing_unknown warning flags the gap


def test_snapshot_of_an_unknown_run_is_the_empty_shape():
    totals = run_cost_snapshot("never-ran")
    assert totals["calls"] == 0 and totals["cost_usd"] == 0.0
    assert totals["by_step"] == {}


def test_snapshot_does_not_hand_out_the_live_accumulator():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.score", response=_response())
    snapshot = run_cost_snapshot("r1")
    snapshot["calls"] = 99
    snapshot["by_step"]["matching.score"]["calls"] = 99
    assert run_cost_snapshot("r1")["calls"] == 1
    assert run_cost_snapshot("r1")["by_step"]["matching.score"]["calls"] == 1


def test_reset_drops_the_entry():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.score", response=_response())
    reset_run_cost("r1")
    assert "r1" not in llm_cost._ACCUMULATORS
    reset_run_cost("r1")  # idempotent: flushing twice must not blow up


# ------------------------------------------------------ persist_run_cost()


def _flush(db, **kw):
    return asyncio.run(persist_run_cost(db, "u1", "r1", **kw))


def test_persist_writes_the_ledger_doc_and_resets():
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.score", response=_response())
    db = _FakeDB()

    _flush(db, runner="auto_discovery", trigger="manual", jobs={"scored": 1})

    assert db.written["path"] == ("users", "u1", "runs", "r1")
    assert db.written["merge"] is True
    doc = db.written["doc"]
    assert doc["run_id"] == "r1" and doc["user_id"] == "u1"
    assert doc["runner"] == "auto_discovery" and doc["trigger"] == "manual"
    assert doc["ended_at"]
    # Every numeric leaf is an Increment so a late batch ingest adds to this
    # doc instead of overwriting the cycle that ordered it.
    assert isinstance(doc["llm"]["calls"], Increment)
    assert doc["llm"]["calls"].value == 1
    assert doc["llm"]["input_tokens"].value == 1000
    assert doc["by_step"]["matching.score"]["calls"].value == 1
    assert doc["jobs"]["scored"].value == 1
    # Flushed means banked: the next flush must not re-count this spend.
    assert "r1" not in llm_cost._ACCUMULATORS


def test_persist_drops_none_meta_so_a_late_write_cannot_blank_the_doc():
    # The batch ingest flush knows the run tag but not the trigger; passing
    # None must leave the originating cycle's value alone.
    db = _FakeDB()
    _flush(db, runner=None, trigger=None, batch_run="20260710-120000-abc123")
    doc = db.written["doc"]
    assert "runner" not in doc and "trigger" not in doc
    assert doc["batch_run"] == "20260710-120000-abc123"


def test_persist_of_a_zero_cost_run_still_records_it():
    db = _FakeDB()
    _flush(db, runner="liveness_sweep")
    assert db.written["doc"]["llm"]["cost_usd"].value == 0.0


def test_empty_by_step_is_omitted_rather_than_written():
    """An empty map is a literal, not a transform: writing it would land in
    the update mask and erase the breakdown an earlier wave of the same run
    recorded (parse-stage ingest, then a score ingest that priced nothing)."""
    db = _FakeDB()
    _flush(db, runner="auto_discovery")
    assert "by_step" not in db.written["doc"]
    assert "jobs" not in db.written["doc"]


def test_persist_swallows_firestore_failures():
    class _FailingDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    with structlog.contextvars.bound_contextvars(run_id="r1"):
        record_llm_call(step="matching.score", response=_response())

    _flush(_FailingDB())  # telemetry must never fail the pipeline it measures

    assert "r1" not in llm_cost._ACCUMULATORS  # and never double-counts later


def test_persist_swallows_a_client_that_cannot_be_constructed():
    """Callers pass a factory and flush from a `finally`; a lazy
    firestore.Client() runs google.auth.default(), which can raise on a
    transient ADC failure. That must not escape into the caller."""

    def _exploding_factory():
        raise RuntimeError("could not determine default credentials")

    _flush(_exploding_factory)


def test_persist_accepts_a_client_factory():
    db = _FakeDB()
    _flush(lambda: db, runner="matching")
    assert db.written["path"] == ("users", "u1", "runs", "r1")


def test_persist_runs_the_sync_client_write_off_the_event_loop():
    """API routes hold a sync client, whose set() blocks on network I/O — it
    has to go to a thread, not stall the loop the cycle is running on."""

    class _SyncDB:
        def __init__(self):
            self.written = {}

        def collection(self, name):
            return self

        def document(self, doc_id):
            return self

        def set(self, doc, merge=False):
            self.written["doc"] = doc
            self.written["thread"] = threading.current_thread()
            return object()  # a WriteResult, not a coroutine

    db = _SyncDB()
    _flush(db, runner="auto_discovery")
    assert db.written["doc"]["run_id"] == "r1"
    assert db.written["thread"] is not threading.main_thread()


# ------------------------------------------------- job-doc spend attribution


def _job() -> Job:
    return Job(
        id="j1",
        user_id="u1",
        source="greenhouse",
        source_id="123",
        company="Acme",
        title="Staff Software Engineer",
        url="https://boards.greenhouse.io/acme/jobs/123",
        jd_raw="Build things.",
        discovered_at=datetime.now(UTC),
    )


def _match(overall: float) -> JobMatch:
    return JobMatch(
        job_id="j1",
        overall_score=overall,
        breakdown=ScoreBreakdown(
            role_fit=overall,
            qualifications_match=overall,
            seniority_match=overall,
            comp_alignment=50,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="ok",
        recommendation="apply",
    )


class _CapturingRef:
    def __init__(self):
        self.updates: list[dict] = []

    async def update(self, fields: dict) -> None:
        self.updates.append(fields)


def test_persist_result_stamps_the_run_that_paid_for_the_score():
    ref = _CapturingRef()
    with structlog.contextvars.bound_contextvars(run_id="r1"):
        outcome = asyncio.run(score.persist_result(ref, _job(), _match(85)))

    assert outcome == "scored"
    written = ref.updates[0]
    assert written["scored_run_id"] == "r1"
    assert written["scored_at"].endswith("+00:00")


def test_persist_result_outside_a_run_stamps_a_null_run_id():
    ref = _CapturingRef()
    asyncio.run(score.persist_result(ref, _job(), _match(85)))
    assert ref.updates[0]["scored_run_id"] is None


def test_persist_result_tombstones_under_the_paying_run():
    # Most of a cycle's Pro spend ends up in tombstones, not job docs, so the
    # discard path is the one that most needs the stamp.
    stones = {}

    class _TombstoneDoc:
        async def set(self, doc):
            stones.update(doc)

    class _TombstoneCollection:
        def document(self, job_id):
            return _TombstoneDoc()

    class _UserRef:
        def collection(self, name):
            assert name == "discarded_jobs"
            return _TombstoneCollection()

    class _DiscardingRef:
        # persist_result walks ref.parent.parent to reach the user doc.
        parent = SimpleNamespace(parent=_UserRef())

        async def delete(self):
            pass

    with structlog.contextvars.bound_contextvars(run_id="r1"):
        outcome = asyncio.run(score.persist_result(_DiscardingRef(), _job(), _match(0)))

    assert outcome == "discarded"
    assert stones["scored_run_id"] == "r1"


def _flush_site_harness(monkeypatch, *, counts: dict, cost_calls: int = 1):
    """Patch every seam of run_discovery_cycle except the cost flush itself."""
    flushes: list[tuple] = []
    state: dict = {}

    async def fake_run_discovery(user_id):
        for _ in range(cost_calls):
            record_llm_call(step="matching.score", response=_response())
        return {
            "jobs": [],
            "jobs_by_platform": {},
            "failures": [],
            "empty_boards": [],
        }

    async def fake_load_prefs(user_id):
        return None

    async def fake_persist_new_jobs(jobs):
        return 0

    async def fake_score(user_id):
        return counts

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        flushes.append((user_id, run_id, kw))

    class _StateWriter:
        def set(self, doc, merge=False):
            state.update(doc)

    monkeypatch.setattr(discovery, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(discovery, "load_job_preferences", fake_load_prefs)
    monkeypatch.setattr(discovery, "prefilter_jobs", lambda jobs, prefs: (jobs, {}))
    monkeypatch.setattr(discovery, "persist_new_jobs", fake_persist_new_jobs)
    monkeypatch.setattr(discovery, "score_pending_jobs", fake_score)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    monkeypatch.setattr(discovery, "_user_ref", lambda uid: _StateWriter())
    monkeypatch.delenv("QUEUE_MODE", raising=False)
    return flushes, state


def test_discovery_cycle_flushes_cost_and_reports_it_in_metrics(monkeypatch):
    counts = {"scored": 2, "discarded": 5, "failed": 1, "pending": 8}
    flushes, state = _flush_site_harness(monkeypatch, counts=counts)

    asyncio.run(discovery.run_discovery_cycle("u1", trigger="manual"))

    one_call = (1000 * 2.00 + 500 * 12.00) / 1_000_000
    # The UI reads cost_usd off discovery_state.last_discovery.
    assert state["discovery_state"]["last_discovery"]["cost_usd"] == pytest.approx(
        round(one_call, 6)
    )
    user_id, _run_id, meta = flushes[0]
    assert user_id == "u1"
    assert meta["runner"] == "auto_discovery" and meta["trigger"] == "manual"
    assert meta["started_at"]
    # Key names are the ledger's contract; `pending`, not `reserved`.
    assert meta["jobs"] == {
        "pending": 8,
        "scored": 2,
        "discarded": 5,
        "failed": 1,
    }


def test_discovery_cycle_flushes_even_when_the_cycle_fails(monkeypatch):
    flushes, _state = _flush_site_harness(monkeypatch, counts={})

    async def explode(user_id):
        record_llm_call(step="matching.score", response=_response())
        raise RuntimeError("board fetch died")

    monkeypatch.setattr(discovery, "run_discovery", explode)

    asyncio.run(discovery.run_discovery_cycle("u1"))  # swallowed, as designed

    # The run still paid for that call, and zeros stand in for counts the
    # cycle never got to compute.
    assert flushes[0][2]["jobs"] == {
        "pending": 0,
        "scored": 0,
        "discarded": 0,
        "failed": 0,
    }


def test_discovery_cycle_survives_a_ledger_client_failure(monkeypatch):
    """The flush runs in a finally; if it raised, /tasks/discovery would 500
    and Cloud Tasks would hot-retry the whole paid cycle."""
    _flush_site_harness(monkeypatch, counts={"scored": 0, "discarded": 0, "failed": 0})
    monkeypatch.setattr(
        discovery, "_client", lambda: (_ for _ in ()).throw(RuntimeError("no ADC"))
    )
    # Restore the real flush: the point is that it eats the client failure.
    monkeypatch.setattr(discovery, "persist_run_cost", persist_run_cost)

    asyncio.run(discovery.run_discovery_cycle("u1"))


def test_sweep_cycle_flushes_cost(monkeypatch):
    flushes: list[tuple] = []

    async def fake_sweep(user_id):
        return {"checked": 3, "removed": 1, "boards_failed": 0}

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        flushes.append((user_id, run_id, kw))

    class _StateWriter:
        def set(self, doc, merge=False):
            pass

    monkeypatch.setattr(discovery, "sweep_postings", fake_sweep)
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    monkeypatch.setattr(discovery, "_user_ref", lambda uid: _StateWriter())

    asyncio.run(discovery.run_sweep_cycle("u1", trigger="cron"))

    assert flushes[0][2]["runner"] == "liveness_sweep"


def test_tombstone_run_id_is_explicit_not_ambient():
    """`cli.purge_discarded` reuses discard_tombstone for jobs scored long
    ago; its own (free) run must not claim their spend."""
    with structlog.contextvars.bound_contextvars(run_id="purge-run"):
        backfilled = score.discard_tombstone(_job(), _match(0))
        carried = score.discard_tombstone(
            _job(), _match(0), scored_run_id="the-run-that-paid"
        )
    assert backfilled["scored_run_id"] is None
    assert carried["scored_run_id"] == "the-run-that-paid"
