# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Resumable batch runs (Phase C): start/resume state machine + claims.

Pins the contracts that make fire-and-forget safe: paid batches are only
*submitted* inline (never awaited), every position survives in the
``batch_runs`` doc, ingestion joins on content so it needs no memory of the
submitting process, and claims stop racing resumers from double-submitting a
paid Pro stage.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from google.api_core.exceptions import FailedPrecondition
from google.genai import types

import tools.matching.batch_runs as batch_runs
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from obs.llm_cost import reset_run_cost, run_cost_snapshot
from obs.logging import current_run_id
from tools.matching import budget


def _job(job_id="j1", jd_raw="Build things at Acme.", parsed=None) -> Job:
    job = Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id="123",
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=jd_raw,
        discovered_at=datetime.now(UTC),
    )
    job.jd_parsed = parsed
    return job


def _parsed(family="engineering") -> ParsedJD:
    return ParsedJD(role_family=family, seniority="staff", summary="Build.")


def _match_json(job_id, score=80.0) -> str:
    return JobMatch(
        job_id=job_id,
        overall_score=score,
        breakdown=ScoreBreakdown(
            role_fit=80,
            qualifications_match=80,
            seniority_match=80,
            comp_alignment=50,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="ok",
        recommendation="apply",
    ).model_dump_json()


def _line(request_text, response_text) -> dict:
    """One batch output line: echoed request + model response."""
    return {
        "request": {"contents": [{"role": "user", "parts": [{"text": request_text}]}]},
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": response_text}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        },
    }


PROFILE = SimpleNamespace(
    preferences=SimpleNamespace(target_role_families=["engineering"])
)


class _FakeRunRef:
    def __init__(self, run_id, store, lose_claim_race=False):
        self.id = run_id
        self._store = store
        self._lose_claim_race = lose_claim_race

    async def set(self, doc):
        self._store[self.id] = dict(doc)

    async def update(self, fields, option=None):
        if option is not None and self._lose_claim_race:
            raise FailedPrecondition("lost the claim race")
        self._store.setdefault(self.id, {}).update(fields)

    async def get(self):
        return _FakeSnap(self)


class _FakeSnap:
    update_time = "server-time-1"

    def __init__(self, ref):
        self.reference = ref
        self.id = ref.id

    def to_dict(self):
        return dict(self.reference._store[self.reference.id])


class _FakeQuery:
    def __init__(self, refs):
        self._refs = refs

    def where(self, filter=None):
        return self

    def limit(self, n):
        return self

    async def stream(self):
        for ref in self._refs:
            yield _FakeSnap(ref)


class _FakeDB:
    """Just enough Firestore for the batch_runs collection."""

    def __init__(self, refs=None):
        self.store = {}
        self._refs = refs or []

    def collection(self, name):
        assert name == batch_runs.COLLECTION
        return self

    def document(self, tag):
        return _FakeRunRef(tag, self.store)

    def where(self, filter=None):
        return _FakeQuery(self._refs)

    def write_option(self, last_update_time=None):
        return ("precondition", last_update_time)


@pytest.fixture
def harness(monkeypatch, unlimited_budget):
    """Patch every external seam; return the recorders.

    ``unlimited_budget`` gets ``start()`` past the per-user scoring cap — what
    the cap does to it is pinned in ``test_scoring_budget.py``.
    """
    rec = SimpleNamespace(
        submitted=[],  # (model, n_lines, gcs_dir, display_name)
        uploaded={},  # gcs_path -> text
        cached=[],  # store_many payload keys
        jd_persisted=[],  # job ids whose parse was persisted
        results=[],  # (job_id, overall_score)
        cache_hits={},  # lookup_many return value
        online_calls=[],
        cost_flushes=[],  # (user_id, run_id, meta) banked by persist_run_cost
        banked=[],  # the accumulated totals each flush would have written
    )

    async def fake_persist_run_cost(db, user_id, run_id, **meta):
        rec.cost_flushes.append((user_id, run_id, meta))
        # Capture what the real flush would have banked, then release the
        # entry exactly as it does — otherwise spend bleeds between tests.
        rec.banked.append(run_cost_snapshot(run_id))
        reset_run_cost(run_id)

    async def fake_submit(*, model, lines, gcs_dir, display_name):
        rec.submitted.append((model, len(lines), gcs_dir, display_name))
        return f"batch/{len(rec.submitted)}"

    async def fake_upload(gcs_path, text, **kw):
        rec.uploaded[gcs_path] = text

    async def fake_lookup_many(db, texts):
        return {t: rec.cache_hits[t] for t in texts if t in rec.cache_hits}

    async def fake_store_many(db, parses, *, model):
        rec.cached.extend(parses)

    async def fake_persist_jd_parsed(ref, job):
        rec.jd_persisted.append(job.id)

    async def fake_persist_result(ref, job, match):
        rec.results.append((job.id, match.overall_score))
        return "discarded" if match.overall_score <= 20 else "scored"

    monkeypatch.setattr(batch_runs, "submit_batch", fake_submit)
    monkeypatch.setattr(batch_runs, "upload_text", fake_upload)
    monkeypatch.setattr(batch_runs, "persist_jd_parsed", fake_persist_jd_parsed)
    monkeypatch.setattr(batch_runs, "persist_result", fake_persist_result)
    monkeypatch.setattr(batch_runs, "persist_run_cost", fake_persist_run_cost)
    monkeypatch.setattr(batch_runs.jd_cache, "lookup_many", fake_lookup_many)
    monkeypatch.setattr(batch_runs.jd_cache, "store_many", fake_store_many)
    monkeypatch.setattr(batch_runs, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch_runs, "build_match_context", lambda p: "CTX")
    monkeypatch.setattr(batch_runs, "build_match_job_block", lambda j: f"BLOCK-{j.id}")
    return rec


def _patch_pending(monkeypatch, pending):
    async def fake_load(db, user_id, limit=None):
        return PROFILE, pending

    monkeypatch.setattr(batch_runs, "load_profile_and_pending", fake_load)


# ---------------------------------------------------------------- start()


def test_start_below_min_pending_does_nothing(harness, monkeypatch):
    db = _FakeDB()
    _patch_pending(monkeypatch, [(_FakeRunRef("x", {}), _job())])

    result = asyncio.run(batch_runs.start("u1", min_pending=50, db=db))

    assert result == {
        "started": False,
        "pending": 1,
        # The reservation was taken before the backlog was known, and the
        # unlimited_budget fixture grants a full cycle; start() gives it all
        # back when it submits nothing.
        "budget_granted": budget.DEFAULT_PER_CYCLE,
        "budget_remaining_cycle": 0,
        "budget_remaining_day": budget.DEFAULT_PER_DAY - budget.DEFAULT_PER_CYCLE,
        "budget_capped": False,
    }
    assert db.store == {} and harness.submitted == []


def test_start_submits_parse_batch_and_records_run(harness, monkeypatch):
    db = _FakeDB()
    _patch_pending(monkeypatch, [(object(), _job("j1")), (object(), _job("j2"))])

    result = asyncio.run(batch_runs.start("u1", db=db))

    assert result["started"] is True and result["stage"] == "parse"
    model, n_lines, _gcs_dir, display_name = harness.submitted[0]
    assert model == batch_runs.BATCH_FLASH_MODEL
    assert n_lines == 1  # identical jd_raw dedupes to one request
    assert display_name == f"hermes-parse-{result['run']}"
    doc = db.store[result["run"]]
    assert doc["state"] == "running" and doc["stage"] == "parse"
    assert doc["job_name"] == "batch/1"
    # The claim taken at creation is released once the job name is durable.
    assert doc["claimed_at"] is None


def test_start_with_cache_hits_goes_straight_to_score(harness, monkeypatch):
    job = _job("j1")
    harness.cache_hits[job.jd_raw] = _parsed()
    db = _FakeDB()
    _patch_pending(monkeypatch, [(object(), job)])

    result = asyncio.run(batch_runs.start("u1", db=db))

    assert result["stage"] == "score"
    assert harness.jd_persisted == ["j1"]  # the free hit became durable
    model, n_lines, _gcs_dir, _ = harness.submitted[0]
    assert model == batch_runs.BATCH_PRO_MODEL and n_lines == 1
    # The context travels with the run so ingest can strip the exact prefix.
    assert (
        harness.uploaded[f"{db.store[result['run']]['gcs_root']}/score/context.txt"]
        == "CTX"
    )
    assert db.store[result["run"]]["stage"] == "score"


def test_start_all_out_of_family_completes_without_llm(harness, monkeypatch):
    job = _job("j1", parsed=_parsed(family="sales"))
    db = _FakeDB()
    _patch_pending(monkeypatch, [(object(), job)])

    result = asyncio.run(batch_runs.start("u1", db=db))

    assert result["stage"] == "done"
    assert harness.submitted == []  # no batch was ever paid for
    assert harness.results == [("j1", 0)]  # OUT_OF_FAMILY tombstone
    doc = db.store[result["run"]]
    assert doc["state"] == "done" and doc["counts"]["discarded"] == 1


# --------------------------------------------------------------- resume()


def _running_doc(stage="parse", **extra):
    return {
        "user_id": "u1",
        "state": "running",
        "stage": stage,
        "job_name": "batch/1",
        "gcs_root": "gs://test-bucket/vertex-batch/r1",
        "counts": {"scored": 0, "discarded": 0, "failed": 0, "parse_failed": 0},
        "claimed_at": None,
        **extra,
    }


def _resume(db):
    return asyncio.run(batch_runs.resume(db=db))


def _patch_vertex_state(monkeypatch, state, error=None):
    async def fake_get(name):
        return SimpleNamespace(name=name, state=state, error=error)

    monkeypatch.setattr(batch_runs, "get_batch_job", fake_get)


def test_resume_leaves_running_jobs_alone(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc()
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_RUNNING)

    summary = _resume(db)

    assert summary["running"] == 1 and summary["advanced"] == 0
    assert store["r1"]["state"] == "running"


def test_resume_skips_recently_claimed_runs(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(
        claimed_at=datetime.now(UTC).isoformat()  # someone else is mid-ingest
    )
    db = _FakeDB(refs=[ref])

    async def explode(name):  # the Vertex poll must not even happen
        raise AssertionError("polled a claimed run")

    monkeypatch.setattr(batch_runs, "get_batch_job", explode)

    summary = _resume(db)

    assert summary["running"] == 1


def test_resume_reclaims_after_ttl(harness, monkeypatch):
    stale = datetime.now(UTC) - timedelta(seconds=batch_runs._CLAIM_TTL_SECONDS + 60)
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score", claimed_at=stale.isoformat())
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)

    async def fake_download(path):
        return "CTX"

    async def fake_fetch(gcs_dir):
        return []

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)
    _patch_pending(monkeypatch, [])

    summary = _resume(db)

    assert summary["completed"] == 1
    assert store["r1"]["state"] == "done"


