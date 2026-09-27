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
(``RETENTION_DAYS`` out) for Firestore's TTL to collect. Writing the field is
all the client library can do — **the policy itself is a per-collection-group
setting in GCP and is not in this repo or in terraform.** Until it is enabled
the field is inert and nothing is ever deleted. To activate it, once per
database:

    gcloud firestore fields ttls update expires_at \
        --collection-group=runs \
        --enable-ttl \
        --database='(default)' \
        --project="$GOOGLE_CLOUD_PROJECT"

    # verify (state should read ACTIVE, after a one-off index build):
    gcloud firestore fields ttls list --database='(default)'

``--collection-group=runs`` matches *every* collection named ``runs`` at any
depth; ``users/{uid}/runs`` is the only one. Docs written before the policy
existed carry no ``expires_at`` and are never collected — TTL ignores a
missing or non-timestamp field rather than deleting the doc, which is also why
``expires_at`` is written as a plain ``datetime`` literal and not an ISO
string like ``ended_at``. It is deliberately kept out of the ``jobs``/``llm``
maps for the same reason: ``_increments`` would turn it into an Increment,
which is not a timestamp and would silently disable collection for that doc.

Liveness
--------
The ledger is also the product's **record that a run exists**. It used to be
written only from ``persist_run_cost``'s ``finally``, so an in-flight run had
no document at all and a run killed by ``CancelledError`` or SIGKILL never got
one — 67 live docs, every one already closed. :func:`open_run` now writes
``state: "running"`` the moment a run starts, and ``persist_run_cost`` closes
it with the caller's own outcome. That makes "is anything happening?"
answerable from one query (``runs where state == "running"``) instead of from
client state, which is what ``GET /activity`` reads.

``state`` is a **plain field**, never a member of the ``jobs``/``llm`` maps:
``_increments`` would call ``firestore.Increment("done")``, which raises
``ValueError: Pass an integer / float value.``

A SIGKILLed process leaves a permanently open doc, and that is intended.
Staleness is **derived, not heartbeated** (see
``api.routes.discovery._LEASE_SECONDS`` for the reasoning, which is the same
reasoning and is settled): past :data:`STALE_AFTER` an open doc reads
``stalled``, which is more honest than no record at all, and needs no reaper.
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
#: not among them — it is *derived* from ``RUNNING`` plus age (see
#: :func:`run_is_stalled`), so nothing has to write it and no reaper has to run.
RUNNING = "running"
DONE = "done"
FAILED = "failed"

#: Same grace as the slot lease, and for the same reason: a ceiling shorter
#: than the longest run a live process could still be inside would report
#: healthy runs as stalled.
_LEASE_GRACE_SECONDS = 60

#: How long an open ledger doc may stay open before it stops being evidence
#: that anything is running. Cloud Tasks abandons a dispatch at
#: ``_DISPATCH_DEADLINE_SECONDS`` (1800), so past 1860s no queued run can still
#: be alive as far as the queue is concerned, and an open doc means the process
#: died without closing it.
STALE_AFTER = timedelta(seconds=_DISPATCH_DEADLINE_SECONDS + _LEASE_GRACE_SECONDS)

# How long a ledger doc lives once the TTL policy above is enabled. These are
# cost records, and the question they answer ("what did this month cost, and
# how does that compare?") is month-over-month — so a year plus a margin, not
# days. 400 rather than 365 so a review run late in a month can still see the
# same month a year earlier; below that, the year-ago comparison silently
# vanishes mid-review.
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

    Pure, and the only definition of ``stalled`` in the codebase. A closed doc
    is never stalled — it has an outcome. An open one with no readable
    ``started_at`` *is*, because it cannot be aged and "it is running" is the
    dishonest direction to guess in.
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

    The counterpart to :func:`persist_run_cost`, and the whole reason the run
    ledger can answer "is anything happening right now?". Written with
    ``set(merge=True)`` so it composes with the close (and with a batch
    ingest's later wave) rather than racing it.

    ``db`` resolution, the ``None``-dropping and the swallowed Firestore error
    are all :func:`persist_run_cost`'s, for the same reasons — a liveness
    record that could fail a pipeline would be worse than no liveness record.
    """
    try:
        now = datetime.now(UTC)
        doc: dict[str, Any] = {
            "run_id": run_id,
            "user_id": user_id,
            "state": RUNNING,
            "runner": runner,
            "started_at": started_at,
            # Stamped here too: a run that dies without ever closing must not
            # be the one doc the TTL can never collect.
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

    ``db`` is a Firestore client *or* a zero-arg factory for one (``_client``,
    ``firestore.AsyncClient``); a factory is preferred, because every caller
    flushes from a ``finally`` and building a client lazily runs
    ``google.auth.default()``, which can raise. Resolving it inside the guard
    below is what keeps that failure out of the caller.

    ``meta`` sets the doc's scalar fields (runner, trigger, started_at,
    batch_run, ...). ``None`` values are dropped so a later write — the batch
    ingest, arriving hours after the cycle that ordered it — can't blank out
    what the originating cycle recorded. That is also why callers no longer
    re-send ``started_at`` on close: :func:`open_run` already wrote it, and the
    close must not move it.

    ``state`` is this run's own outcome and an **explicit parameter**, so it
    can never be mistaken for a count: every leaf under ``jobs``/``llm`` goes
    through :func:`_increments`, and ``firestore.Increment("done")`` raises. A
    caller whose work is still outstanding — a cycle that submitted a Vertex
    batch and will be closed by the ingest hours later — passes
    ``state=RUNNING`` and keeps the doc open.

    Telemetry must never fail a pipeline, so a Firestore error is logged and
    swallowed. The accumulator is dropped either way: whatever this write
    missed is worth less than double-counting it on a later flush.
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
            # the module docstring's Retention section. Re-stamped on every
            # wave, so a doc a batch ingest is still adding to keeps its full
            # retention from the *last* write rather than the first.
            "expires_at": now + timedelta(days=RETENTION_DAYS),
            **{key: value for key, value in meta.items() if value is not None},
            # A plain field, deliberately outside every map ``_increments``
            # touches, and written last so no ``meta`` key can shadow it — see
            # the docstring.
            "state": state,
            "llm": _increments(totals),
        }
        # Only written when non-empty: an empty map is not a transform, so it
        # would land in the update mask as a literal and wipe the breakdown a
        # previous wave of this same run already recorded. (``llm`` is safe —
        # every leaf there is an Increment.)
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
        # Both Firestore clients are in play across the call sites — the API
        # routes hold the sync one, the pipelines and CLIs an AsyncClient. The
        # sync client's set() blocks on network I/O, and every caller here is
        # async, so it goes to a thread rather than stalling the event loop.
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
