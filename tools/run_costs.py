# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-run LLM spend ledger at ``users/{uid}/runs/{run_id}``.

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
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore

from obs.llm_cost import reset_run_cost, run_cost_snapshot
from obs.logging import get_logger

log = get_logger("tools.run_costs")

COLLECTION = "runs"


def _increments(counts: dict[str, Any]) -> dict[str, Any]:
    """Turn a flat count map into Increments so late writes add, not replace."""
    return {key: firestore.Increment(value) for key, value in counts.items()}


async def persist_run_cost(
    db,
    user_id: str,
    run_id: str,
    *,
    jobs: dict[str, int] | None = None,
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
    what the originating cycle recorded.

    Telemetry must never fail a pipeline, so a Firestore error is logged and
    swallowed. The accumulator is dropped either way: whatever this write
    missed is worth less than double-counting it on a later flush.
    """
    try:
        totals = run_cost_snapshot(run_id)
        by_step = totals.pop("by_step")
        doc: dict[str, Any] = {
            "run_id": run_id,
            "user_id": user_id,
            "ended_at": datetime.now(UTC).isoformat(),
            **{key: value for key, value in meta.items() if value is not None},
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
            calls=totals["calls"],
            cost_usd=totals["cost_usd"],
        )
    except Exception:
        log.exception("run_cost.persist_failed", run_id=run_id)
    finally:
        reset_run_cost(run_id)
