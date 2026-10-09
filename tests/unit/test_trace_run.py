# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.trace_run``: one run's ledger doc, log lines and lineage.

Firestore is :class:`firestore_fakes.FakeQueryDB`. Cloud Logging is the fake
below, which parses the filter string the tool sends and applies every clause
— resource type, ``jsonPayload`` equalities, time bounds, severity, ``OR``
groups — plus ordering and ``max_results``. A clause it does not understand
fails the test rather than being ignored, so a filter the tool stops sending
is a filter the fake stops applying.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest
from firestore_fakes import FakeQueryDB
from google.cloud import logging as cloud_logging
from google.cloud.logging import Resource, StructEntry
from google.protobuf import json_format, struct_pb2

import cli.trace_run as tr

UID = "u1"
RUN = "run-1"
PROJECT = "test-project"

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=2)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iso(minutes: float) -> str:
    return at(minutes).isoformat()


# ------------------------------------------------------- the logging fake

_SEVERITY = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
}
_CLAUSE = re.compile(r'^([\w.]+)(>=|<=|=|>|<)("[^"]*"|\w+)$')


def _split_top(text: str, sep: str) -> list[str]:
    """Split on ``sep`` outside parentheses."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def _compare(left, op, right) -> bool:
    if left is None:
        return False
    return {
        "=": left == right,
        ">=": left >= right,
        "<=": left <= right,
        ">": left > right,
        "<": left < right,
    }[op]


def _predicate(clause: str):
    if clause.startswith("(") and clause.endswith(")"):
        terms = [_predicate(t) for t in _split_top(clause[1:-1], " OR ")]
        return lambda e: any(t(e) for t in terms)
    match = _CLAUSE.match(clause)
    if not match:
        raise AssertionError(f"fake logging cannot apply clause {clause!r}")
    key, op, raw = match.groups()
    value = raw.strip('"')
    if key == "resource.type":
        assert op == "=", clause
        return lambda e: e.resource.type == value
    if key == "timestamp":
        bound = datetime.fromisoformat(value)
        return lambda e: _compare(e.timestamp, op, bound)
    if key == "severity":
        rank = _SEVERITY[value]
        return lambda e: _compare(_SEVERITY.get(e.severity or "DEFAULT"), op, rank)
    if key.startswith("jsonPayload."):
        name = key.removeprefix("jsonPayload.")
        assert op == "=", clause
        return lambda e: (
            str(e.payload.get(name)) == value if name in e.payload else False
        )
    raise AssertionError(f"fake logging does not know field {key!r}")


def _as_returned(e: StructEntry) -> StructEntry:
    """``e`` as Cloud Logging hands it back: the payload has been through a
    protobuf ``Struct``, so every number is a double (``2`` reads ``2.0``)."""
    struct = struct_pb2.Struct()
    struct.update(e.payload)
    return StructEntry(
        payload=json_format.MessageToDict(struct),
        timestamp=e.timestamp,
        severity=e.severity,
        resource=e.resource,
        insert_id=e.insert_id,
    )


class FakeLogging:
    """A Cloud Logging client over a list of real ``StructEntry`` objects,
    returned with float numbers the way the real client returns them.
    ``calls`` records each ``(filter, max_results)`` asked for."""

    def __init__(self, entries=(), project=PROJECT):
        self.entries = list(entries)
        self.project = project
        self.calls: list[tuple[str, int | None]] = []

    def list_entries(
        self,
        *,
        resource_names=None,
        filter_=None,
        order_by=None,
        max_results=None,
        page_size=None,
        page_token=None,
    ):
        assert resource_names == [f"projects/{self.project}"], resource_names
        assert filter_, "an unfiltered read of the whole project"
        self.calls.append((filter_, max_results))
        preds = [_predicate(c) for c in _split_top(filter_, " AND ")]
        rows = [e for e in self.entries if all(p(e) for p in preds)]
        rows.sort(
            key=lambda e: e.timestamp, reverse=order_by == cloud_logging.DESCENDING
        )
        if max_results is not None:
            rows = rows[:max_results]
        return iter([_as_returned(e) for e in rows])


_ids = itertools.count()


def entry(
    minutes,
    message,
    *,
    run_id=RUN,
    severity="INFO",
    service="hermes-worker",
    resource_type="cloud_run_revision",
    **fields,
):
    payload = {"message": message, **fields}
    if run_id is not None:
        payload["run_id"] = run_id
    return StructEntry(
        payload=payload,
        timestamp=at(minutes),
        severity=severity,
        resource=Resource(type=resource_type, labels={"service_name": service}),
        insert_id=f"i{next(_ids)}",
    )


def started(minutes, agent="matching", **kw):
    return entry(minutes, "agent.started", agent=agent, **kw)


def finished(minutes, agent="matching", outcome="completed", ms=60000.0, **kw):
    return entry(
        minutes, "agent.finished", agent=agent, outcome=outcome, duration_ms=ms, **kw
    )


# ------------------------------------------------------------------ fixtures


def run_doc(**over):
    doc = {
        "run_id": RUN,
        "user_id": UID,
        "runner": "score_task",
        "state": "done",
        "started_at": iso(0),
        "ended_at": iso(4),
        "llm": {"cost_usd": 0.0193, "calls": 2},
        "jobs": {"scored": 10, "discarded": 3},
    }
    doc.update(over)
    return doc


def _db(runs=None) -> FakeQueryDB:
    return FakeQueryDB({f"users/{UID}/runs": dict(runs or {})})


def _trace(db, logs, *, now=LATER, max_entries=100, since=None):
    return asyncio.run(
        tr.load_run_trace(
            db,
            logs,
            PROJECT,
            UID,
            RUN,
            max_entries=max_entries,
            since=since,
            now=now,
        )
    )


def _text(db, logs, **kw) -> str:
    return "\n".join(tr.render(_trace(db, logs, **kw)))


@pytest.fixture(autouse=True)
def no_real_logging_client(monkeypatch):
    """The suite must never build a real Cloud Logging client."""

    def _refuse(*args, **kwargs):
        raise AssertionError("tests/unit built a real cloud_logging.Client")

    monkeypatch.setattr(cloud_logging, "Client", _refuse)


# --------------------------------------------------------------------- tests


def test_finished_run_renders_header_stages_cost_and_counts():
    logs = FakeLogging(
        [
            started(0.1, agent="score_task", service="hermes-worker"),
            entry(1, "llm.call"),
            finished(3.9, agent="score_task", ms=228000.0),
            entry(4.1, "http.request", service="hermes-api"),
        ]
    )
    text = _text(_db({RUN: run_doc()}), logs)
    assert "runner score_task, state done" in text
    assert "2026-10-01 10:00:00Z → 2026-10-01 10:04:00Z (4m 00s)" in text
    assert "cost $0.0193 over 2 call(s)" in text
    assert "jobs: discarded=3, scored=10" in text
    assert "score_task: completed in 3m 48s on hermes-worker" in text
    assert "4 line(s): hermes-worker 3, hermes-api 1" in text
    assert "DIED" not in text and "capped" not in text
    assert 'jsonPayload.run_id="run-1"' in text


def test_unfinished_stage_on_a_closed_run_is_flagged_as_died():
    logs = FakeLogging(
        [
            started(0.1, agent="discovery"),
            finished(1, agent="discovery"),
            started(1.5, agent="matching"),
        ]
    )
    text = _text(_db({RUN: run_doc(state="failed")}), logs)
    assert (
        "!!! DIED MID-STAGE: matching started 2026-10-01 10:01:30Z on "
        "hermes-worker and never logged agent.finished"
    ) in text
    assert "matching: started on hermes-worker, NEVER FINISHED" in text
    # The banner comes before every section, straight after the header.
    assert text.index("DIED MID-STAGE") < text.index("\n   logs")
    assert "discovery: completed" in text


def test_unfinished_stage_of_a_live_run_is_in_progress_not_dead():
    doc = run_doc(state="running", ended_at=None)
    logs = FakeLogging([started(0.1)])
    text = _text(_db({RUN: doc}), logs, now=at(5))
    assert "matching started 2026-10-01 10:00:06Z on hermes-worker and is " in text
    assert "still in progress" in text
    assert "DIED" not in text


def test_without_a_ledger_doc_an_open_stage_is_aged_by_its_start():
    logs = FakeLogging([started(0.1)])
    recent = _text(_db(), logs, now=at(10))
    assert "still in progress" in recent and "DIED" not in recent
    old = _text(_db(), logs, now=at(60))
    assert "!!! DIED MID-STAGE: matching" in old


def test_a_capped_stage_read_does_not_fake_a_death():
    # Two starts and two finishes; a cap of 3 drops the last finish.
    logs = FakeLogging(
        [
            started(0.1, agent="a"),
            started(0.2, agent="b"),
            finished(1, agent="a"),
            finished(2, agent="b"),
        ]
    )
    text = _text(_db({RUN: run_doc()}), logs, max_entries=3)
    assert "DIED" not in text
    assert "b started 2026-10-01 10:00:12Z on hermes-worker; its finish may lie" in text


def test_stalled_running_doc_is_shown_as_stalled_and_its_stage_as_dead():
    doc = run_doc(state="running", ended_at=None, llm=None, jobs=None)
    logs = FakeLogging([started(0.1)])
    text = _text(_db({RUN: doc}), logs, now=at(120))
    assert "state STALLED — the doc still says running" in text
    assert "died without closing it" in text
    assert "within the staleness ceiling" not in text
    assert "!!! DIED MID-STAGE: matching" in text
    assert "cost: not recorded" in text


def test_running_doc_within_the_ceiling_is_running():
    doc = run_doc(state="running", ended_at=None)
    text = _text(_db({RUN: doc}), FakeLogging(), now=at(10))
    assert "state running (open 10m 00s, within the staleness ceiling)" in text
    assert "STALLED" not in text


def test_missing_ledger_doc_still_reads_logs_and_says_it_is_not_empty():
    logs = FakeLogging([started(0.1, runner="score_task"), finished(2)])
    text = _text(_db(), logs, now=at(60))
    assert "no ledger doc at users/u1/runs/run-1" in text
    assert "That is not an empty run; logs were read anyway." in text
    assert "runner score_task (from logs)" in text
    assert "matching: completed in 1m 00s" in text
    assert "(--since 7d, no ledger doc)" in text
    assert logs.calls, "logs were not read"


def test_an_empty_run_is_not_reported_as_a_missing_doc():
    doc = run_doc(llm={"cost_usd": 0.0, "calls": 0}, jobs={"scored": 0})
    text = _text(_db({RUN: doc}), FakeLogging([started(0.1), finished(1)]))
    assert "no ledger doc" not in text
    assert "cost $0.0000 over 0 call(s)" in text
    assert "jobs: scored=0" in text


def test_no_log_lines_mentions_retention_not_that_nothing_was_logged():
    text = _text(_db({RUN: run_doc()}), FakeLogging())
    assert "no Cloud Run log lines for this run in that window" in text
    assert "keeps lines 30 days by default" in text
    assert "Absence is not evidence the run did nothing" in text
    assert "logged nothing" not in text


def test_other_runs_and_out_of_window_lines_never_appear():
    logs = FakeLogging(
        [
            started(0.1),
            finished(3),
            entry(2, "OTHER RUN LINE", run_id="run-OTHER", severity="ERROR"),
            entry(-60, "TOO EARLY", severity="ERROR"),
            entry(90, "TOO LATE", severity="ERROR"),
            entry(1, "NOT CLOUD RUN", severity="ERROR", resource_type="gce_instance"),
        ]
    )
    trace = _trace(_db({RUN: run_doc()}), logs)
    messages = {line.message for line in trace.lines}
    assert messages == {"agent.started", "agent.finished"}
    text = "\n".join(tr.render(trace))
    for absent in ("OTHER RUN LINE", "TOO EARLY", "TOO LATE", "NOT CLOUD RUN"):
        assert absent not in text
    assert "none at WARNING or above" in text


def test_without_a_ledger_doc_the_since_window_bounds_the_read():
    logs = FakeLogging([entry(0, "recent"), entry(-60 * 24 * 8, "eight days old")])
    trace = _trace(_db(), logs, now=at(60), since=tr.parse_since("7d"))
    assert [line.message for line in trace.main.lines] == ["recent"]
    assert trace.window.start == at(60) - timedelta(days=7)


def test_window_follows_the_ledger_with_a_margin():
    window = tr.log_window(run_doc(), since=timedelta(days=7), now=LATER)
    assert (window.start, window.end) == (at(-5), at(9))
    assert "ledger started_at → ended_at" in window.source


def test_entry_cap_is_honoured_and_reported():
    logs = FakeLogging([entry(i * 0.1, f"line {i}") for i in range(10)])
    trace = _trace(_db({RUN: run_doc()}), logs, max_entries=4)
    assert [ln.message for ln in trace.main.lines] == [f"line {i}" for i in range(4)]
    assert trace.main.capped
    assert all(max_results == 5 for _, max_results in logs.calls)
    text = "\n".join(tr.render(trace))
    assert "! main read capped at --max-entries 4" in text
    assert "nothing after 2026-10-01 10:00:18Z was read" in text


def test_a_read_that_fits_the_cap_exactly_is_not_capped():
    logs = FakeLogging([entry(i * 0.1, f"line {i}") for i in range(4)])
    trace = _trace(_db({RUN: run_doc()}), logs, max_entries=4)
    assert len(trace.main.lines) == 4 and not trace.main.capped


def test_repeated_identical_warnings_collapse_with_a_count():
    logs = FakeLogging(
        [entry(i, "fetch.retry", severity="WARNING") for i in (0.5, 1, 1.5)]
        + [
            entry(2, "fetch.retry", severity="WARNING", service="hermes-api"),
            entry(
                3, "score.failed", severity="ERROR", exception="Trace\nValueError: x"
            ),
            entry(3.5, "info only"),
        ]
    )
    text = _text(_db({RUN: run_doc()}), logs)
    assert (
        "10:00:30Z  WARNING  hermes-worker  fetch.retry  (x3, last 10:01:30Z)" in text
    )
    assert "10:02:00Z  WARNING  hermes-api  fetch.retry" in text
    assert text.count("hermes-worker  fetch.retry") == 1
    assert "10:03:00Z  ERROR    hermes-worker  score.failed" in text
    assert "ValueError: x" in text
    assert "info only" not in text.split("problems")[1].split("retries")[0]


def test_problems_beyond_a_capped_main_read_are_still_reported():
    logs = FakeLogging(
        [entry(i * 0.1, "chatter") for i in range(20)]
        + [entry(3.5, "late.error", severity="ERROR")]
    )
    text = _text(_db({RUN: run_doc()}), logs, max_entries=5)
    assert "late.error" in text


def test_retries_are_grouped_by_task_and_dropped_deliveries_called_out():
    task = {"task_name": "projects/p/queues/q/tasks/t1"}
    logs = FakeLogging(
        [
            started(0.1, retry_count=0, execution_count=0, **task),
            entry(
                0.5, "boom", severity="ERROR", retry_count=0, execution_count=0, **task
            ),
            started(2, retry_count=2, execution_count=1, **task),
            finished(3, retry_count=2, execution_count=1, **task),
            entry(1, "other", task_name="t2", retry_count=0, execution_count=0),
        ]
    )
    text = _text(_db({RUN: run_doc()}), logs)
    assert "task projects/p/queues/q/tasks/t1: 2 attempt(s) seen" in text
    assert "retry_count=0 execution_count=0 on hermes-worker\n" in text
    assert (
        "retry_count=2 execution_count=1 on hermes-worker  ! 1 attempt(s) ended "
        "in a 5xx or no response: a handler crash (500), a timeout, or the platform"
    ) in text
    assert "task t2: 1 attempt(s) seen" in text


def test_float_counts_from_cloud_logging_render_as_ints_and_flag_a_gap():
    """Real Cloud Logging returns ``2.0`` / ``1.0``; the gap must still fire."""
    task = {"task_name": "t1"}
    logs = FakeLogging(
        [
            started(0.1, retry_count=2.0, execution_count=1.0, **task),
            finished(1, retry_count=2.0, execution_count=1.0, **task),
        ]
    )
    text = _text(_db({RUN: run_doc()}), logs)
    assert "retry_count=2 execution_count=1 on hermes-worker  ! 1 attempt(s)" in text
    assert "2.0" not in text


def test_whole_number_floats_are_coerced_and_fractions_kept():
    assert tr._whole_numbers(
        {"a": 2.0, "b": 1.5, "c": [0.0, {"d": 3.0}], "e": True, "f": "2.0"}
    ) == {"a": 2, "b": 1.5, "c": [0, {"d": 3}], "e": True, "f": "2.0"}
    assert type(tr._whole_numbers(2.0)) is int


def test_no_task_fields_says_so():
    text = _text(_db({RUN: run_doc()}), FakeLogging([started(0.1), finished(1)]))
    assert "no Cloud Tasks fields on any line" in text


def test_children_from_ledger_and_logs_parent_and_origin_request():
    runs = {
        RUN: run_doc(origin_run_id="run-parent", origin_request_id="req-42"),
        "run-parent": run_doc(
            run_id="run-parent", runner="auto_discovery", ended_at=iso(1)
        ),
        "child-ledger": run_doc(
            run_id="child-ledger",
            runner="batch_start",
            origin_run_id=RUN,
            started_at=iso(3),
            ended_at=iso(5),
        ),
        "child-both": run_doc(
            run_id="child-both",
            runner="score_task",
            origin_run_id=RUN,
            started_at=iso(2),
            ended_at=iso(2.5),
        ),
        "unrelated": run_doc(run_id="unrelated", origin_run_id="run-ELSE"),
    }
    logs = FakeLogging(
        [
            started(0.1),
            finished(3.9),
            entry(2, "run.opened", run_id="child-both", origin_run_id=RUN),
            entry(20, "agent.started", run_id="child-logs", origin_run_id=RUN),
            entry(21, "run.opened", run_id="child-ELSE", origin_run_id="run-ELSE"),
        ]
    )
    trace = _trace(_db(runs), logs)
    assert set(trace.children) == {"child-ledger", "child-both", "child-logs"}
    text = "\n".join(tr.render(trace))
    caused = text.split("\n     caused\n")[1]
    assert caused.index("child-both") < caused.index("child-ledger")
    assert "run child-both: score_task, done, 30s" in text and "(ledger + logs)" in text
    assert "run child-ledger: batch_start, done, 2m 00s" in text
    assert "run child-logs: no ledger doc (logs)" in text
    assert "unrelated" not in text and "child-ELSE" not in text
    assert "run run-parent: auto_discovery, done, 1m 00s" in text
    assert (
        'jsonPayload.request_id="req-42" OR jsonPayload.origin_request_id="req-42"'
        in text
    )


def test_origin_is_read_from_logs_when_the_doc_is_missing():
    logs = FakeLogging([started(0.1, origin_run_id="run-p", origin_request_id="r9")])
    text = _text(_db(), logs, now=at(60))
    assert "run run-p: no ledger doc (from logs)" in text
    assert "request r9 (from logs)" in text


def test_no_lineage_says_so():
    text = _text(_db({RUN: run_doc()}), FakeLogging([started(0.1), finished(1)]))
    assert "caused by\n       nothing recorded" in text
    assert "nothing with origin_run_id=run-1 in the ledger" in text


def test_children_query_is_equality_only_with_no_order_by():
    db = _db({RUN: run_doc()})
    _trace(db, FakeLogging())
    assert db.queries == [(f"users/{UID}/runs", (("origin_run_id", RUN),), (), None)]


def test_children_read_reaches_past_the_run_window():
    """A queued child can start after its parent closed."""
    logs = FakeLogging(
        [entry(25, "run.opened", run_id="late-child", origin_run_id=RUN)]
    )
    trace = _trace(_db({RUN: run_doc()}), logs)
    assert "late-child" in trace.children
    assert "late-child" not in {ln.payload.get("run_id") for ln in trace.lines}


def test_the_filter_the_tool_builds():
    window = tr.log_window(run_doc(), since=timedelta(days=7), now=LATER)
    assert tr.build_filter("run_id", RUN, window) == (
        'resource.type="cloud_run_revision" AND jsonPayload.run_id="run-1" AND '
        'timestamp>="2026-10-01T09:55:00Z" AND timestamp<="2026-10-01T10:09:00Z"'
    )


def test_service_is_shown_per_problem_line():
    logs = FakeLogging(
        [
            entry(1, "api.warn", severity="WARNING", service="hermes-api"),
            entry(2, "worker.warn", severity="WARNING", service="hermes-worker"),
        ]
    )
    text = _text(_db({RUN: run_doc()}), logs)
    assert "hermes-api  api.warn" in text and "hermes-worker  worker.warn" in text


def _problems(text: str) -> str:
    return text.split("\n   problems")[1].split("\n   retries")[0]


def _discovery_complete(minutes, **counts):
    return entry(minutes, "discovery.complete", **counts)


def test_board_404s_and_failures_are_problems_though_logged_at_info():
    """A 404 is logged at info per board, so the WARNING read never sees it;
    the counts on ``discovery.complete`` are how it reaches the summary."""
    logs = FakeLogging(
        [
            _discovery_complete(
                2,
                boards_not_found=2.0,
                boards_failing=1.0,
                unhealthy_boards=[
                    "greenhouse/acme:not_found",
                    "lever/globex:not_found",
                    "ashby/initech:rate_limited",
                ],
            )
        ]
    )
    problems = _problems(_text(_db({RUN: run_doc()}), logs))
    assert "none at WARNING or above" in problems
    assert (
        "10:02:00Z  boards not found (404): 2, failing (429/5xx/timeout/error): 1"
        in problems
    )
    assert "greenhouse/acme:not_found, lever/globex:not_found" in problems


def test_board_counts_show_beside_other_warnings():
    logs = FakeLogging(
        [
            entry(1, "fetch.retry", severity="WARNING"),
            _discovery_complete(2, boards_not_found=0.0, boards_failing=4.0),
        ]
    )
    problems = _problems(_text(_db({RUN: run_doc()}), logs))
    assert "fetch.retry" in problems
    assert "boards not found (404): 0, failing (429/5xx/timeout/error): 4" in problems


def test_a_clean_discovery_run_adds_no_board_line():
    logs = FakeLogging(
        [_discovery_complete(2, boards_not_found=0.0, boards_failing=0.0)]
    )
    problems = _problems(_text(_db({RUN: run_doc()}), logs))
    assert "boards not found" not in problems


def test_the_board_counts_survive_a_capped_main_read():
    logs = FakeLogging(
        [entry(i * 0.1, "chatter") for i in range(20)]
        + [_discovery_complete(3.5, boards_not_found=1.0, boards_failing=0.0)]
    )
    text = _text(_db({RUN: run_doc()}), logs, max_entries=5)
    assert "boards not found (404): 1" in _problems(text)


def test_a_board_health_change_names_the_board():
    logs = FakeLogging(
        [
            entry(
                1,
                "board_health.changed",
                severity="WARNING",
                platform=p,
                slug=s,
                from_state="ok",
                to_state="failing",
                outcome="not_found",
                status=404.0,
            )
            for p, s in (("greenhouse", "acme"), ("lever", "globex"))
        ]
    )
    problems = _problems(_text(_db({RUN: run_doc()}), logs))
    assert "WARNING  hermes-worker  board_health.changed" in problems
    assert "greenhouse/acme: ok → failing (not_found, status 404)" in problems
    assert "lever/globex: ok → failing (not_found, status 404)" in problems


def test_exit_status_and_both_clients_are_pinned_to_the_project(monkeypatch, capsys):
    built = {}

    def fake_firestore(*, project):
        built["firestore"] = project
        return _db()

    def fake_logging(*, project):
        built["logging"] = project
        return FakeLogging(project=project)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setattr(tr.firestore, "AsyncClient", fake_firestore)
    monkeypatch.setattr(tr.cloud_logging, "Client", fake_logging)
    monkeypatch.setattr(sys, "argv", ["trace_run", "--user-id", UID, "--run-id", RUN])
    assert asyncio.run(tr.main()) == 1
    assert built == {"firestore": PROJECT, "logging": PROJECT}
    assert "no ledger doc" in capsys.readouterr().out


def test_unset_project_refuses_to_guess(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", " ")
    with pytest.raises(AssertionError, match="cannot pin"):
        tr.pinned_project()


def test_found_run_exits_zero(capsys):
    code = asyncio.run(
        tr.run(
            _db({RUN: run_doc()}),
            FakeLogging(),
            PROJECT,
            UID,
            RUN,
            max_entries=10,
            since=timedelta(days=7),
            now=LATER,
        )
    )
    assert code == 0


def test_logging_fake_applies_what_it_is_sent():
    logs = FakeLogging(
        [entry(0, "mine"), entry(1, "theirs", run_id="x"), entry(99, "late")]
    )
    got = list(
        logs.list_entries(
            resource_names=[f"projects/{PROJECT}"],
            filter_=(
                'jsonPayload.run_id="run-1" AND timestamp<="2026-10-01T10:10:00Z"'
            ),
        )
    )
    assert [e.payload["message"] for e in got] == ["mine"]
    # Numbers come back as doubles, as they do from the real client — the
    # fake returning ints is how a float-only bug passed every test.
    counted = list(
        FakeLogging([entry(0, "n", retry_count=2, execution_count=0)]).list_entries(
            resource_names=[f"projects/{PROJECT}"],
            filter_='resource.type="cloud_run_revision"',
        )
    )
    assert counted[0].payload["retry_count"] == 2.0
    assert type(counted[0].payload["retry_count"]) is float
    assert type(counted[0].payload["execution_count"]) is float
    capped = logs.list_entries(
        resource_names=[f"projects/{PROJECT}"],
        filter_='resource.type="cloud_run_revision"',
        max_results=2,
    )
    assert [e.payload["message"] for e in capped] == ["mine", "theirs"]
    with pytest.raises(AssertionError, match="cannot apply"):
        list(logs.list_entries(resource_names=[f"projects/{PROJECT}"], filter_="x ~ y"))


def test_parse_since():
    assert tr.parse_since("30m") == timedelta(minutes=30)
    assert tr.parse_since("24h") == timedelta(hours=24)
    assert tr.parse_since("7d") == timedelta(days=7)
    with pytest.raises(Exception, match="use a number"):
        tr.parse_since("week")
