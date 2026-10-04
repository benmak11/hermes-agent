# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-user company exclusions: a Firestore overlay on the global company pool.

The pool itself does not move. ``data/companies/*.yaml`` (read by
:mod:`tools.companies`) stays what it is: a git-shipped, reviewed, global list
of boards worth fetching. What an individual user has excluded is per-user
state, so it lives in Firestore as a thin overlay *on top of* that pool:

    users/{uid}/company_prefs/{platform}:{slug}
      {
        "platform":   "greenhouse",
        "slug":       "stripe",
        "state":      "excluded",
        "action":     "block" | "dismiss" | "pause",
        "reason":     str | None,
        "updated_at": "2026-09-03T12:00:00+00:00",
      }

One document per ``(user, platform, slug)``, never a list or map that is
read, mutated and written back: under two concurrent writers the second write
silently loses the first, and a document whose id *is* the key has no such
window.

Read ``state``, not ``action``. ``action`` is what the user clicked, for audit
and UI grouping; ``state`` is what the pipeline acts on. Filtering the fetch
set on ``action`` would couple which boards get crawled to whatever the
buttons are called this month.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from obs.logging import get_logger
from tools.companies import PLATFORMS, Platform

#: What the user clicked. Audit and UI grouping only — the pipeline reads
#: ``state``. All three mean "stop fetching this board for me".
ExclusionAction = Literal["block", "dismiss", "pause"]

log = get_logger("tools.company_prefs")

#: Subcollection under ``users/{uid}`` holding one document per exclusion.
COLLECTION = "company_prefs"


def exclusion_key(platform: Platform, slug: str) -> str:
    """Document id for one ``(platform, slug)`` exclusion.

    Raises on a ``/`` in either half: a slash is a Firestore path separator,
    so an id carrying one silently addresses a different subcollection rather
    than failing. The ATS slugs cannot contain one, but ``google_jobs`` and
    ``meta_jobs`` reuse the slug slot as a free-text search query.
    """
    if "/" in platform or "/" in slug:
        raise ValueError(
            f"company_prefs key may not contain '/': {platform!r}, {slug!r}"
        )
    return f"{platform}:{slug}"


async def load_exclusions(db, user_id: str) -> frozenset[tuple[Platform, str]]:
    """The ``(platform, slug)`` pairs this user has excluded, as one snapshot.

    One ``stream()`` over the whole subcollection rather than a lookup per
    board: a write landing halfway through a per-board loop would apply to
    some boards and not others, running one cycle against two views of the
    world.

    An immutable snapshot, held for a whole cycle and allowed to be slightly
    stale — a mid-cycle change is picked up by the next one. A document that
    does not match the shape above is skipped with a warning, so one bad
    overlay row cannot take down a crawl.
    """
    exclusions: set[tuple[Platform, str]] = set()
    col = db.collection("users").document(user_id).collection(COLLECTION)
    async for snap in col.stream():
        doc = snap.to_dict() or {}
        if doc.get("state") != "excluded":
            continue
        platform, slug = doc.get("platform"), doc.get("slug")
        if platform not in PLATFORMS or not isinstance(slug, str) or not slug:
            log.warning(
                "company_prefs.malformed",
                user_id=user_id,
                doc_id=snap.id,
                platform=platform,
                slug=slug,
            )
            continue
        exclusions.add((platform, slug))
    return frozenset(exclusions)


async def set_exclusion(
    db,
    user_id: str,
    platform: Platform,
    slug: str,
    *,
    action: ExclusionAction,
    reason: str | None = None,
) -> str:
    """Record that ``user_id`` no longer wants ``(platform, slug)`` fetched.

    One blind ``set()`` of the document whose id is :func:`exclusion_key`, and
    deliberately neither a compare-and-swap nor a ``merge``: every field is
    derived from this one click, so two identical clicks converge and there is
    no prior state to preserve. It must never become a read-modify-write of a
    list, where a second concurrent write silently drops the first.

    Raises ``ValueError`` from :func:`exclusion_key`, before any I/O, if the
    pair cannot be a document id; callers on the request path must turn that
    into a 4xx. Returns the document id, for logging.
    """
    key = exclusion_key(platform, slug)
    doc = {
        "platform": platform,
        "slug": slug,
        "state": "excluded",
        "action": action,
        "reason": reason,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    await (
        db.collection("users")
        .document(user_id)
        .collection(COLLECTION)
        .document(key)
        .set(doc)
    )
    log.info(
        "company_prefs.excluded",
        user_id=user_id,
        doc_id=key,
        platform=platform,
        slug=slug,
        action=action,
    )
    return key
