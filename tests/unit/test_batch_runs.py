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
        doc = self._store.setdefault(self.id, {})
        for path, value in fields.items():
            # Firestore reads a dotted key as a path into a nested map and
            # merges at the leaf, leaving sibling keys alone — which is the
            # whole point of writing the per-leg cost marker that way.
            head, _, tail = path.partition(".")
            if tail:
                doc.setdefault(head, {})[tail] = value
            else:
                doc[path] = value

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
    """Streams the given refs, honouring ``==`` and ``in`` field filters.

    The filters used to be ignored, which was fine while every query in this
    module asked the same question. ``outstanding_committed`` asks a
    *different* one (which states, whose runs), and a fake that answers "all
    of them" would let a run this function must skip pass the test."""

    def __init__(self, refs, filters=()):
        self._refs = refs
        self._filters = tuple(filters)

    def where(self, filter=None):
        return _FakeQuery(self._refs, self._filters + ((filter,) if filter else ()))

    def limit(self, n):
        return self

    def _matches(self, doc) -> bool:
        for f in self._filters:
            value = doc.get(f.field_path)
            if f.op_string == "in":
                if value not in f.value:
                    return False
            elif value != f.value:
                return False
        return True

    async def stream(self):
        for ref in self._refs:
            snap = _FakeSnap(ref)
            if self._matches(snap.to_dict() or {}):
                yield snap


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
        # The filter is *kept*, not dropped. It used to be discarded here and
        # only honoured on chained `.where` calls, so the first filter of
        # every query — which for `outstanding_committed` is the state
        # filter, the whole point of `OUTSTANDING_STATES` — silently matched
        # everything. A fake that answers a question nobody asked is how a
        # test passes for the wrong reason.
        return _FakeQuery(self._refs).where(filter=filter)

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
        # (job_id, profile): geo shadow recording is on only where a profile
        # was handed down, so this is what says which paths are instrumented.
        persist_profiles=[],
        # (job_id, geo_gate): non-None only for a gate-enforced skip, which
        # carries its verdict explicitly instead of through the shadow path.
        persist_gates=[],
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

    async def fake_persist_result(ref, job, match, *, profile=None, geo_gate=None):
        rec.results.append((job.id, match.overall_score))
        rec.persist_profiles.append((job.id, profile))
        rec.persist_gates.append((job.id, geo_gate))
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
        # back when it submits nothing — so the *reported* remainder is the
        # untouched budget, not the momentary debit. This used to read 0 and
        # PER_DAY - PER_CYCLE, i.e. the Profile card telling a user a whole
        # cycle was gone on a run that never submitted a job.
        "budget_granted": budget.DEFAULT_PER_CYCLE,
        "budget_remaining_cycle": budget.DEFAULT_PER_CYCLE,
        "budget_remaining_day": budget.DEFAULT_PER_DAY,
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


# ------------------------------------------------- geo gate shadow recording


def test_score_ingest_hands_persist_the_profile(harness, monkeypatch):
    """The resumable path is the cheap path, so it is the one most likely to
    ship silently uninstrumented — ``_ingest_score`` used to throw the profile
    away outright (``_, pending = ...``). Without it every geo verdict on this
    path is simply never recorded, and the coverage measurement this whole
    phase exists for is quietly taken over the online scorer alone."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])

    async def fake_download(path):
        return "CTX"

    async def fake_fetch(gcs_dir):
        return [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))]

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    assert _resume(db)["completed"] == 1
    assert harness.persist_profiles == [("j1", PROFILE)]


def test_out_of_family_tombstones_are_not_geo_recorded(harness, monkeypatch):
    """The submit-time tombstones never reached Pro — the free family filter
    rejected them — so there is no Pro decision for the gate to be scored
    against, and handing them a profile would only pollute the corpus."""
    db = _FakeDB()
    _patch_pending(
        monkeypatch, [(object(), _job("j1", parsed=_parsed(family="sales")))]
    )

    asyncio.run(batch_runs.start("u1", db=db))

    assert harness.persist_profiles == [("j1", None)]


def test_started_batch_run_reports_the_same_keys_as_the_online_scorer(monkeypatch):
    """Both branches of score_or_start_run feed one caller (the discovery
    cycle's metrics), so they have to agree on their key set."""

    async def fake_start(user_id, *, min_pending, cycle_id=budget.CURRENT_RUN):
        return {
            "started": True,
            "run": "r9",
            "stage": "parse",
            "pending": 904,
            "counts": {"scored": 0, "discarded": 7, "failed": 0, "parse_failed": 0},
        }

    monkeypatch.setattr(batch_runs, "start", fake_start)

    counts = asyncio.run(batch_runs.score_or_start_run("u1"))

    # Zero-so-far, exactly like scored/failed: the verdicts are recorded by
    # whichever worker tick ingests the run.
    assert counts["geo_ineligible"] == 0 and counts["geo_abstain"] == 0


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
    assert harness.cost_flushes == [
        (
            "u1",
            "cycle-1",
            {
                "batch_run": "r1",
                "jobs": {
                    "scored": 1,
                    "discarded": 0,
                    "failed": 0,
                    "parse_failed": 0,
                },
            },
        )
    ]


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

    async def exploding_persist(ref, job, match, *, profile=None):
        raise RuntimeError("Firestore died after the batch was priced")

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)
    monkeypatch.setattr(batch_runs, "persist_result", exploding_persist)

    summary = _resume(db)

    assert summary["completed"] == 0  # the ingest did not finish
    assert store["r1"]["state"] == "running"  # left claimed for the retry
    # No ``jobs``: the leg never returned its counts, so the money is banked
    # and the outcome breakdown is simply absent rather than guessed at.
    assert harness.cost_flushes == [
        ("u1", "cycle-1", {"batch_run": "r1", "jobs": None})
    ]
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


