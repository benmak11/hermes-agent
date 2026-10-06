# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.trace_job``: one job's records, stitched into one timeline.

Runs against :class:`firestore_fakes.FakeQueryDB`, which honours ``where``,
``order_by`` and ``limit`` and refuses a query needing a composite index.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from firestore_fakes import FakeQueryDB as FakeDB
from google.cloud import firestore

import cli.trace_job as tj

UID = "u1"
JOB = "job-1"


def _path(name):
    return f"users/{UID}/{name}"


def _db(**collections) -> FakeDB:
    return FakeDB({_path(k): v for k, v in collections.items()})


def _trace(db, job_id=JOB, limit=tj.DEFAULT_EXPOSURES):
    return asyncio.run(tj.load_trace(db, UID, job_id, exposure_limit=limit))


def _text(db, job_id=JOB, limit=tj.DEFAULT_EXPOSURES) -> str:
    return "\n".join(tj.render(_trace(db, job_id, limit)))


# ----------------------------------------------------------------- fixtures

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def iso(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def scored_with(parse_model="gemini-flash", match_model="gemini-pro"):
    return {
        "parse_model": parse_model,
        "match_model": match_model,
        "parse_prompt_version": "p3" if parse_model else None,
        "match_prompt_version": "m5" if match_model else None,
        "scored_at": iso(5),
    }


def job_doc(**over):
    doc = {
        "id": JOB,
        "company": "Acme",
        "title": "Staff Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "discovered_at": iso(0),
        "discovered_via": "known",
        "user_decision": "approved",
        "jd_parsed": {"role_family": "engineering", "seniority": "staff"},
        "match": {
            "overall_score": 82.0,
            "recommendation": "strong_apply",
            "breakdown": {"role_fit": 90, "qualifications_match": 80},
        },
        "scored_at": iso(5),
        "scored_run_id": "run-score",
        "scored_with": scored_with(),
        "geo_gate": {
            "version": 2,
            "verdict": "eligible",
            "rule": "same_country",
            "residence_country": "US",
            "job_country": "US",
        },
    }
    doc.update(over)
    return doc


def tombstone(**over):
    doc = {
        "job_id": JOB,
        "company": "Acme",
        "title": "Sales Lead",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "score": 20.0,
        "recommendation": "skip",
        "reasoning": "Wrong country.",
        "discarded_at": iso(6),
        "scored_run_id": "run-discard",
        "jd_parsed": None,
        "geo_gate": {
            "version": 2,
            "verdict": "ineligible",
            "rule": "country_mismatch",
            "residence_country": "US",
            "job_country": "DE",
            "enforced": True,
        },
    }
    doc.update(over)
    return doc


def exposure(minutes, items, request_id="r"):
    return {
        "shown_at": iso(minutes),
        "request_id": request_id,
        "min_score": 60,
        "items": items,
    }


def item(job_id=JOB, rank=0, exploration=False):
    return {
        "job_id": job_id,
        "rank": rank,
        "overall_score": 82.0,
        "exploration": exploration,
    }


def decision(minutes, job_id=JOB, decision="approved", prev="pending", **over):
    doc = {
        "job_id": job_id,
        "decision": decision,
        "previous_decision": prev,
        "decided_at": iso(minutes),
        "actor": "user",
        "score_snapshot": {"overall_score": 82.0, "recommendation": "strong_apply"},
        "shown_at": iso(minutes - 1),
    }
    doc.update(over)
    return doc


# --------------------------------------------------------------------- tests


def test_tombstoned_job_is_traced_from_discarded_jobs():
    db = _db(discarded_jobs={JOB: tombstone()})
    trace = _trace(db)
    assert trace.found and trace.job is None
    text = _text(db)
    assert "in discarded_jobs only" in text
    assert "discarded (tombstone)" in text
    assert "score 20.0, recommendation skip" in text
    assert "ineligible" in text and "ENFORCED skip" in text
    assert "stored parse: none" in text


def test_job_in_both_collections_shows_both_and_is_flagged():
    db = _db(jobs={JOB: job_doc()}, discarded_jobs={JOB: tombstone()})
    text = _text(db)
    assert "in BOTH jobs and discarded_jobs" in text
    assert "scored (jobs)" in text and "discarded (tombstone)" in text
    # Both runs are looked up, not just the first record's.
    assert "run run-score" in text and "run run-discard" in text


def test_missing_job_says_so_and_exits_nonzero(capsys):
    db = _db(jobs={"other": job_doc(id="other")})
    code = asyncio.run(tj.run(db, UID, JOB, exposure_limit=10))
    out = capsys.readouterr().out
    assert code == 1
    assert f"not found in users/{UID}/jobs or users/{UID}/discarded_jobs" in out


def test_found_job_exits_zero(capsys):
    db = _db(jobs={JOB: job_doc()})
    assert asyncio.run(tj.run(db, UID, JOB, exposure_limit=10)) == 0


def test_score_is_read_from_match_not_the_top_level():
    # A stray top-level score must not be what the report shows.
    db = _db(jobs={JOB: job_doc(overall_score=11.0)})
    text = _text(db)
    assert "score 82.0, recommendation strong_apply" in text
    assert "role_fit=90" in text


def test_decision_for_a_different_job_is_excluded():
    db = _db(
        jobs={JOB: job_doc()},
        decisions={
            "d1": decision(10),
            "d2": decision(11, job_id="job-OTHER", decision="rejected"),
        },
    )
    trace = _trace(db)
    assert [d["decision"] for d in trace.decisions] == ["approved"]
    assert "rejected" not in _text(db)


def test_exposure_window_stops_at_the_limit():
    exps = {f"e{i}": exposure(i, [item()]) for i in range(10)}
    db = _db(jobs={JOB: job_doc()}, exposures=exps)
    trace = _trace(db, limit=4)
    assert len(trace.exposures) == 4
    text = _text(db, limit=4)
    assert "scanned the 4 most recent exposure(s)" in text
    assert "window capped at --exposures 4" in text


def test_exposure_scan_reads_the_newest_not_an_arbitrary_window():
    # Inserted newest-last and oldest-last alike: only an honoured
    # descending order picks minutes 7..9.
    exps = {f"e{i}": exposure(i, [item()]) for i in (3, 9, 0, 7, 1, 8, 2)}
    db = _db(jobs={JOB: job_doc()}, exposures=exps)
    trace = _trace(db, limit=3)
    assert sorted(e["shown_at"] for e in trace.exposures) == [iso(7), iso(8), iso(9)]
    assert f"{tj.fmt_time(T0 + timedelta(minutes=7))} →" in _text(db, limit=3)


def test_consecutive_exposures_collapse_into_one_run():
    exps = {f"e{i}": exposure(i, [item(rank=2)]) for i in range(14)}
    # A gap, then the job comes back at rank 0.
    exps["gap"] = exposure(20, [item(job_id="x")])
    exps["back"] = exposure(21, [item(rank=0)])
    db = _db(jobs={JOB: job_doc()}, exposures=exps)
    runs = tj.exposure_runs(_trace(db).exposures, JOB)
    assert [(r.rank, r.count) for r in runs] == [(2, 14), (0, 1)]
    text = _text(db)
    assert text.count("returned at rank 2") == 1
    assert "14 times between 2026-10-01 10:00:00Z and 2026-10-01 10:13:00Z" in text
    assert "shown at rank 0 (the card on screen)" in text
    assert "seen at rank" not in text


def test_rank_change_starts_a_new_run():
    exps = {
        "a": exposure(0, [item(rank=1)]),
        "b": exposure(1, [item(rank=1)]),
        "c": exposure(2, [item(rank=0)]),
    }
    runs = tj.exposure_runs(list(exps.values()), JOB)
    assert [(r.rank, r.count) for r in runs] == [(1, 2), (0, 1)]


def test_no_exposures_is_distinct_from_not_found_in_window():
    empty = _text(_db(jobs={JOB: job_doc()}))
    assert "no exposures recorded — LOG_EXPOSURES may be off" in empty
    assert "not returned in any scanned exposure" not in empty

    others = {"e": exposure(1, [item(job_id="x")])}
    absent = _text(_db(jobs={JOB: job_doc()}, exposures=others))
    assert "not returned in any scanned exposure" in absent
    assert "scanned the 1 most recent exposure(s)" in absent
    assert "LOG_EXPOSURES" not in absent


def test_out_of_order_records_come_out_chronological():
    # Mixed timestamp types too: a Firestore datetime, an offset ISO string.
    app = {
        "job_id": JOB,
        "status": "submitted",
        "timeline": [
            {"at": iso(40), "status": "submitted"},
            {"at": T0 + timedelta(minutes=20), "status": "queued"},
            {"at": "2026-10-01T06:30:00-04:00", "status": "ready_for_review"},
        ],
    }
    db = _db(
        jobs={JOB: job_doc()},
        decisions={
            "z": decision(15, decision="approved", prev="starred"),
            "a": decision(12, decision="starred", prev="pending"),
        },
        applications={"app-job-1": app},
        exposures={"e": exposure(11, [item()])},
    )
    events = tj.timeline(_trace(db))
    times = [e.at for e in events]
    assert all(t is not None for t in times)
    assert times == sorted(times)
    titles = [e.title for e in events]
    assert titles == [
        "discovered",
        "scored (jobs)",
        "shown at rank 0 (the card on screen)",
        "decided: pending → starred",
        "decided: starred → approved",
        "application app-job-1: queued",
        "application app-job-1: ready_for_review",
        "application app-job-1: submitted",
    ]


def test_null_parse_model_is_no_parse_paid_here_not_unknown():
    db = _db(jobs={JOB: job_doc(scored_with=scored_with(parse_model=None))})
    text = _text(db)
    assert "parse: no parse paid here" in text
    assert "unknown" not in text.lower()


def test_absent_null_and_named_provenance_render_differently():
    named = tj.provenance_lines({"scored_with": scored_with()})
    null_parse = tj.provenance_lines({"scored_with": scored_with(parse_model=None)})
    prefilter = tj.provenance_lines(
        {"scored_with": scored_with(parse_model=None, match_model=None)}
    )
    absent = tj.provenance_lines({})
    assert named[0] == "parse: gemini-flash (prompt p3)"
    assert null_parse[0].startswith("parse: no parse paid here")
    assert "pre-filter" in prefilter[1]
    assert absent[0].startswith("provenance: no scored_with record")
    assert len({named[0], null_parse[0], absent[0]}) == 3


def test_score_snapshot_absent_null_and_present_differ():
    present = tj.decision_event(decision(1)).details
    null = tj.decision_event(decision(1, score_snapshot=None)).details
    absent_doc = decision(1)
    del absent_doc["score_snapshot"]
    absent = tj.decision_event(absent_doc).details
    assert "score at decision: 82.0 (strong_apply)" in present
    assert "score snapshot: none — the job was unscored when decided" in null
    assert "score snapshot: not recorded" in absent


def test_run_link_prints_origin_request_filter_when_present():
    run = {
        "runner": "matching",
        "state": "done",
        "started_at": iso(4),
        "ended_at": iso(6),
        "llm": {"cost_usd": 0.0193, "calls": 2},
        "jobs": {"scored": 1},
        "origin_request_id": "req-42",
        "origin_run_id": "run-parent",
    }
    db = _db(jobs={JOB: job_doc()}, runs={"run-score": run})
    text = _text(db)
    assert 'jsonPayload.run_id="run-score"' in text
    assert (
        'jsonPayload.request_id="req-42" OR jsonPayload.origin_request_id="req-42"'
        in text
    )
    assert 'jsonPayload.run_id="run-parent"' in text
    assert "cost $0.0193 over 2 call(s)" in text


def test_run_without_origin_or_ledger_doc_still_prints_its_log_filter():
    db = _db(jobs={JOB: job_doc()})
    text = _text(db)
    assert "no ledger doc at runs/run-score" in text
    assert 'jsonPayload.run_id="run-score"' in text
    assert "origin_request_id" not in text


def test_exploration_and_discovery_are_reported():
    text = _text(_db(jobs={JOB: job_doc(exploration=True)}))
    assert "exploration sample: yes" in text
    assert "via known (from jobs)" in text
    assert "no discovery run id is recorded" in text


def test_queries_need_no_composite_index_and_nothing_is_written():
    db = _db(
        jobs={JOB: job_doc()},
        decisions={"d": decision(1)},
        applications={"a": {"job_id": JOB, "timeline": []}},
        exposures={"e": exposure(1, [item()])},
    )
    _trace(db, limit=7)
    by_path = {q[0]: q for q in db.queries}
    assert by_path[_path("decisions")][2] == ()
    assert by_path[_path("applications")][2] == ()
    assert by_path[_path("exposures")][1:] == (
        (),
        (("shown_at", firestore.Query.DESCENDING),),
        7,
    )


def test_fake_refuses_a_query_needing_a_composite_index():
    from google.cloud.firestore_v1.base_query import FieldFilter

    db = _db(decisions={"d": decision(1)})
    query = (
        db.collection("users")
        .document(UID)
        .collection("decisions")
        .where(filter=FieldFilter("job_id", "==", JOB))
        .order_by("decided_at")
    )
    with pytest.raises(AssertionError, match="composite index"):
        asyncio.run(tj._stream(query))


def test_to_utc_normalizes_every_stored_shape():
    assert tj.to_utc("2026-10-01T06:00:00-04:00") == T0
    assert tj.to_utc("2026-10-01T10:00:00Z") == T0
    assert tj.to_utc("2026-10-01T10:00:00") == T0
    assert tj.to_utc(datetime(2026, 10, 1, 10, 0)) == T0
    assert tj.to_utc(None) is None
    assert tj.to_utc("not a time") is None


def test_an_untimed_event_keeps_the_timeline_column_aligned():
    """Pre-provenance jobs have no scoring time; their title must still start
    in the same column as timed events, or the detail lines read as belonging
    to the wrong step."""
    db = _db(jobs={JOB: job_doc(scored_at=None, scored_with=None)})
    rows = [
        line
        for line in tj.render(_trace(db))
        if line.startswith("     2026") or "(no time)" in line
    ]
    untimed = next(line for line in rows if "(no time)" in line)
    timed = next(line for line in rows if line.startswith("     2026"))
    assert untimed.index("scored") == timed.index("discovered")
