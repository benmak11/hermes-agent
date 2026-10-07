# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Print what one background run did: its ledger summary (state, window, cost,
counts), the stages it logged and whether each finished, its warnings and
errors, its Cloud Tasks retries, what caused it and what it caused.

Free and read-only: Firestore reads and Cloud Logging reads only, no write and
no model call. The one Firestore query is a single-field equality, so no
composite index is needed; children are sorted client-side.

Every log read is bounded in time — by the ledger doc's ``started_at`` /
``ended_at`` with a margin, or by ``--since`` when there is no ledger doc —
and capped at ``--max-entries``. The window searched and any cap hit are
always printed, so "not found" is never read as "did not happen".

Usage:
    python -m cli.trace_run --user-id me --run-id abc123
    python -m cli.trace_run --user-id me --run-id abc123 --since 30d
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud import logging as cloud_logging
from google.cloud.firestore_v1.base_query import FieldFilter

from cli.trace_job import fmt_time, positive_int, to_utc
from tools.run_costs import RETENTION_DAYS, RUNNING, STALE_AFTER, run_is_stalled

# Pins GOOGLE_CLOUD_PROJECT from the project-root .env. ADC's default quota
# project on a dev machine is a different GCP project, so both clients below
# are constructed with an explicit project rather than letting it be inferred.
load_dotenv()

RUNS = "runs"

DEFAULT_MAX_ENTRIES = 1000
DEFAULT_SINCE = "7d"

#: Slack either side of the ledger window: a run logs a little before
#: ``open_run`` and a little after ``ended_at`` is stamped.
WINDOW_MARGIN = timedelta(minutes=5)

#: Cloud Logging's default ``_Default`` bucket retention.
LOG_RETENTION_DAYS = 30

STAGE_EVENTS = ("agent.started", "agent.finished")
#: Events a child run logs once near its start; matching on them keeps the
#: children read small enough that one chatty child cannot exhaust the cap.
CHILD_EVENTS = ("run.opened", "agent.started")

_SEVERITY_RANK = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
    "ALERT": 700,
    "EMERGENCY": 800,
}
_WARNING = _SEVERITY_RANK["WARNING"]

_SINCE = re.compile(r"([1-9][0-9]*)([mhd])")


# ------------------------------------------------------------------ reading


@dataclass
class Window:
    """The time range every log read is bounded to, and where it came from."""

    start: datetime
    end: datetime
    source: str


@dataclass
class LogLine:
    at: datetime | None
    severity: str
    service: str
    payload: dict
    insert_id: str | None = None

    @property
    def message(self) -> str:
        return str(self.payload.get("message") or "(no message)")


@dataclass
class LogRead:
    """One Cloud Logging read: the filter sent, what came back, and whether
    the entry cap cut it short."""

    filter: str
    lines: list[LogLine] = field(default_factory=list)
    capped: bool = False


@dataclass
class RunTrace:
    """Everything read about one run. Nothing here is derived."""

    user_id: str
    run_id: str
    now: datetime
    window: Window
    max_entries: int
    doc: dict | None = None
    main: LogRead | None = None
    stage_read: LogRead | None = None
    problem_read: LogRead | None = None
    child_read: LogRead | None = None
    #: ``run_id -> doc``, ``None`` when a log-found child has no ledger doc.
    children: dict[str, dict | None] = field(default_factory=dict)
    child_sources: dict[str, set[str]] = field(default_factory=dict)
    parent_id: str | None = None
    parent: dict | None = None
    origin_request_id: str | None = None
    origin_source: str = "ledger"

    @property
    def lines(self) -> list[LogLine]:
        """Every line read for this run, deduplicated, oldest first."""
        seen: set = set()
        out: list[LogLine] = []
        for read in (self.main, self.stage_read, self.problem_read):
            for line in read.lines if read else ():
                key = line.insert_id or (line.at, line.message, repr(line.payload))
                if key not in seen:
                    seen.add(key)
                    out.append(line)
        far = datetime.max.replace(tzinfo=UTC)
        return sorted(out, key=lambda ln: (ln.at is None, ln.at or far))