# ------------------------------------------- per-leg job counts on the ledger


def _patch_score_ingest(monkeypatch, lines):
    async def fake_download(path):
        return "CTX"

    async def fake_fetch(gcs_dir):
        return lines

    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)


def _flushed_jobs(harness):
    """The ``jobs=`` map the single cost flush of a resume pass carried."""
    assert len(harness.cost_flushes) == 1, harness.cost_flushes
    return harness.cost_flushes[0][2]["jobs"]


def test_score_leg_banks_the_jobs_it_scored(harness, monkeypatch):
    """The whole point of item 1: without ``jobs=``, ``jobs.scored`` stays 0
    on the ledger doc for every batch run, so cost-per-scored-job is
    underivable for exactly the runs big enough to route through a batch."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(
        monkeypatch,
        [
            (object(), _job("j1", parsed=_parsed())),
            (object(), _job("j2", parsed=_parsed())),
            (object(), _job("j3", parsed=_parsed())),
        ],
    )
    _patch_score_ingest(
        monkeypatch,
        [
            _line("CTX\n\nBLOCK-j1", _match_json("j1", 85)),
            _line("CTX\n\nBLOCK-j2", _match_json("j2", 10)),  # tombstoned
            _line("CTX\n\nBLOCK-j3", "not json"),  # failed line
        ],
    )

    _resume(db)

    assert _flushed_jobs(harness) == {
        "scored": 1,
        "discarded": 1,
        "failed": 1,
        "parse_failed": 0,
    }


def test_score_leg_banks_a_delta_not_the_running_total(harness, monkeypatch):
    """``run["counts"]`` is cumulative across both legs, and every leaf under
    ``jobs`` is applied with ``firestore.Increment``. Sending the total would
    re-add the parse leg's outcomes on the score leg's flush and roughly
    double the ledger's job counts — so what goes over is this leg's delta."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(
        stage="score",
        origin_run_id="cycle-1",
        # What the parse leg left behind, and already banked itself.
        counts={"scored": 0, "discarded": 7, "failed": 0, "parse_failed": 3},
    )
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    _patch_score_ingest(monkeypatch, [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))])

    _resume(db)

    assert _flushed_jobs(harness) == {
        "scored": 1,
        "discarded": 0,
        "failed": 0,
        "parse_failed": 0,
    }
    # The run doc keeps the cumulative view; only the ledger gets the delta.
    assert store["r1"]["counts"] == {
        "scored": 1,
        "discarded": 7,
        "failed": 0,
        "parse_failed": 3,
    }