def test_resume_loses_claim_race_and_backs_off(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store, lose_claim_race=True)
    store["r1"] = _running_doc()
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)

    summary = _resume(db)

    assert summary["running"] == 1  # counted as in someone else's hands
    assert store["r1"]["state"] == "running"


def test_resume_marks_vertex_failure(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc()
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_FAILED, error="quota")

    summary = _resume(db)

    assert summary["failed"] == 1
    assert store["r1"]["state"] == "failed" and "quota" in store["r1"]["error"]


def test_resume_ingests_parse_and_submits_score(harness, monkeypatch):
    """The full parse→score advance, joined purely on content."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="parse")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)

    in_family = _job("j1", jd_raw="ENG JD")
    out_family = _job("j2", jd_raw="SALES JD")
    bad_line = _job("j3", jd_raw="BROKEN JD")
    _patch_pending(
        monkeypatch,
        [(object(), in_family), (object(), out_family), (object(), bad_line)],
    )

    async def fake_fetch(gcs_dir):
        assert gcs_dir.endswith("/parse")
        return [
            _line("ENG JD", _parsed("engineering").model_dump_json()),
            _line("SALES JD", _parsed("sales").model_dump_json()),
            _line("BROKEN JD", "not json"),
        ]

    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    summary = _resume(db)

    assert summary["advanced"] == 1
    # Fresh parses became shared property + durable on the job docs.
    assert sorted(harness.cached) == ["ENG JD", "SALES JD"]
    assert sorted(harness.jd_persisted) == ["j1", "j2"]
    # Out-of-family tombstoned locally; only the in-family job goes to Pro.
    assert harness.results == [("j2", 0)]
    model, n_lines, _gcs_dir, _ = harness.submitted[-1]
    assert model == batch_runs.BATCH_PRO_MODEL and n_lines == 1
    doc = store["r1"]
    assert doc["stage"] == "score" and doc["job_name"] == "batch/1"
    assert doc["counts"]["parse_failed"] == 1  # the broken line
    assert doc["counts"]["discarded"] == 1
    assert doc["claimed_at"] is None  # released for the next tick


def test_score_stage_submits_only_the_jobs_the_run_reserved(harness, monkeypatch):
    """The ingest reload is a superset of the run — the join needs it — and
    every parsed job in it would otherwise become a paid Pro request. A run
    that reserved one slot must submit one request, not one per parsed job
    left pending by every earlier run."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="parse", job_ids=["j1"])
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)

    owned = _job("j1", jd_raw="ENG JD")
    # Parsed, in-family, still pending — the shape a failed earlier run leaves
    # behind, and a permanent floor if the score stage sweeps it in.
    stranger_a = _job("j2", jd_raw="OTHER JD", parsed=_parsed())
    stranger_b = _job("j3", jd_raw="THIRD JD", parsed=_parsed())
    _patch_pending(
        monkeypatch,
        [(object(), owned), (object(), stranger_a), (object(), stranger_b)],
    )

    async def fake_fetch(gcs_dir):
        return [_line("ENG JD", _parsed("engineering").model_dump_json())]

    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    summary = _resume(db)

    assert summary["advanced"] == 1
    model, n_lines, _gcs_dir, _ = harness.submitted[-1]
    assert (model, n_lines) == (batch_runs.BATCH_PRO_MODEL, 1)
    assert harness.jd_persisted == ["j1"]
    assert harness.results == []  # nobody else's job was touched at all


