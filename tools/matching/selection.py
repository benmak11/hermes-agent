# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Which unscored jobs get the scarce scoring slots, behind ``PRERANK_MODE``.

``off`` (the default) is the historical pick, made by
``score.load_profile_and_pending`` itself: the first ``limit`` unscored pending
docs in stream order. Firestore streams in document-id order and ids are
hashes, so that pick is content-random.

``shadow`` makes exactly that pick, and logs beside it the top ``limit`` the
prerank would have chosen. ``on`` fills each slot with the best remaining job
by :func:`prerank.sort_key`, or, with probability ``SELECTION_EXPLORE_RATE``,
a uniformly random remaining one, so the prerank stays measurable against an
unbiased sample.

Both non-off modes stream the **whole** unscored pending pool (projected), one
read per document, where ``off`` stops after ``limit`` unscored docs. Each pick
appends one :mod:`tools.selections` record.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
from collections.abc import Mapping
from dataclasses import dataclass

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from obs.logging import get_logger
from tools import selections
from tools.matching.pipeline import geo_enforce_enabled
from tools.matching.prerank import (
    PRERANK_VERSION,
    Prerank,
    preferences_from,
    prerank,
    residence_country,
    sort_key,
)

log = get_logger("tools.matching.selection")

OFF = "off"
SHADOW = "shadow"
ON = "on"
MODES = frozenset({OFF, SHADOW, ON})

ARM_RANK = "rank"
ARM_EXPLORE = "explore"
ARM_BASELINE = "baseline"

#: Never 0 by fallback: without explore picks the prerank cannot be evaluated.
DEFAULT_EXPLORE_RATE = 1 / 3

#: Everything :func:`prerank.prerank` reads, plus ``match`` to drop scored docs.
POOL_FIELDS = ["title", "company", "location", "discovered_at", "jd_parsed", "match"]

_warned_modes: set[str] = set()


def prerank_mode() -> str:
    """``PRERANK_MODE``: ``off`` (default), ``shadow`` or ``on``, read per call.

    Any other value means ``off`` and logs a warning once per process.
    """
    raw = os.getenv("PRERANK_MODE", "").strip().lower()
    if not raw:
        return OFF
    if raw in MODES:
        return raw
    if raw not in _warned_modes:
        _warned_modes.add(raw)
        log.warning("selection.mode_env_invalid", value=raw[:40])
    return OFF


def explore_rate() -> float:
    """``SELECTION_EXPLORE_RATE`` clamped to [0, 1]; default 1/3.

    Unparseable or non-finite falls back to the default, not to 0. Distinct
    from ``score.EXPLORATION_RATE``, which acts after scoring.
    """
    raw = os.getenv("SELECTION_EXPLORE_RATE", "").strip()
    if not raw:
        return DEFAULT_EXPLORE_RATE
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        log.warning("selection.explore_rate_env_invalid", value=raw[:40])
        return DEFAULT_EXPLORE_RATE
    return min(max(value, 0.0), 1.0)


@dataclass
class Candidate:
    """One unscored pool job: its prerank and its 1-based rank in the pool."""

    job_id: str
    rank: Prerank
    key: tuple
    rank_in_pool: int = 0


async def _pool(
    jobs_ref, user_doc: Mapping | None, *, enforce_geo: bool
) -> list[Candidate]:
    """Every unscored pending job, preranked, in stream order. One projected
    query, no ``order_by``: sorting on a new field would need a composite
    index."""
    prefs = preferences_from(user_doc)
    residence = residence_country(user_doc)
    query = jobs_ref.where(filter=FieldFilter("user_decision", "==", "pending"))
    pool: list[Candidate] = []
    async for snap in query.select(POOL_FIELDS).stream():
        doc = snap.to_dict() or {}
        if "match" in doc:
            continue
        rank = prerank(
            {**doc, "id": snap.id}, prefs, residence, enforce_geo=enforce_geo
        )
        pool.append(
            Candidate(
                snap.id, rank, sort_key(rank.score, doc.get("discovered_at"), snap.id)
            )
        )
    for position, candidate in enumerate(sorted(pool, key=lambda c: c.key), 1):
        candidate.rank_in_pool = position
    return pool


def choose(
    pool: list[Candidate],
    limit: int,
    mode: str,
    *,
    rate: float,
    rng: random.Random,
) -> list[tuple[Candidate, str]]:
    """The ``(candidate, arm)`` picks for ``limit`` slots, in pick order.

    ``shadow`` takes the first ``limit`` in stream order, as ``off`` does.
    ``on`` draws one slot at a time from what remains.
    """
    if mode == SHADOW:
        return [(c, ARM_BASELINE) for c in pool[:limit]]
    remaining = sorted(pool, key=lambda c: c.key)
    picks: list[tuple[Candidate, str]] = []
    while remaining and len(picks) < limit:
        if rate > 0 and rng.random() < rate:
            picks.append((remaining.pop(rng.randrange(len(remaining))), ARM_EXPLORE))
        else:
            picks.append((remaining.pop(0), ARM_RANK))
    return picks


async def select_pending(
    db: firestore.AsyncClient,
    user_id: str,
    user_doc: Mapping | None,
    limit: int,
    *,
    mode: str,
    rng: random.Random | None = None,
) -> list[tuple]:
    """``(doc_ref, Job)`` pairs for up to ``limit`` unscored pending jobs, in
    pick order, chosen under ``mode`` (``shadow`` or ``on``).

    The chosen docs are re-read in full after the projection, and one that has
    since vanished, been scored or left ``pending`` is skipped, so this can
    return **fewer than** ``limit``. Writes one selections record for what it
    returns; that write never raises.
    """
    user_ref = db.collection("users").document(user_id)
    jobs_ref = user_ref.collection("jobs")
    pool = await _pool(jobs_ref, user_doc, enforce_geo=geo_enforce_enabled())
    rate = explore_rate() if mode == ON else None
    picks = choose(pool, limit, mode, rate=rate or 0.0, rng=rng or random.Random())

    refs = [jobs_ref.document(c.job_id) for c, _ in picks]
    snaps = await asyncio.gather(*(ref.get() for ref in refs))
    pending: list[tuple] = []
    items: list[selections.SelectionItem] = []
    for ref, snap, (candidate, arm) in zip(refs, snaps, picks, strict=True):
        doc = snap.to_dict() if snap.exists else None
        if not doc or "match" in doc or doc.get("user_decision") != "pending":
            log.info("selection.skipped_stale", job_id=candidate.job_id)
            continue
        pending.append((ref, Job.model_validate(doc)))
        items.append(
            selections.SelectionItem(
                job_id=candidate.job_id,
                arm=arm,
                prerank=candidate.rank.score,
                features=dict(candidate.rank.features),
                signals=dict(candidate.rank.signals),
                rank_in_pool=candidate.rank_in_pool,
            )
        )

    shadow_top = None
    if mode == SHADOW:
        shadow_top = [
            selections.ShadowItem(job_id=c.job_id, prerank=c.rank.score)
            for c in sorted(pool, key=lambda c: c.key)[:limit]
        ]
    log.info(
        "selection.picked",
        mode=mode,
        pool=len(pool),
        chosen=[item["job_id"] for item in items],
        arms=[item["arm"] for item in items],
        shadow_top=[item["job_id"] for item in shadow_top or []] or None,
    )
    await selections.log_selection(
        user_ref,
        mode=mode,
        prerank_version=PRERANK_VERSION,
        explore_rate=rate,
        pool_size=len(pool),
        items=items,
        shadow_top=shadow_top,
    )
    return pending