def test_parse_leg_banks_its_own_failures_and_free_tombstones(harness, monkeypatch):
    """The parse leg retires out-of-family jobs for free before Pro sees
    them; those are real outcomes of this leg and belong on its flush."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="parse", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(
        monkeypatch,
        [
            (object(), _job("j1", jd_raw="ENG JD")),
            (object(), _job("j2", jd_raw="SALES JD")),
            (object(), _job("j3", jd_raw="BROKEN JD")),
        ],
    )

    async def fake_fetch(gcs_dir):
        return [
            _line("ENG JD", _parsed("engineering").model_dump_json()),
            _line("SALES JD", _parsed("sales").model_dump_json()),
            _line("BROKEN JD", "not json"),
        ]

    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)

    _resume(db)

    assert _flushed_jobs(harness) == {
        "scored": 0,
        "discarded": 1,  # the out-of-family tombstone
        "failed": 0,
        "parse_failed": 1,  # the unparseable line
    }


def test_no_pending_is_banked_from_an_ingest_leg(harness, monkeypatch):
    """``pending`` is a level, not a delta — and the cycle that ordered the
    run already banked it. Incrementing it again per leg would inflate the
    denominator of every cost-per-job number derived from this doc."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    _patch_score_ingest(monkeypatch, [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))])

    _resume(db)

    assert "pending" not in _flushed_jobs(harness)


# ------------------------------------------- banking each leg exactly once


def test_a_successful_leg_marks_itself_banked(harness, monkeypatch):
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(stage="score", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    _patch_score_ingest(monkeypatch, [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))])

    _resume(db)

    assert store["r1"]["cost_banked_at"]["score"]


def test_a_leg_already_banked_is_not_banked_again(harness, monkeypatch):
    """The at-least-once flush: a run that died mid-ingest is retried after
    the claim TTL, re-reads the same GCS output and re-prices the same calls.
    ``Increment`` is not idempotent, so without the marker that spend lands on
    the ledger twice."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(
        stage="score",
        origin_run_id="cycle-1",
        cost_banked_at={"score": "2026-08-30T00:00:00+00:00"},
    )
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    _patch_score_ingest(monkeypatch, [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))])

    summary = _resume(db)

    assert harness.cost_flushes == []
    # Not banking is not the same as not ingesting: the retry still has to
    # finish the run and persist what the first attempt did not.
    assert summary["completed"] == 1
    assert harness.results == [("j1", 85.0)]
    assert store["r1"]["state"] == "done"
    # persist_run_cost is what normally releases the accumulator entry; the
    # skip path has to do it by hand or the bounded map leaks one entry per
    # retried run, carrying re-priced spend into whatever flushes next.
    assert run_cost_snapshot("cycle-1")["calls"] == 0


def test_the_marker_is_per_leg_not_per_run(harness, monkeypatch):
    """One marker for the whole run would let the parse leg's flush suppress
    the score leg's — which is the leg that carries almost all the spend."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(
        stage="score",
        origin_run_id="cycle-1",
        cost_banked_at={"parse": "2026-08-30T00:00:00+00:00"},
    )
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    _patch_score_ingest(monkeypatch, [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))])

    _resume(db)

    assert _flushed_jobs(harness)["scored"] == 1
    # ...and the parse leg's marker survived the score leg's write.
    assert set(store["r1"]["cost_banked_at"]) == {"parse", "score"}


def test_the_marker_is_written_after_the_money_is_banked(harness, monkeypatch):
    """The failure direction, pinned. This ledger feeds a budget cap, so it
    has always preferred over-counting to losing spend. Marking the leg before
    flushing would invert that: a crash in between would leave the leg looking
    banked when nothing was ever written. Bank first, mark second — a failed
    marker write costs a double-count on retry, never a lost dollar."""
    store = {}

    class _MarkerRefusingRef(_FakeRunRef):
        async def update(self, fields, option=None):
            if any(k.startswith("cost_banked_at") for k in fields):
                raise RuntimeError("Firestore died writing the marker")
            return await super().update(fields, option=option)

    ref = _MarkerRefusingRef("r1", store)
    store["r1"] = _running_doc(stage="score", origin_run_id="cycle-1")
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    _patch_score_ingest(monkeypatch, [_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))])

    _resume(db)  # the marker failure is caught by resume's per-run guard

    assert _flushed_jobs(harness)["scored"] == 1  # the money reached the ledger
    assert "cost_banked_at" not in store["r1"]  # the marker did not


# ------------------------------------------------- score_or_start_run()


