# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Print the life of one job as a single chronological timeline: discovered,
parsed, geo-gated, scored or discarded, returned to the user, decided,
tailored, submitted — plus the run that paid to score it and the Cloud Logging
filters that pull that run's logs.

Free and read-only: Firestore reads only, no write and no model call. Every
query is either a document get or a single-field equality / order, so none
needs a composite index; decisions and applications are sorted client-side.

Exposures cannot be queried by job id (``items`` is an array of maps), so only
the ``--exposures`` most recent are scanned, and the window scanned is always
printed beside the result.

Usage:
    python -m cli.trace_job --user-id me --job-id abc123
    python -m cli.trace_job --user-id me --job-id abc123 --exposures 2000
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# Pins GOOGLE_CLOUD_PROJECT from the project-root .env. ADC's default quota
# project on a dev machine is a different GCP project, so the client below is
# constructed with an explicit project rather than letting it be inferred.
load_dotenv()

JOBS = "jobs"
TOMBSTONES = "discarded_jobs"
DECISIONS = "decisions"
EXPOSURES = "exposures"
APPLICATIONS = "applications"
RUNS = "runs"

DEFAULT_EXPOSURES = 500

#: Key-absent marker, so "absent" and "present but None" stay distinct.
_ABSENT = object()


# ------------------------------------------------------------------ reading


@dataclass
class Trace:
    """Everything stored about one job, as read. Nothing here is derived."""

    user_id: str
    job_id: str
    job: dict | None = None
    tombstone: dict | None = None
    decisions: list[dict] = field(default_factory=list)
    applications: list[tuple[str, dict]] = field(default_factory=list)
    exposures: list[dict] = field(default_factory=list)
    exposure_limit: int = DEFAULT_EXPOSURES
    runs: dict[str, dict | None] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.job is not None or self.tombstone is not None


async def _stream(query) -> list:
    return [snap async for snap in query.stream()]


async def load_trace(
    db, user_id: str, job_id: str, *, exposure_limit: int = DEFAULT_EXPOSURES
) -> Trace:
    """Read every record about ``job_id``. Stops after the two document reads
    when the job is in neither ``jobs`` nor ``discarded_jobs``."""
    user_ref = db.collection("users").document(user_id)
    trace = Trace(user_id=user_id, job_id=job_id, exposure_limit=exposure_limit)

    snap = await user_ref.collection(JOBS).document(job_id).get()
    if snap.exists:
        trace.job = snap.to_dict() or {}
    snap = await user_ref.collection(TOMBSTONES).document(job_id).get()
    if snap.exists:
        trace.tombstone = snap.to_dict() or {}
    if not trace.found:
        return trace

    # Equality only: an order_by on another field would need a composite index.
    by_job = FieldFilter("job_id", "==", job_id)
    trace.decisions = [
        s.to_dict() or {}
        for s in await _stream(user_ref.collection(DECISIONS).where(filter=by_job))
    ]
    trace.applications = [
        (s.id, s.to_dict() or {})
        for s in await _stream(user_ref.collection(APPLICATIONS).where(filter=by_job))
    ]
    # Auto-ids are random, so "newest" has to be an explicit order.
    trace.exposures = [
        s.to_dict() or {}
        for s in await _stream(
            user_ref.collection(EXPOSURES)
            .order_by("shown_at", direction=firestore.Query.DESCENDING)
            .limit(exposure_limit)
        )
    ]

    for doc in (trace.job, trace.tombstone):
        run_id = (doc or {}).get("scored_run_id")
        if run_id and run_id not in trace.runs:
            run = await user_ref.collection(RUNS).document(run_id).get()
            trace.runs[run_id] = (run.to_dict() or {}) if run.exists else None
    return trace


# --------------------------------------------------------------- timestamps


