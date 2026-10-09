# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared per-board health: one ``board_health/{platform}:{slug}`` doc per board.

Discovery records what each fresh board fetch answered, so a board that moved
platform or died stops looking like an empty one. The records are shared by
every user and hold no user data: a board answers the same whoever asks, which
is also why ``cli/reset_user.py`` and the account wipe leave them alone.

Detection only. Nothing here changes which boards are fetched.

Read once per cycle (one stream of the collection) and written only where a
stored field would change, with absolute values so two users' cycles on the
same day are idempotent. Every failure is logged and swallowed: health
tracking must never fail a search.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from obs.logging import get_logger
from tools.ats._http import NOT_FOUND, OK

log = get_logger("tools.ats.board_health")

COLLECTION = "board_health"

#: Board platforms. ``google_jobs`` / ``meta_jobs`` slugs are search queries,
#: not boards, so they get no record.
TRACKED_PLATFORMS = frozenset({"greenhouse", "lever", "ashby"})

STATE_OK = "ok"
STATE_FAILING = "failing"

#: Concurrent writes. A first cycle writes every board once; after that a cycle
#: writes only the boards whose state moved.
_WRITE_CONCURRENCY = 20

#: Fields whose change earns a write. ``updated_at`` is not one of them.
_COMPARED = (
    "platform",
    "slug",
    "state",
    "last_outcome",
    "last_status",
    "last_ok_at",
    "failing_since",
    "not_found_days",
    "last_not_found_day",
)


def doc_id(platform: str, slug: str) -> str:
    """The record's id. The slug keeps its case: Lever slugs are case-sensitive."""
    return f"{platform}:{slug}"


def _now() -> datetime:
    return datetime.now(UTC)


def next_record(
    prev: dict | None,
    platform: str,
    slug: str,
    outcome: str,
    status: int | None,
    now: datetime,
) -> dict:
    """The record after one observation, before ``updated_at`` is stamped.

    ``last_ok_at`` moves only when the board turns ok (or is first seen ok):
    a board that stays ok is not rewritten.
    """
    prev = prev or {}
    was_ok = prev.get("state") == STATE_OK
    was_failing = prev.get("state") == STATE_FAILING
    now = now.astimezone(UTC)
    stamp = now.isoformat()
    record: dict[str, Any] = {
        "platform": platform,
        "slug": slug,
        "last_outcome": outcome,
        "last_status": status,
    }
    if outcome == OK:
        record.update(
            state=STATE_OK,
            last_ok_at=prev.get("last_ok_at") if was_ok else stamp,
            failing_since=None,
            not_found_days=0,
            last_not_found_day=None,
        )
        return record
    days = (prev.get("not_found_days") or 0) if was_failing else 0
    last_day = prev.get("last_not_found_day") if was_failing else None
    today = now.date().isoformat()
    if outcome == NOT_FOUND and last_day != today:
        days, last_day = days + 1, today
    record.update(
        state=STATE_FAILING,
        last_ok_at=prev.get("last_ok_at"),
        failing_since=prev.get("failing_since") if was_failing else stamp,
        not_found_days=days,
        last_not_found_day=last_day,
    )
    return record


def _changed(prev: dict | None, record: dict) -> bool:
    if prev is None:
        return True
    return any(prev.get(k) != record.get(k) for k in _COMPARED)


async def _read_all(db) -> dict[str, dict]:
    return {
        snap.id: snap.to_dict() or {}
        async for snap in db.collection(COLLECTION).stream()
    }


async def record_outcomes(
    db,
    observations: list[tuple[str, str, str, int | None]],
    now: datetime | None = None,
) -> dict[str, int]:
    """Fold this cycle's ``(platform, slug, outcome, status)`` into the records.

    Untracked platforms are ignored; a board observed twice keeps the last
    observation. Returns ``{"read", "written", "changed", "errors"}`` and never
    raises.
    """
    counts = {"read": 0, "written": 0, "changed": 0, "errors": 0}
    latest: dict[str, tuple[str, str, str, int | None]] = {}
    for platform, slug, outcome, status in observations:
        if platform in TRACKED_PLATFORMS and slug and "/" not in slug:
            latest[doc_id(platform, slug)] = (platform, slug, outcome, status)
    if not latest:
        return counts
    now = (now or _now()).astimezone(UTC)
    try:
        existing = await _read_all(db)
    except Exception as e:
        # Writing without the previous state would reset the 404 day count.
        log.warning("board_health.read_failed", error=f"{type(e).__name__}: {e}")
        counts["errors"] += 1
        return counts
    counts["read"] = len(existing)

    pending: list[tuple[str, dict]] = []
    for key, (platform, slug, outcome, status) in latest.items():
        prev = existing.get(key)
        record = next_record(prev, platform, slug, outcome, status, now)
        if not _changed(prev, record):
            continue
        record["updated_at"] = now.isoformat()
        pending.append((key, record))
        fields = {
            "platform": platform,
            "slug": slug,
            "outcome": outcome,
            "status": status,
        }
        if prev is None:
            log.info("board_health.first_seen", state=record["state"], **fields)
        elif prev.get("state") != record["state"]:
            counts["changed"] += 1
            log.warning(
                "board_health.changed",
                from_state=prev.get("state"),
                to_state=record["state"],
                not_found_days=record["not_found_days"],
                **fields,
            )

    sem = asyncio.Semaphore(_WRITE_CONCURRENCY)

    async def _write(key: str, record: dict) -> None:
        async with sem:
            await db.collection(COLLECTION).document(key).set(record)

    results = await asyncio.gather(
        *(_write(k, r) for k, r in pending), return_exceptions=True
    )
    failed = [
        (k, r)
        for (k, _), r in zip(pending, results, strict=True)
        if isinstance(r, BaseException)
    ]
    counts["written"] = len(pending) - len(failed)
    if failed:
        counts["errors"] += len(failed)
        key, err = failed[0]
        log.warning(
            "board_health.write_failed",
            failed=len(failed),
            first=key,
            error=f"{type(err).__name__}: {err}",
        )
    log.info("board_health.recorded", boards=len(latest), **counts)
    return counts