def test_score_or_start_run_small_backlog_stays_online(monkeypatch):
    seen = {}

    async def fake_start(user_id, *, min_pending, cycle_id=budget.CURRENT_RUN):
        assert min_pending == batch_runs.BATCH_MIN_PENDING
        seen["start"] = cycle_id
        return {"started": False, "pending": 3}

    online = {"scored": 2, "discarded": 1, "failed": 0, "pending": 3}

    async def fake_online(user_id, *, cycle_id=budget.CURRENT_RUN):
        seen["online"] = cycle_id
        return online

    monkeypatch.setattr(batch_runs, "start", fake_start)
    monkeypatch.setattr(batch_runs, "score_pending_jobs", fake_online)

    assert asyncio.run(batch_runs.score_or_start_run("u1")) == online
    # The discovery cycle's default: open a window under the ambient run.
    assert seen == {"start": budget.CURRENT_RUN, "online": budget.CURRENT_RUN}


def test_score_or_start_run_passes_its_cycle_id_to_whichever_arm_runs(monkeypatch):
    """The ad-hoc backlog score passes ``cycle_id=None`` — draw down the open
    window, never open a fresh one. Both arms have to honour it, or "200 per
    cycle" becomes resettable by whichever arm the backlog size happened to
    pick."""
    seen = {}

    async def fake_start(user_id, *, min_pending, cycle_id=budget.CURRENT_RUN):
        seen["start"] = cycle_id
        return {"started": False, "pending": 3}

    async def fake_online(user_id, *, cycle_id=budget.CURRENT_RUN):
        seen["online"] = cycle_id
        return {"scored": 0, "discarded": 0, "failed": 0, "pending": 0}

    monkeypatch.setattr(batch_runs, "start", fake_start)
    monkeypatch.setattr(batch_runs, "score_pending_jobs", fake_online)

    asyncio.run(batch_runs.score_or_start_run("u1", cycle_id=None))

    assert seen == {"start": None, "online": None}


def test_score_or_start_run_big_backlog_returns_run_tag(monkeypatch):
    async def fake_start(user_id, *, min_pending, cycle_id=budget.CURRENT_RUN):
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


# ------------------------------------------------- committed cost (Phase 3)
#
# **Committed spend used to be invisible until ingest.** Google bills a Vertex
# batch when it runs it; our ledger prices it hours later, when a resume pass
# reads the output. So a run submitted and then abandoned was billed and
# recorded nowhere: on 2026-09-26 a ledger doc read `cost_usd: 0.0, calls: 0`
# while a completed batch sat on the invoice.
#
# The fix is a per-leg estimate written onto the *batch_runs* doc — never onto
# the Increment-based ledger doc, which cannot carry it (see
# outstanding_committed's docstring). "Outstanding" is then a query over legs
# with no cost_banked_at marker, not a subtraction that has to be run exactly
# once.


def test_submitting_a_batch_records_its_committed_estimate_before_any_ingest(
    harness, monkeypatch
):
    """T11. In the same write that records the job name — the write that makes
    the batch trackable is the write that records its price, so there is no
    state in which a run is known and its cost is not."""
    db = _FakeDB()
    _patch_pending(
        monkeypatch, [(object(), _job("j1")), (object(), _job("j2", "Other"))]
    )

    result = asyncio.run(batch_runs.start("u1", db=db))

    doc = db.store[result["run"]]
    assert doc["job_name"] == "batch/1"
    committed = doc["committed"]["parse"]
    assert committed["requests"] == 2  # two distinct jd_raw -> two requests
    assert committed["model"] == batch_runs.BATCH_FLASH_MODEL
    assert 0 < committed["usd_low"] < committed["usd_high"]
    assert committed["at"]
    # Nothing has been ingested, so the ledger is still untouched — which is
    # the whole gap this closes.
    assert doc.get("cost_banked_at") is None
    assert harness.cost_flushes == []


def test_the_score_leg_records_its_own_committed_estimate(harness, monkeypatch):
    """The Pro leg is created by a *resume* pass hours after the click, and it
    is the expensive one. Consent captured at the click covers it only because
    the quote is per job over the whole grant rather than per leg."""
    job = _job("j1")
    harness.cache_hits[job.jd_raw] = _parsed()
    db = _FakeDB()
    _patch_pending(monkeypatch, [(object(), job)])

    result = asyncio.run(batch_runs.start("u1", db=db))

    committed = db.store[result["run"]]["committed"]["score"]
    assert committed["model"] == batch_runs.BATCH_PRO_MODEL
    assert committed["requests"] == 1 and committed["usd_low"] > 0