def test_score_stage_of_a_legacy_run_falls_back_to_what_it_parsed(harness, monkeypatch):
    """batch_runs docs written before job_ids existed (there are live ones)
    score only the jobs their own batch echoed — a subset of what the run
    reserved, never a superset. The remainder is picked up by a later start(),
    which is itself budgeted."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="parse")  # no job_ids
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)

    mine = _job("j1", jd_raw="ENG JD")
    stranger = _job("j2", jd_raw="OTHER JD", parsed=_parsed())
    _patch_pending(monkeypatch, [(object(), mine), (object(), stranger)])

    async def fake_fetch(gcs_dir):
        return [_line("ENG JD", _parsed("engineering").model_dump_json())]

    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    _resume(db)

    model, n_lines, _gcs_dir, _ = harness.submitted[-1]
    assert (model, n_lines) == (batch_runs.BATCH_PRO_MODEL, 1)


def test_start_records_the_jobs_it_reserved_on_the_run_doc(harness, monkeypatch):
    db = _FakeDB()
    _patch_pending(monkeypatch, [(object(), _job("j1")), (object(), _job("j2"))])

    result = asyncio.run(batch_runs.start("u1", db=db))

    assert db.store[result["run"]]["job_ids"] == ["j1", "j2"]


def test_resume_ingests_score_and_completes(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)

    keeper = _job("j1", parsed=_parsed())
    tossed = _job("j2", parsed=_parsed())
    failed = _job("j3", parsed=_parsed())
    _patch_pending(
        monkeypatch, [(object(), keeper), (object(), tossed), (object(), failed)]
    )

    async def fake_download(path):
        assert path.endswith("/score/context.txt")
        return "CTX"

    async def fake_fetch(gcs_dir):
        assert gcs_dir.endswith("/score")
        return [
            _line("CTX\n\nBLOCK-j1", _match_json("j1", 85)),
            _line("CTX\n\nBLOCK-j2", _match_json("j2", 10)),
            _line("CTX\n\nBLOCK-j3", "not json"),
        ]

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    summary = _resume(db)

    assert summary["completed"] == 1
    assert sorted(harness.results) == [("j1", 85.0), ("j2", 10.0)]
    doc = store["r1"]
    assert doc["state"] == "done" and doc["stage"] == "done"
    assert doc["counts"] == {
        "scored": 1,
        "discarded": 1,
        "failed": 1,  # the unparseable response line
        "parse_failed": 0,
    }


def test_resume_marks_run_without_job_name_as_orphaned(harness, monkeypatch):
    stale = datetime.now(UTC) - timedelta(seconds=batch_runs._CLAIM_TTL_SECONDS + 60)
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(job_name=None, claimed_at=stale.isoformat())
    db = _FakeDB(refs=[ref])

    summary = _resume(db)

    assert summary["failed"] == 1
    assert store["r1"]["state"] == "failed"
    assert "hermes-*-r1" in store["r1"]["error"]


# ------------------------------------------- cost attribution across the seam


def test_start_stamps_the_originating_run_id(harness, monkeypatch):
    db = _FakeDB()
    _patch_pending(monkeypatch, [(object(), _job("j1"))])

    with structlog.contextvars.bound_contextvars(run_id="cycle-1"):
        result = asyncio.run(batch_runs.start("u1", db=db))

    assert db.store[result["run"]]["origin_run_id"] == "cycle-1"


def test_resume_prices_the_batch_under_the_run_that_ordered_it(harness, monkeypatch):
    """A batch's tokens are only priced at ingest, on a later worker tick —
    so the ingest must run under the submitting cycle's run_id, not the
    tick's, or one cycle's spend scatters across whichever passes ingested it.
    """
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])

    bound_during_ingest = []

    async def fake_download(path):
        return "CTX"

    async def fake_fetch(gcs_dir):
        bound_during_ingest.append(current_run_id())
        return [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))]

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    with structlog.contextvars.bound_contextvars(run_id="worker-tick"):
        summary = _resume(db)
        # The tick's own context is restored once the run is ingested.
        assert current_run_id() == "worker-tick"

    assert summary["completed"] == 1
    assert bound_during_ingest == ["cycle-1"]
    assert harness.cost_flushes == [("u1", "cycle-1", {"batch_run": "r1"})]


def test_resume_ingests_runs_predating_origin_run_id(harness, monkeypatch):
    """One live July batch_runs doc has no origin_run_id; it must still
    ingest — just unattributed, exactly as it does today."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score")  # no origin_run_id
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])

    async def fake_download(path):
        return "CTX"

    async def fake_fetch(gcs_dir):
        return [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))]

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    summary = _resume(db)

    assert summary["completed"] == 1
    assert harness.results == [("j1", 85.0)]
    assert harness.cost_flushes == []  # nothing to attribute it to