def parse_since(value: str) -> timedelta:
    """``30m`` / ``24h`` / ``7d`` as a timedelta."""
    match = _SINCE.fullmatch(value.strip())
    if not match:
        raise argparse.ArgumentTypeError("use a number and m, h or d, e.g. 7d")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    return timedelta(**{unit: int(match.group(1))})


def log_window(doc: dict | None, *, since: timedelta, now: datetime) -> Window:
    """Bound the log reads by the ledger doc when it has a readable start,
    else by ``since`` back from ``now``."""
    started = to_utc((doc or {}).get("started_at"))
    if started is None:
        why = "no ledger doc" if doc is None else "ledger doc has no started_at"
        return Window(now - since, now, f"--since {_fmt_delta(since)}, {why}")
    assert doc is not None
    ended = to_utc(doc.get("ended_at"))
    if doc.get("state") == RUNNING and not run_is_stalled(doc, now=now):
        end, source = now, "ledger started_at → now (run is open)"
    elif doc.get("state") == RUNNING:
        end, source = started + STALE_AFTER, "ledger started_at → staleness ceiling"
    elif ended is not None:
        end, source = ended, "ledger started_at → ended_at"
    else:
        end, source = started + STALE_AFTER, "ledger started_at → staleness ceiling"
    margin = int(WINDOW_MARGIN.total_seconds() // 60)
    return Window(
        started - WINDOW_MARGIN,
        min(end + WINDOW_MARGIN, max(now, started)),
        f"{source}, ±{margin}m",
    )


def _ts(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_filter(
    field_name: str,
    value: str,
    window: Window,
    *extra: str,
    end: datetime | None = None,
) -> str:
    """A Cloud Logging filter: Cloud Run only, one ``jsonPayload`` equality,
    bounded to ``window`` (or to ``end`` when given), plus ``extra`` clauses."""
    clauses = [
        'resource.type="cloud_run_revision"',
        f'jsonPayload.{field_name}="{value}"',
        f'timestamp>="{_ts(window.start)}"',
        f'timestamp<="{_ts(end or window.end)}"',
        *extra,
    ]
    return " AND ".join(clauses)


def _any_message(events: tuple[str, ...]) -> str:
    return "(" + " OR ".join(f'jsonPayload.message="{e}"' for e in events) + ")"


def _whole_numbers(value: Any) -> Any:
    """Whole-number floats as ints, recursively.

    Cloud Logging hands every ``jsonPayload`` number back as a double, so a
    logged ``retry_count=2`` arrives as ``2.0``.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _whole_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole_numbers(v) for v in value]
    return value


def _to_line(entry: Any) -> LogLine:
    payload = entry.payload
    if not isinstance(payload, dict):
        payload = {} if payload is None else {"message": str(payload)}
    labels = getattr(entry.resource, "labels", None) or {}
    return LogLine(
        at=to_utc(entry.timestamp),
        severity=(entry.severity or "DEFAULT").upper(),
        service=labels.get("service_name") or "(unknown service)",
        payload=_whole_numbers(dict(payload)),
        insert_id=getattr(entry, "insert_id", None),
    )


def read_logs(client, project: str, filter_: str, max_entries: int) -> LogRead:
    """Read up to ``max_entries`` lines matching ``filter_``, oldest first.
    One past the cap is requested, so a read that fits exactly is not
    reported as capped."""
    entries = client.list_entries(
        resource_names=[f"projects/{project}"],
        filter_=filter_,
        order_by=cloud_logging.ASCENDING,
        max_results=max_entries + 1,
    )
    lines = [_to_line(e) for e in entries]
    return LogRead(filter_, lines[:max_entries], capped=len(lines) > max_entries)


async def _stream(query) -> list:
    return [snap async for snap in query.stream()]


async def _get_run(runs, run_id: str) -> dict | None:
    snap = await runs.document(run_id).get()
    return (snap.to_dict() or {}) if snap.exists else None


async def load_run_trace(
    db,
    logs,
    project: str,
    user_id: str,
    run_id: str,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    since: timedelta | None = None,
    now: datetime | None = None,
) -> RunTrace:
    """Read the ledger doc, the run's log lines, its children and its parent."""
    now = now or datetime.now(UTC)
    runs = db.collection("users").document(user_id).collection(RUNS)
    doc = await _get_run(runs, run_id)
    window = log_window(doc, since=since or parse_since(DEFAULT_SINCE), now=now)
    trace = RunTrace(user_id, run_id, now, window, max_entries, doc=doc)

    def read(*extra: str, field_name: str = "run_id", end=None) -> LogRead:
        flt = build_filter(field_name, run_id, window, *extra, end=end)
        return read_logs(logs, project, flt, max_entries)

    trace.main = read()
    # Read separately so a capped main read can lose neither a stage's end
    # (which would fake a death) nor a late error.
    trace.stage_read = read(_any_message(STAGE_EVENTS))
    trace.problem_read = read("severity>=WARNING")
    # A child can start after this run ends, as late as a queued dispatch.
    trace.child_read = read(
        _any_message(CHILD_EVENTS),
        field_name="origin_run_id",
        end=min(window.end + STALE_AFTER, max(now, window.end)),
    )

    # Equality only: an order_by beside it would need a composite index.
    by_origin = FieldFilter("origin_run_id", "==", run_id)
    for snap in await _stream(runs.where(filter=by_origin)):
        trace.children[snap.id] = snap.to_dict() or {}
        trace.child_sources.setdefault(snap.id, set()).add("ledger")
    for line in trace.child_read.lines:
        child = line.payload.get("run_id")
        if not child or child == run_id:
            continue
        trace.child_sources.setdefault(child, set()).add("logs")
        if child not in trace.children:
            trace.children[child] = await _get_run(runs, child)

    trace.parent_id = (doc or {}).get("origin_run_id")
    trace.origin_request_id = (doc or {}).get("origin_request_id")
    if not (trace.parent_id or trace.origin_request_id):
        for line in trace.lines:
            if line.payload.get("origin_run_id") or line.payload.get(
                "origin_request_id"
            ):
                trace.parent_id = line.payload.get("origin_run_id")
                trace.origin_request_id = line.payload.get("origin_request_id")
                trace.origin_source = "logs"
                break
    if trace.parent_id:
        trace.parent = await _get_run(runs, trace.parent_id)
    return trace


# ------------------------------------------------------------------ helpers


def _fmt_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    whole = round(seconds)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _short(at: datetime | None) -> str:
    return at.strftime("%H:%M:%SZ") if at else "(no time)"


def state_label(doc: dict, now: datetime) -> str:
    """``state`` as stored, except an open doc past the staleness ceiling,
    which reads as dead rather than running."""
    state = doc.get("state")
    if run_is_stalled(doc, now=now):
        return (
            f"STALLED — the doc still says {RUNNING}, but it is past the "
            f"{fmt_duration(STALE_AFTER.total_seconds())} ceiling: the process "
            "died without closing it"
        )
    if state == RUNNING:
        started = to_utc(doc.get("started_at"))
        age = fmt_duration((now - started).total_seconds()) if started else "?"
        return f"{RUNNING} (open {age}, within the staleness ceiling)"
    return str(state)


def run_summary(run_id: str, doc: dict | None, now: datetime) -> str:
    """One line: runner, state and duration, or the absence of a ledger doc."""
    if doc is None:
        return f"run {run_id}: no ledger doc"
    started, ended = to_utc(doc.get("started_at")), to_utc(doc.get("ended_at"))
    if run_is_stalled(doc, now=now):
        state, took = "STALLED", "died"
    elif doc.get("state") == RUNNING:
        state, took = RUNNING, "still open"
    else:
        state = str(doc.get("state"))
        took = (
            fmt_duration((ended - started).total_seconds())
            if started and ended
            else "?"
        )
    return (
        f"run {run_id}: {doc.get('runner')}, {state}, {took}, "
        f"started {fmt_time(started)}"
    )


# ------------------------------------------------------------------- stages


@dataclass
class Stage:
    agent: str
    started: LogLine | None
    finished: LogLine | None = None


def pair_stages(lines: list[LogLine]) -> list[Stage]:
    """Match each ``agent.finished`` to the earliest open ``agent.started``
    of the same agent. Input oldest first; stages come out in start order."""
    stages: list[Stage] = []
    open_by_agent: dict[str, list[Stage]] = {}
    for line in lines:
        agent = str(line.payload.get("agent") or "(no agent)")
        if line.message == "agent.started":
            stage = Stage(agent, line)
            stages.append(stage)
            open_by_agent.setdefault(agent, []).append(stage)
        elif line.message == "agent.finished":
            pending = open_by_agent.get(agent)
            if pending:
                pending.pop(0).finished = line
            else:
                stages.append(Stage(agent, None, line))
    return stages


def _still_alive(trace: RunTrace, stage: Stage) -> bool:
    """Could the run that opened this stage still be inside it?"""
    if trace.doc is not None:
        return trace.doc.get("state") == RUNNING and not run_is_stalled(
            trace.doc, now=trace.now
        )
    at = stage.started.at if stage.started else None
    return at is not None and trace.now - at < STALE_AFTER


def unfinished_lines(trace: RunTrace, stages: list[Stage]) -> list[str]:
    """The banner for stages that started and never finished."""
    out = []
    capped = bool(trace.stage_read and trace.stage_read.capped)
    for stage in stages:
        if stage.started is None or stage.finished is not None:
            continue
        where = f"{fmt_time(stage.started.at)} on {stage.started.service}"
        if capped:
            out.append(
                f"? {stage.agent} started {where}; its finish may lie past the "
                "stage read's cap — raise --max-entries"
            )
        elif _still_alive(trace, stage):
            out.append(f"… {stage.agent} started {where} and is still in progress")
        else:
            out.append(
                f"!!! DIED MID-STAGE: {stage.agent} started {where} and never "
                "logged agent.finished"
            )
    return out


def stage_lines(stages: list[Stage]) -> list[str]:
    if not stages:
        return ["no agent.started / agent.finished lines read"]
    out = []
    for stage in stages:
        start, end = stage.started, stage.finished
        if start is None and end is not None:
            out.append(
                f"{_short(end.at)}  {stage.agent}: finished "
                f"({end.payload.get('outcome')}) with no agent.started in the window"
            )
            continue
        assert start is not None
        if end is None:
            out.append(
                f"{_short(start.at)}  {stage.agent}: started on {start.service}, "
                "NEVER FINISHED"
            )
            continue
        ms = end.payload.get("duration_ms")
        took = fmt_duration(ms / 1000) if isinstance(ms, int | float) else "?"
        out.append(
            f"{_short(start.at)}  {stage.agent}: {end.payload.get('outcome')} "
            f"in {took} on {end.service}"
        )
    return out


# ----------------------------------------------------------------- problems


def _detail(line: LogLine) -> str:
    """The last line of an attached error or traceback, if any."""
    for key in ("exception", "error"):
        value = line.payload.get(key)
        if value:
            text = str(value).strip().splitlines()
            return text[-1] if text else ""
    return ""


def problem_lines(lines: list[LogLine]) -> list[str]:
    """Lines at WARNING or above, identical ones collapsed with a count."""
    groups: dict[tuple, list[LogLine]] = {}
    for line in lines:
        if _SEVERITY_RANK.get(line.severity, 0) < _WARNING:
            continue
        key = (line.severity, line.service, line.message, _detail(line))
        groups.setdefault(key, []).append(line)
    if not groups:
        return ["none at WARNING or above"]
    out = []
    for (severity, service, message, detail), group in groups.items():
        first, last = group[0].at, group[-1].at
        line = f"{_short(first)}  {severity:<8} {service}  {message}"
        if len(group) > 1:
            line += f"  (x{len(group)}, last {_short(last)})"
        out.append(line)
        if detail:
            out.append(f"{' ' * 12}{detail}")
    return out


# ------------------------------------------------------------------ retries


def retry_lines(lines: list[LogLine]) -> list[str]:
    """Attempts per Cloud Tasks task, and attempts that ended in a 5xx or no response."""
    tasks: dict[str, dict[tuple, LogLine]] = {}
    for line in lines:
        name = line.payload.get("task_name")
        if not name:
            continue
        key = (line.payload.get("retry_count"), line.payload.get("execution_count"))
        tasks.setdefault(str(name), {}).setdefault(key, line)
    if not tasks:
        return [
            "no Cloud Tasks fields on any line (not a queued run, or pre-dates them)"
        ]
    out = []
    for name, attempts in tasks.items():
        out.append(f"task {name}: {len(attempts)} attempt(s) seen")
        for (retries, executions), first in sorted(
            attempts.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or 0)
        ):
            row = (
                f"  {_short(first.at)}  retry_count={retries} "
                f"execution_count={executions} on {first.service}"
            )
            if (
                isinstance(retries, int)
                and isinstance(executions, int)
                and retries != executions
            ):
                # Cloud Tasks leaves 5xx failures out of the execution count,
                # and the request middleware answers an unhandled exception
                # with a 500 -- so the gap is usually this handler failing.
                row += (
                    f"  ! {retries - executions} attempt(s) ended in a 5xx or no "
                    "response: a handler crash (500), a timeout, or the platform"
                )
            out.append(row)
    return out


# ------------------------------------------------------------------ lineage


def lineage_lines(trace: RunTrace) -> list[str]:
    out = ["caused by"]
    if not (trace.parent_id or trace.origin_request_id):
        out.append(
            "  nothing recorded (a cron tick, a CLI run, a direct request, or a "
            "run predating origin tracking)"
        )
    if trace.parent_id:
        out.append(
            f"  {run_summary(trace.parent_id, trace.parent, trace.now)} "
            f"(from {trace.origin_source})"
        )
        out.append(f'    logs: jsonPayload.run_id="{trace.parent_id}"')
    if trace.origin_request_id:
        rid = trace.origin_request_id
        out.append(f"  request {rid} (from {trace.origin_source})")
        out.append(
            f'    logs: jsonPayload.request_id="{rid}" OR '
            f'jsonPayload.origin_request_id="{rid}"'
        )
    out.append("caused")
    if not trace.children:
        out.append(
            f"  nothing with origin_run_id={trace.run_id} in the ledger, or in "
            "logs over the window searched"
        )
    far = datetime.max.replace(tzinfo=UTC)

    def started(item):
        at = to_utc((item[1] or {}).get("started_at"))
        return (at is None, at or far)

    for child_id, doc in sorted(trace.children.items(), key=started):
        sources = " + ".join(sorted(trace.child_sources.get(child_id, ())))
        out.append(f"  {run_summary(child_id, doc, trace.now)} ({sources})")
    if trace.child_read and trace.child_read.capped:
        out.append(f"  ! children read capped at --max-entries {trace.max_entries}")
    return out


# ------------------------------------------------------------------- render


def header_lines(trace: RunTrace) -> list[str]:
    doc = trace.doc
    if doc is None:
        out = [
            f"no ledger doc at users/{trace.user_id}/{RUNS}/{trace.run_id} — "
            f"expired (kept {RETENTION_DAYS} days), never flushed, or a wrong "
            "id. That is not an empty run; logs were read anyway."
        ]
        runner = next(
            (ln.payload["runner"] for ln in trace.lines if ln.payload.get("runner")),
            None,
        )
        if runner:
            out.append(f"runner {runner} (from logs)")
        return out
    started, ended = to_utc(doc.get("started_at")), to_utc(doc.get("ended_at"))
    out = [f"runner {doc.get('runner')}, state {state_label(doc, trace.now)}"]
    if doc.get("state") == RUNNING:
        out.append(f"started {fmt_time(started)}, not closed")
    else:
        took = (
            fmt_duration((ended - started).total_seconds())
            if started and ended
            else "?"
        )
        out.append(f"{fmt_time(started)} → {fmt_time(ended)} ({took})")
    llm = doc.get("llm")
    llm = llm if isinstance(llm, dict) else {}
    if "cost_usd" in llm:
        out.append(f"cost ${llm['cost_usd']:.4f} over {llm.get('calls')} call(s)")
    else:
        out.append("cost: not recorded (the run never closed, or spent nothing)")
    if isinstance(doc.get("jobs"), dict):
        counts = ", ".join(f"{k}={v}" for k, v in sorted(doc["jobs"].items()))
        out.append(f"jobs: {counts}")
    else:
        out.append("jobs: no counts recorded")
    if doc.get("trigger"):
        out.append(f"trigger {doc['trigger']}")
    return out


def log_summary_lines(trace: RunTrace) -> list[str]:
    w = trace.window
    out = [f"searched {fmt_time(w.start)} → {fmt_time(w.end)} ({w.source})"]
    main = trace.main
    if main is None or not main.lines:
        out.append(
            "no Cloud Run log lines for this run in that window. Cloud Logging "
            f"keeps lines {LOG_RETENTION_DAYS} days by default, so an older "
            "run's logs have aged out; a run executed by a local cli process "
            "logs to its own stdout, not here. Absence is not evidence the run "
            "did nothing."
        )
        return out
    by_service = Counter(line.service for line in main.lines)
    services = ", ".join(f"{s} {n}" for s, n in by_service.most_common())
    out.append(f"{len(main.lines)} line(s): {services}")
    for name, read in (
        ("main", trace.main),
        ("stage", trace.stage_read),
        ("problem", trace.problem_read),
    ):
        if read and read.capped:
            last = read.lines[-1].at if read.lines else None
            out.append(
                f"! {name} read capped at --max-entries {trace.max_entries}; "
                f"nothing after {fmt_time(last)} was read"
            )
    return out


def render(trace: RunTrace) -> list[str]:
    """The whole report as lines. Pure: reads nothing, writes nothing."""
    out = [f"── users/{trace.user_id} run {trace.run_id} " + "─" * 20]
    out += [f"   {line}" for line in header_lines(trace)]
    lines = trace.lines
    stages = pair_stages(lines)
    banner = unfinished_lines(trace, stages)
    if banner:
        out.append("")
        out += [f"   {line}" for line in banner]

    sections = (
        ("logs", log_summary_lines(trace)),
        ("stages", stage_lines(stages)),
        ("problems", problem_lines(lines)),
        ("retries", retry_lines(lines)),
        ("lineage", lineage_lines(trace)),
    )
    for title, body in sections:
        out.append(f"\n   {title}")
        out += [f"     {line}" for line in body]

    out.append("\n   full log")
    out.append(f'     jsonPayload.run_id="{trace.run_id}"')
    out.append(
        f'     with what it caused: jsonPayload.run_id="{trace.run_id}" OR '
        f'jsonPayload.origin_run_id="{trace.run_id}"'
    )
    if trace.main:
        out.append(f"     as read here: {trace.main.filter}")
    return out


# -------------------------------------------------------------------- main


def pinned_project() -> str:
    """``GOOGLE_CLOUD_PROJECT``, never ADC's default quota project, which on a
    dev machine is a different project."""
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    assert project, "GOOGLE_CLOUD_PROJECT is unset; cannot pin the GCP project."
    return project


def found(trace: RunTrace) -> bool:
    return trace.doc is not None or bool(trace.lines) or bool(trace.children)


async def run(
    db,
    logs,
    project: str,
    user_id: str,
    run_id: str,
    *,
    max_entries: int,
    since: timedelta,
    now: datetime | None = None,
) -> int:
    """Print the trace; exit status 1 when neither a ledger doc nor any log
    line was found."""
    trace = await load_run_trace(
        db,
        logs,
        project,
        user_id,
        run_id,
        max_entries=max_entries,
        since=since,
        now=now,
    )
    print("\n".join(render(trace)))
    return 0 if found(trace) else 1


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--max-entries",
        type=positive_int,
        default=DEFAULT_MAX_ENTRIES,
        help=f"cap per log read (default {DEFAULT_MAX_ENTRIES})",
    )
    parser.add_argument(
        "--since",
        type=parse_since,
        default=parse_since(DEFAULT_SINCE),
        help=f"log window when there is no ledger doc (default {DEFAULT_SINCE})",
    )
    args = parser.parse_args()
    project = pinned_project()
    return await run(
        firestore.AsyncClient(project=project),
        cloud_logging.Client(project=project),
        project,
        args.user_id,
        args.run_id,
        max_entries=args.max_entries,
        since=args.since,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