def test_a_second_leg_does_not_clobber_the_first(harness, monkeypatch):
    """The two entries are written by different processes hours apart, so the
    write has to be a dotted-path merge and not a whole-map replace."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(
        stage="parse",
        origin_run_id="cycle-1",
        job_ids=["j1"],
        committed={"parse": {"requests": 1, "usd_low": 0.1, "usd_high": 0.2}},
    )
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    job = _job("j1")
    _patch_pending(monkeypatch, [(object(), job)])

    async def fake_fetch(gcs_dir):
        return [_line(_request_text_for(job), _parsed().model_dump_json())]

    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)
    monkeypatch.setattr(batch_runs, "download_text", _async_value("CTX"))

    _resume(db)

    committed = store["r1"]["committed"]
    assert set(committed) == {"parse", "score"}
    assert committed["parse"]["usd_low"] == 0.1  # untouched


def _async_value(value):
    async def _f(*a, **kw):
        return value

    return _f


def _request_text_for(job) -> str:
    return batch_runs._request_text(batch_runs.build_parse_request(job.jd_raw))


def test_ingesting_a_leg_never_adds_the_committed_estimate_to_actual_cost(
    harness, monkeypatch
):
    """T12. ``llm.cost_usd`` means **actual, priced, ingested** and nothing
    else.

    Adding the estimate to the ledger at submit would have to be subtracted at
    ingest, and ``tools.run_costs`` writes every leaf with ``Increment`` —
    with a documented crash window between banking and marking, so the
    subtraction is not idempotent. The result is negative committed totals or
    double-counted actuals, silently. So the two never meet: this asserts the
    banked figure is the priced tokens alone, and that no committed dollars
    ride along in the flush's metadata either.
    """
    store = {}
    ref = _FakeRunRef("r1", store)
    committed = {"requests": 100, "usd_low": 5.0, "usd_high": 10.0}
    store["r1"] = _running_doc(
        stage="score", origin_run_id="cycle-1", committed={"score": committed}
    )
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(monkeypatch, types.JobState.JOB_STATE_SUCCEEDED)
    _patch_pending(monkeypatch, [(object(), _job("j1", parsed=_parsed()))])
    monkeypatch.setattr(batch_runs, "download_text", _async_value("CTX"))
    monkeypatch.setattr(
        batch_runs,
        "fetch_batch_output",
        _async_value([_line("CTX\n\nBLOCK-j1", _match_json("j1", 85))]),
    )

    _resume(db)

    (banked,) = harness.banked
    # 10 prompt + 5 output tokens of gemini-2.5-flash at the batch rate: cents
    # of a cent, and nowhere near the $5 estimate sitting on the run doc.
    assert banked["cost_usd"] < 0.01
    assert banked["cost_usd"] != committed["usd_low"]
    # ...and nothing named it into the ledger's metadata by another route.
    (_user, _run, meta) = harness.cost_flushes[0]
    assert "committed" not in meta
    assert committed["usd_low"] not in _dollar_values(meta)
    # The estimate stays exactly where it was written.
    assert store["r1"]["committed"]["score"] == committed


def _dollar_values(meta) -> set:
    out = set()
    for value in meta.values():
        if isinstance(value, int | float):
            out.add(float(value))
        elif isinstance(value, dict):
            out |= _dollar_values(value)
    return out


def test_outstanding_committed_excludes_legs_already_banked(harness, monkeypatch):
    """T13. "Outstanding" is *defined* as the legs with no ``cost_banked_at``
    marker — a query, not arithmetic. That is what makes it safe to re-run and
    impossible for a retried ingest to corrupt."""
    store = {}
    refs = [_FakeRunRef("r1", store), _FakeRunRef("r2", store)]
    store["r1"] = _running_doc(
        stage="score",
        committed={
            "parse": {"usd_low": 1.0, "usd_high": 2.0},
            "score": {"usd_low": 8.0, "usd_high": 16.0},
        },
        cost_banked_at={"parse": "2026-09-26T00:00:00+00:00"},
    )
    store["r2"] = _running_doc(
        stage="parse", committed={"parse": {"usd_low": 0.5, "usd_high": 1.0}}
    )
    db = _FakeDB(refs=refs)

    total = asyncio.run(batch_runs.outstanding_committed(db))

    # r1's parse leg is banked — its real cost is on the ledger now, so
    # counting the estimate too would be double-counting the same money.
    assert total["usd_low"] == pytest.approx(8.0 + 0.5)
    assert total["usd_high"] == pytest.approx(16.0 + 1.0)
    assert total["runs"] == 2
    assert all(leg["leg"] != "parse" or leg["run"] != "r1" for leg in total["legs"])
    assert {leg["leg"] for leg in total["legs"]} == {"score", "parse"}


def test_a_failed_run_keeps_its_committed_estimate(harness, monkeypatch):
    """T14. Google billed it. A failed or orphaned run is precisely the case
    this exists for — clearing the figure would erase the honest record of
    money spent and never ingested."""
    store = {}
    ref = _FakeRunRef("r1", store)
    committed = {"parse": {"requests": 9, "usd_low": 0.4, "usd_high": 0.8}}
    store["r1"] = _running_doc(stage="parse", committed=committed)
    db = _FakeDB(refs=[ref])
    _patch_vertex_state(
        monkeypatch, types.JobState.JOB_STATE_FAILED, error="out of quota"
    )

    summary = _resume(db)

    assert summary["failed"] == 1
    assert store["r1"]["state"] == "failed"
    assert store["r1"]["committed"] == committed

    # And it keeps showing up as outstanding, forever, because it is.
    outstanding = asyncio.run(
        batch_runs.outstanding_committed(_FakeDB(refs=[_FakeRunRef("r1", store)]))
    )
    assert outstanding["usd_low"] == pytest.approx(0.4)


def test_an_orphaned_run_keeps_its_committed_estimate(harness, monkeypatch):
    """The other way a run is abandoned: start() died between paying for the
    batch and recording its job name. Exactly the spend nobody could see."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = _running_doc(
        stage="parse", committed={"parse": {"usd_low": 0.4, "usd_high": 0.8}}
    )
    store["r1"]["job_name"] = None
    db = _FakeDB(refs=[ref])

    assert _resume(db)["failed"] == 1
    assert store["r1"]["committed"]["parse"]["usd_low"] == 0.4