def to_utc(value: Any) -> datetime | None:
    """A stored timestamp — ISO string or Firestore datetime — as aware UTC,
    or ``None`` when absent or unreadable. Naive values are taken as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def fmt_time(at: datetime | None) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%SZ") if at else "(no time)"


# ------------------------------------------------------------------- events


@dataclass
class Event:
    at: datetime | None
    title: str
    details: list[str] = field(default_factory=list)


def _score_of(doc: dict, *, tombstone: bool) -> Any:
    """``match.overall_score`` on a job doc, ``score`` on a tombstone. A job
    doc keeps nothing score-related at the top level."""
    if tombstone:
        return doc.get("score", _ABSENT)
    match = doc.get("match")
    if not isinstance(match, dict):
        return _ABSENT
    return match.get("overall_score", _ABSENT)


def provenance_lines(doc: dict) -> list[str]:
    """How the parse and score were produced, keeping absent, null and named
    distinct: a null ``parse_model`` is "not paid for here", never "unknown"."""
    if "scored_with" not in doc:
        return [
            "provenance: no scored_with record (no model ran here, or the job "
            "predates provenance)"
        ]
    sw = doc.get("scored_with")
    if not isinstance(sw, dict):
        return ["provenance: scored_with is null"]
    lines = []
    parse_model = sw.get("parse_model")
    if parse_model:
        lines.append(
            f"parse: {parse_model} (prompt {sw.get('parse_prompt_version') or '?'})"
        )
    else:
        lines.append(
            "parse: no parse paid here (already parsed, a jd_cache hit, or an "
            "earlier run)"
        )
    match_model = sw.get("match_model")
    if match_model:
        lines.append(
            f"score model: {match_model} "
            f"(prompt {sw.get('match_prompt_version') or '?'})"
        )
    else:
        lines.append("score model: none — the free pre-filter decided, not a model")
    return lines


def parse_lines(doc: dict) -> list[str]:
    if "jd_parsed" not in doc:
        return []
    parsed = doc.get("jd_parsed")
    if not isinstance(parsed, dict):
        return ["stored parse: none"]
    keys = ("role_family", "seniority", "remote_policy", "job_country")
    return ["stored parse: " + ", ".join(f"{k}={parsed.get(k)}" for k in keys)]


def geo_lines(doc: dict) -> list[str]:
    gate = doc.get("geo_gate")
    if not isinstance(gate, dict):
        return []
    mode = "ENFORCED skip" if gate.get("enforced") else "shadow"
    line = (
        f"geo gate v{gate.get('version')}: {gate.get('verdict')} "
        f"({gate.get('rule')}; residence {gate.get('residence_country')} → "
        f"job {gate.get('job_country')}; {mode})"
    )
    return [line]


def scored_event(doc: dict, run_id: str | None) -> Event | None:
    """The scoring step on a job doc, or ``None`` when it was never scored."""
    match = doc.get("match")
    if not isinstance(match, dict):
        return None
    sw = doc.get("scored_with") if isinstance(doc.get("scored_with"), dict) else {}
    at = to_utc(doc.get("scored_at")) or to_utc(sw.get("scored_at"))
    score = _score_of(doc, tombstone=False)
    details = [
        f"score {score if score is not _ABSENT else '(none)'}, "
        f"recommendation {match.get('recommendation')}"
    ]
    breakdown = match.get("breakdown")
    if isinstance(breakdown, dict):
        details.append(
            "breakdown: " + ", ".join(f"{k}={v}" for k, v in breakdown.items())
        )
    details += provenance_lines(doc) + parse_lines(doc) + geo_lines(doc)
    if doc.get("exploration") is True:
        details.append(
            "exploration sample: yes (surfaced from below the queue threshold by the sampler)"
        )
    details.append(f"scored by run {run_id}" if run_id else "no scored_run_id")
    return Event(at, "scored (jobs)", details)


def discarded_event(stone: dict) -> Event:
    score = _score_of(stone, tombstone=True)
    details = [
        f"score {score if score is not _ABSENT else '(none)'}, "
        f"recommendation {stone.get('recommendation')}"
    ]
    if stone.get("reasoning"):
        details.append(f"reasoning: {stone['reasoning']}")
    details += provenance_lines(stone) + parse_lines(stone) + geo_lines(stone)
    run_id = stone.get("scored_run_id")
    details.append(f"scored by run {run_id}" if run_id else "no scored_run_id")
    return Event(to_utc(stone.get("discarded_at")), "discarded (tombstone)", details)


def discovered_event(trace: Trace) -> Event | None:
    """From the job doc, or from a tombstone's ``restore`` payload, the only
    tombstones that keep ``discovered_at``."""
    doc = trace.job
    source = "jobs"
    if doc is None:
        restore = (trace.tombstone or {}).get("restore")
        doc, source = restore, "tombstone restore payload"
    if not isinstance(doc, dict) or "discovered_at" not in doc:
        return None
    return Event(
        to_utc(doc.get("discovered_at")),
        "discovered",
        [
            f"via {doc.get('discovered_via')} (from {source})",
            "no discovery run id is recorded on job documents",
        ],
    )


def decision_event(event: dict) -> Event:
    details = [f"by {event.get('actor', '(actor not recorded)')}"]
    snapshot = event.get("score_snapshot", _ABSENT)
    if snapshot is _ABSENT:
        details.append("score snapshot: not recorded")
    elif snapshot is None:
        details.append("score snapshot: none — the job was unscored when decided")
    else:
        details.append(
            f"score at decision: {snapshot.get('overall_score')} "
            f"({snapshot.get('recommendation')})"
        )
    shown = event.get("shown_at", _ABSENT)
    if shown is _ABSENT:
        details.append("answering exposure: not recorded")
    elif shown is None:
        details.append("answering exposure: none known (shelf, system, or logging off)")
    else:
        details.append(f"answering exposure: {fmt_time(to_utc(shown))}")
    title = f"decided: {event.get('previous_decision')} → {event.get('decision')}"
    return Event(to_utc(event.get("decided_at")), title, details)


def application_events(app_id: str, doc: dict) -> list[Event]:
    timeline = doc.get("timeline") or []
    if not timeline:
        return [Event(None, f"application {app_id}: {doc.get('status')}", [])]
    events = []
    for entry in timeline:
        if not isinstance(entry, dict):
            continue
        note = [entry["note"]] if entry.get("note") else []
        events.append(
            Event(
                to_utc(entry.get("at")),
                f"application {app_id}: {entry.get('status')}",
                note,
            )
        )
    return events


# ---------------------------------------------------------------- exposures


@dataclass
class ExposureRun:
    """Consecutive exposures that returned the job at the same rank."""

    rank: int | None
    count: int
    first: datetime | None
    last: datetime | None
    exploration: bool = False


def exposure_runs(exposures: list[dict], job_id: str) -> list[ExposureRun]:
    """Collapse consecutive appearances of ``job_id`` at one rank into a run.

    Polls write an exposure every few seconds, so one sighting is often
    hundreds of rows. A gap (an exposure without the job) or a rank change
    starts a new run. Input in any order; runs come out oldest first.
    """
    ordered = sorted(
        exposures,
        key=lambda e: to_utc(e.get("shown_at")) or datetime.min.replace(tzinfo=UTC),
    )
    runs: list[ExposureRun] = []
    current: ExposureRun | None = None
    for exposure in ordered:
        item = next(
            (i for i in exposure.get("items") or [] if i.get("job_id") == job_id),
            None,
        )
        if item is None:
            current = None
            continue
        at = to_utc(exposure.get("shown_at"))
        rank = item.get("rank")
        if current is not None and current.rank == rank:
            current.count += 1
            current.last = at
            continue
        current = ExposureRun(rank, 1, at, at, bool(item.get("exploration")))
        runs.append(current)
    return runs


def exposure_event(run: ExposureRun) -> Event:
    # Only rank 0 is on screen: the review page renders a one-card deck.
    if run.rank == 0:
        title = "shown at rank 0 (the card on screen)"
    else:
        title = f"returned at rank {run.rank} (in the candidate set, not on screen)"
    if run.count == 1:
        detail = "1 time"
    else:
        detail = (
            f"{run.count} times between {fmt_time(run.first)} and {fmt_time(run.last)}"
        )
    details = [detail]
    if run.exploration:
        details.append("as an exploration sample")
    return Event(run.first, title, details)


def exposure_summary(trace: Trace, runs: list[ExposureRun]) -> list[str]:
    """What window was scanned, so "not seen" is never read as "never shown"."""
    n = len(trace.exposures)
    if n == 0:
        return ["no exposures recorded — LOG_EXPOSURES may be off"]
    times = [t for t in (to_utc(e.get("shown_at")) for e in trace.exposures) if t]
    span = f"{fmt_time(min(times))} → {fmt_time(max(times))}" if times else "(no times)"
    lines = [f"scanned the {n} most recent exposure(s), {span}"]
    if n >= trace.exposure_limit:
        lines.append(
            f"window capped at --exposures {trace.exposure_limit}; older "
            "exposures were not scanned"
        )
    if runs:
        total = sum(r.count for r in runs)
        lines.append(f"job appears in {total} of them, in {len(runs)} run(s)")
    else:
        lines.append("job not returned in any scanned exposure")
    return lines


# ---------------------------------------------------------------- timeline


def timeline(trace: Trace) -> list[Event]:
    """Every dated step, oldest first; undated ones last, in source order."""
    events: list[Event] = []
    discovered = discovered_event(trace)
    if discovered:
        events.append(discovered)
    if trace.job is not None:
        scored = scored_event(trace.job, trace.job.get("scored_run_id"))
        if scored:
            events.append(scored)
    if trace.tombstone is not None:
        events.append(discarded_event(trace.tombstone))
    events += [exposure_event(r) for r in exposure_runs(trace.exposures, trace.job_id)]
    events += [decision_event(d) for d in trace.decisions]
    for app_id, doc in trace.applications:
        events += application_events(app_id, doc)
    # sorted() is stable, so undated events keep their source order.
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(events, key=lambda e: (e.at is None, e.at or far))


def run_lines(run_id: str, run: dict | None) -> list[str]:
    """The scoring run's ledger summary and the filters for its logs."""
    lines = [f"run {run_id}"]
    if run is None:
        lines.append(f"  no ledger doc at runs/{run_id} (expired, or never flushed)")
    else:
        llm = run.get("llm")
        llm = llm if isinstance(llm, dict) else {}
        lines.append(
            f"  runner {run.get('runner')}, state {run.get('state')}, "
            f"{run.get('started_at')} → {run.get('ended_at')}"
        )
        if "cost_usd" in llm:
            lines.append(
                f"  cost ${llm['cost_usd']:.4f} over {llm.get('calls')} call(s)"
            )
        else:
            lines.append("  cost: not recorded")
        if isinstance(run.get("jobs"), dict):
            counts = ", ".join(f"{k}={v}" for k, v in sorted(run["jobs"].items()))
            lines.append(f"  jobs: {counts}")
    lines.append(f'  logs:   jsonPayload.run_id="{run_id}"')
    origin_request = (run or {}).get("origin_request_id")
    if origin_request:
        lines.append(
            f'  caused by request: jsonPayload.request_id="{origin_request}" '
            f'OR jsonPayload.origin_request_id="{origin_request}"'
        )
    origin_run = (run or {}).get("origin_run_id")
    if origin_run:
        lines.append(f'  caused by run:     jsonPayload.run_id="{origin_run}"')
    return lines


