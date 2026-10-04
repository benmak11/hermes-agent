# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
r"""Per-run LLM spend ledger at ``users/{uid}/runs/{run_id}``.

``obs.llm_cost`` accumulates every priced call in process, keyed by the
``run_id`` bound by ``run_context``; this module banks that total in Firestore
when the run ends. Deliberately not via the ``llm.call`` log → BigQuery sink:
that only exists on Cloud Run, so a CLI run's spend would evaporate, and it is
the ledger — not a log query — that "a first discovery cycle costs under $X,
measured" has to be answerable from.

Every numeric leaf is written as a ``firestore.Increment`` under
``set(merge=True)``, because one run's spend lands in waves: the cycle that
submitted a Vertex batch pays nothing yet, and the worker's resume pass prices
those calls hours later under the *same* run_id (see
``tools.matching.batch_runs.resume``). Adding lets the late arrival join the
originating cycle's doc instead of clobbering it.

Retention
---------
Every cycle leaves one doc here forever, so each write stamps ``expires_at``
(``RETENTION_DAYS`` out) for Firestore's TTL to collect. The policy itself is
a per-collection-group setting in GCP, not in this repo or in terraform, and
until it is enabled the field is inert and nothing is deleted. Once per
database:

    gcloud firestore fields ttls update expires_at \
        --collection-group=runs \
        --enable-ttl \
        --database='(default)' \
        --project="$GOOGLE_CLOUD_PROJECT"

    # verify (state should read ACTIVE, after a one-off index build):
    gcloud firestore fields ttls list --database='(default)'

``--collection-group=runs`` matches every collection named ``runs`` at any
depth; ``users/{uid}/runs`` is the only one. TTL ignores a missing or
non-timestamp field rather than deleting the doc, so docs predating the policy
are never collected — and so ``expires_at`` must be a plain ``datetime``, not
an ISO string like ``ended_at``, and must stay out of the ``jobs``/``llm``
maps, where ``_increments`` would turn it into an Increment and silently
disable collection.

Liveness
--------
The ledger is also the record that a run exists. :func:`open_run` writes
``state: "running"`` when a run starts and :func:`persist_run_cost` closes it
with the caller's outcome, so "is anything happening?" is one query
(``runs where state == "running"``) rather than client state — which is what
``GET /activity`` reads. Written only on close, a run killed by
``CancelledError`` or SIGKILL left no document at all.

``state`` is a plain field, never a member of the ``jobs``/``llm`` maps:
``_increments`` would call ``firestore.Increment("done")``, which raises.

A SIGKILLed process leaves a permanently open doc, which is intended.
Staleness is derived rather than heartbeated: past :data:`STALE_AFTER` an open
doc reads ``stalled``, which needs no reaper and is more honest than no record.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore

from obs.llm_cost import reset_run_cost, run_cost_snapshot
from obs.logging import get_logger

# The staleness ceiling is defined *against* the dispatch deadline rather than
# restated, exactly as ``tools.applications.state`` imports it. tools.queues
# imports nothing from here.
from tools.queues import _DISPATCH_DEADLINE_SECONDS

log = get_logger("tools.run_costs")

COLLECTION = "runs"

#: The three values ``state`` ever holds on disk. ``stalled`` is deliberately
#: not among them: it is derived from ``RUNNING`` plus age (see
#: :func:`run_is_stalled`), so nothing has to write it.
RUNNING = "running"
DONE = "done"
FAILED = "failed"

#: Same grace as the slot lease, and for the same reason: a ceiling shorter
#: than the longest run a live process could still be inside would report
#: healthy runs as stalled.
_LEASE_GRACE_SECONDS = 60

#: How long an open ledger doc may stay open before it stops being evidence
#: that anything is running. Cloud Tasks abandons a dispatch at
#: ``_DISPATCH_DEADLINE_SECONDS``, so past that plus the grace no queued run
#: can still be alive and an open doc means the process died without closing.
STALE_AFTER = timedelta(seconds=_DISPATCH_DEADLINE_SECONDS + _LEASE_GRACE_SECONDS)

# How long a ledger doc lives once the TTL policy above is enabled. 400 rather
# than 365 so a review late in a month can still see the same month a year
# earlier; below that the year-ago comparison silently vanishes mid-review.
RETENTION_DAYS = 400


def _increments(counts: dict[str, Any]) -> dict[str, Any]:
    """Turn a flat count map into Increments so late writes add, not replace."""
    return {key: firestore.Increment(value) for key, value in counts.items()}


def _parse_iso(value: Any) -> datetime | None:
    """A stored timestamp as an aware datetime, or ``None`` if unusable."""
    if isinstance(value, datetime):  # Firestore hands timestamps back as these
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def run_is_stalled(doc: dict, *, now: datetime | None = None) -> bool:
    """Has this open ledger doc outlived any run that could still be alive?

    Pure, and the only definition of ``stalled`` in the codebase. A closed
    doc is never stalled; an open one with no readable ``started_at`` is,
    since it cannot be aged and "running" is the dishonest guess.
    """
    if doc.get("state") != RUNNING:
        return False
    started = _parse_iso(doc.get("started_at"))
    if started is None:
        return True
    return (now or datetime.now(UTC)) - started >= STALE_AFTER


async def open_run(
    db,
    user_id: str,
    run_id: str,
    *,
    runner: str,
    trigger: str | None = None,
    started_at: str,
) -> None:
    """Record that ``run_id`` has **started**, before it has done anything.

    Writes to Firestore with ``set(merge=True)`` so it composes with the
    close, and with a batch ingest's later wave, rather than racing them.

    ``db`` resolution and the swallowed Firestore error are
    :func:`persist_run_cost`'s: a liveness record must never fail a pipeline.
    """
    try:
        now = datetime.now(UTC)
        doc: dict[str, Any] = {
            "run_id": run_id,
            "user_id": user_id,
            "state": RUNNING,
            "runner": runner,
            "started_at": started_at,
            # Stamped here too, so a run that dies without closing is still
            # collectable by the TTL.
            "expires_at": now + timedelta(days=RETENTION_DAYS),
        }
        if trigger is not None:
            doc["trigger"] = trigger

        client = db() if callable(db) else db
        ref = (
            client.collection("users")
            .document(user_id)
            .collection(COLLECTION)
            .document(run_id)
        )
        if inspect.iscoroutinefunction(ref.set):
            await ref.set(doc, merge=True)
        else:
            await asyncio.to_thread(ref.set, doc, merge=True)
        log.info("run.opened", run_id=run_id, runner=runner)
    except Exception:
        log.exception("run_cost.open_failed", run_id=run_id)


async def persist_run_cost(
    db,
    user_id: str,
    run_id: str,
    *,
    jobs: dict[str, int] | None = None,
    state: str = DONE,
    **meta: Any,
) -> None:
    """Flush ``run_id``'s accumulated spend onto its ledger doc.

    ``db`` is a Firestore client or a zero-arg factory for one; a factory is
    preferred, because callers flush from a ``finally`` and building a client
    lazily runs ``google.auth.default()``, which can raise inside the guard
    here rather than in the caller.

    ``meta`` sets the doc's scalar fields. ``None`` values are dropped so a
    later write — a batch ingest arriving hours after the cycle that ordered
    it — cannot blank out what the originating cycle recorded; for the same
    reason the close must not re-send ``started_at``.

    ``state`` is an explicit parameter so it can never be mistaken for a count
    and passed through :func:`_increments`. A caller whose work is still
    outstanding passes ``state=RUNNING`` and keeps the doc open.

    Writes to Firestore; errors are logged and swallowed, since telemetry must
    never fail a pipeline. The accumulator is dropped either way, because
    losing this write is better than double-counting it on the next.
    """
    try:
        totals = run_cost_snapshot(run_id)
        by_step = totals.pop("by_step")
        now = datetime.now(UTC)
        doc: dict[str, Any] = {
            "run_id": run_id,
            "user_id": user_id,
            "ended_at": now.isoformat(),
            # A plain datetime, not an Increment and not an ISO string: see
            # Retention above. Re-stamped on every wave, so a doc a batch
            # ingest is still adding to keeps its retention from the last.
            "expires_at": now + timedelta(days=RETENTION_DAYS),
            **{key: value for key, value in meta.items() if value is not None},
            # A plain field, outside every map ``_increments`` touches, and
            # written last so no ``meta`` key can shadow it.
            "state": state,
            "llm": _increments(totals),
        }
        # Only written when non-empty: an empty map is not a transform, so it
        # lands in the update mask as a literal and wipes the breakdown an
        # earlier wave of this run recorded. (``llm`` is safe — all Increments.)
        if by_step:
            doc["by_step"] = {step: _increments(c) for step, c in by_step.items()}
        if jobs:
            doc["jobs"] = _increments(jobs)

        client = db() if callable(db) else db
        ref = (
            client.collection("users")
            .document(user_id)
            .collection(COLLECTION)
            .document(run_id)
        )
        # Both Firestore clients are in play across the call sites. The sync
        # client's set() blocks on network I/O and every caller here is async,
        # so it goes to a thread rather than stalling the event loop.
        if inspect.iscoroutinefunction(ref.set):
            await ref.set(doc, merge=True)
        else:
            await asyncio.to_thread(ref.set, doc, merge=True)
        log.info(
            "run_cost.persisted",
            run_id=run_id,
            state=state,
            calls=totals["calls"],
            cost_usd=totals["cost_usd"],
        )
    except Exception:
        log.exception("run_cost.persist_failed", run_id=run_id)
    finally:
        reset_run_cost(run_id)