def test_a_completed_run_is_not_outstanding(harness):
    """Both its legs are banked, so its *real* cost is on the ledger — adding
    the estimate as well would double-count the same money.

    Note this one passes on the ``cost_banked_at`` filter, not on the state
    filter — which is why the test below exists."""
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = {
        "user_id": "u1",
        "state": "done",
        "committed": {"parse": {"usd_low": 3.0, "usd_high": 6.0}},
        "cost_banked_at": {"parse": "2026-09-26T00:00:00+00:00"},
    }
    db = _FakeDB(refs=[ref])

    total = asyncio.run(batch_runs.outstanding_committed(db))

    assert total == {"usd_low": 0.0, "usd_high": 0.0, "runs": 0, "legs": []}


def test_outstanding_committed_can_be_scoped_to_one_user(harness):
    store = {}
    refs = [_FakeRunRef("mine", store), _FakeRunRef("theirs", store)]
    store["mine"] = _running_doc(committed={"parse": {"usd_low": 1.0, "usd_high": 2.0}})
    store["theirs"] = {
        **_running_doc(committed={"parse": {"usd_low": 99.0, "usd_high": 99.0}}),
        "user_id": "u2",
    }
    db = _FakeDB(refs=refs)

    total = asyncio.run(batch_runs.outstanding_committed(db, user_id="u1"))

    assert total["usd_low"] == pytest.approx(1.0) and total["runs"] == 1


def test_a_done_run_is_excluded_by_state_even_with_an_unbanked_leg(harness):
    """The ``OUTSTANDING_STATES`` filter, on its own.

    Every other test here passes through the ``cost_banked_at`` filter, so
    adding ``"done"`` to ``OUTSTANDING_STATES`` left the whole file green —
    the state filter was doing real work and nothing was checking it. A
    completed run whose marker never landed is exactly the case that tells
    the two filters apart: ``done`` means the pipeline finished and its
    tokens were priced onto the ledger, so its estimate must not be counted
    again no matter what the per-leg markers say.
    """
    store = {}
    ref = _FakeRunRef("r1", store)
    store["r1"] = {
        "user_id": "u1",
        "state": "done",
        "committed": {"score": {"usd_low": 12.0, "usd_high": 24.0}},
        # Deliberately no cost_banked_at at all.
    }
    db = _FakeDB(refs=[ref])

    total = asyncio.run(batch_runs.outstanding_committed(db))

    assert total == {"usd_low": 0.0, "usd_high": 0.0, "runs": 0, "legs": []}
    assert "done" not in batch_runs.OUTSTANDING_STATES