def render(trace: Trace) -> list[str]:
    """The whole report as lines. Pure: reads nothing, writes nothing."""
    out = [f"── users/{trace.user_id} job {trace.job_id} " + "─" * 20]
    if not trace.found:
        out.append(
            f"   not found in users/{trace.user_id}/{JOBS} or "
            f"users/{trace.user_id}/{TOMBSTONES}"
        )
        return out

    head = trace.job if trace.job is not None else trace.tombstone or {}
    out.append(f"   {head.get('company')} — {head.get('title')}")
    if head.get("url"):
        out.append(f"   {head['url']}")
    if trace.job is not None and trace.tombstone is not None:
        out.append(
            "   ! in BOTH jobs and discarded_jobs (a geo_resurrect restore?); "
            "both are shown below"
        )
    elif trace.job is not None:
        out.append(f"   in jobs, user_decision={trace.job.get('user_decision')}")
    else:
        out.append("   in discarded_jobs only")

    runs = exposure_runs(trace.exposures, trace.job_id)
    out.append("\n   exposures")
    out += [f"     {line}" for line in exposure_summary(trace, runs)]

    out.append("\n   timeline")
    for event in timeline(trace):
        out.append(f"     {fmt_time(event.at):<20}  {event.title}")
        out += [f"{' ' * 27}{line}" for line in event.details]

    out.append("\n   scoring run")
    if not trace.runs:
        out.append("     no scored_run_id on any record")
    for run_id, run in trace.runs.items():
        out += [f"     {line}" for line in run_lines(run_id, run)]
    return out


# -------------------------------------------------------------------- main


def firestore_client() -> firestore.AsyncClient:
    """An async client pinned to ``GOOGLE_CLOUD_PROJECT``, never to ADC's
    default quota project, which on a dev machine is a different project."""
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    assert project, "GOOGLE_CLOUD_PROJECT is unset; cannot pin the Firestore project."
    return firestore.AsyncClient(project=project)


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


async def run(db, user_id: str, job_id: str, *, exposure_limit: int) -> int:
    """Print the trace; exit status 1 when the job is in neither collection."""
    trace = await load_trace(db, user_id, job_id, exposure_limit=exposure_limit)
    print("\n".join(render(trace)))
    return 0 if trace.found else 1


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--exposures",
        type=positive_int,
        default=DEFAULT_EXPOSURES,
        help=f"how many recent exposures to scan (default {DEFAULT_EXPOSURES})",
    )
    args = parser.parse_args()
    return await run(
        firestore_client(), args.user_id, args.job_id, exposure_limit=args.exposures
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