def test_resume_banks_cost_when_the_ingest_dies_after_pricing(harness, monkeypatch):
    """The failure has to land *after* join_score_responses has priced the
    output, which is the case the finally exists for: that spend is real and
    already charged, so losing it would under-report the run."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])

    async def fake_download(path):
        return "CTX"

    async def fake_fetch(gcs_dir):
        return [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))]

    async def exploding_persist(ref, job, match):
        raise RuntimeError("Firestore died after the batch was priced")

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)
    monkeypatch.setattr(batch_runs, "persist_result", exploding_persist)

    summary = _resume(db)

    assert summary["completed"] == 0  # the ingest did not finish
    assert store["r1"]["state"] == "running"  # left claimed for the retry
    assert harness.cost_flushes == [("u1", "cycle-1", {"batch_run": "r1"})]
    # The point of the test: real spend was banked, not an empty flush.
    banked = harness.banked[0]
    assert banked["calls"] == 1
    assert banked["cost_usd"] > 0
    assert banked["by_step"]["matching.score"]["calls"] == 1


def test_resume_does_not_bank_cost_for_a_failed_vertex_job(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_FAILED, error="quota")

    summary = _resume(db)

    assert summary["failed"] == 1
    assert harness.cost_flushes == []


# ------------------------------------------------- score_or_start_run()


def test_score_or_start_run_small_backlog_stays_online(monkeypatch):
    async def fake_start(user_id, *, min_pending):
        assert min_pending == batch_runs.BATCH_MIN_PENDING
        return {"started": False, "pending": 3}

    online = {"scored": 2, "discarded": 1, "failed": 0, "pending": 3}

    async def fake_online(user_id):
        return online

    monkeypatch.setattr(batch_runs, "start", fake_start)
    monkeypatch.setattr(batch_runs, "score_pending_jobs", fake_online)

    assert asyncio.run(batch_runs.score_or_start_run("u1")) == online


def test_score_or_start_run_big_backlog_returns_run_tag(monkeypatch):
    async def fake_start(user_id, *, min_pending):
        return {
            "started": True,
            "run": "r9",
            "stage": "parse",
            "pending": 904,
            "counts": {"scored": 0, "discarded": 7, "failed": 0, "parse_failed": 0},
        }

    monkeypatch.setattr(batch_runs, "start", fake_start)

    counts = asyncio.run(batch_runs.score_or_start_run("u1"))

    assert counts["batch_run"] == "r9"
    assert counts["pending"] == 904 and counts["discarded"] == 7
