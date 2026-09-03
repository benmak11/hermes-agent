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

**One document per (user, platform, slug)** — never a list or a map that is
read, mutated and written back. ``tools.companies`` does exactly that to the
YAML (``raw.setdefault(platform, []).extend(...)`` then save), which is this
project's recurring bug shape: two writers each read the whole list, each
append one entry, and the second write silently loses the first. A document
whose id *is* the key has no such window.

**Read ``state``, not ``action``.** ``action`` is what the user clicked; it is
audit and UI grouping, and its vocabulary belongs to the UI. ``state`` is what
the pipeline acts on. Filtering the fetch set on ``action`` would couple which
boards get crawled to whatever the buttons happen to be called this month.

This module is the read half. The write half populates these documents; until
it lands, every overlay is empty and the composed fetch set is byte-identical
to the one this pipeline has always produced.
"""

from __future__ import annotations

from obs.logging import get_logger
from tools.companies import PLATFORMS, Platform

log = get_logger("tools.company_prefs")

#: Subcollection under ``users/{uid}`` holding one document per exclusion.
COLLECTION = "company_prefs"


def exclusion_key(platform: Platform, slug: str) -> str:
    """Document id for one ``(platform, slug)`` exclusion.

    Firestore document ids may not contain ``/`` — a slash is a path separator,
    so an id carrying one does not fail loudly, it addresses a *different*
    (sub)collection. For the three multi-tenant ATS platforms neither half can
    contain one: ``platform`` is a five-value ``Literal`` and a slug is a single
    URL path segment (``boards.greenhouse.io/<slug>``).

    The single-company career sites are the exception, and the reason this is a
    raised error rather than a comment: ``google_jobs`` and ``meta_jobs`` reuse
    the slug slot as a free-text *search query* (see ``tools.companies``), so
    nothing about the type stops a future entry from containing a slash.
    """
    if "/" in platform or "/" in slug:
        raise ValueError(
            f"company_prefs key may not contain '/': {platform!r}, {slug!r}"
        )
    return f"{platform}:{slug}"


async def load_exclusions(db, user_id: str) -> frozenset[tuple[Platform, str]]:
    """The ``(platform, slug)`` pairs this user has excluded, as one snapshot.

    One ``stream()`` over the whole subcollection, never a lookup per board:
    198 point reads cost 198 reads, and — the part that actually matters — a
    write landing halfway through would apply to some boards and not others, so
    a single cycle would run against two different views of the world.

    Returned as a ``frozenset`` because that is what it is: an immutable
    snapshot, taken once and held for a whole cycle. It is allowed to be
    slightly stale — a change made mid-cycle is picked up by the next one. This
    is deliberately *not* a transaction and carries no precondition; there is
    nothing here to lose a race over.

    A document that does not match the shape above is skipped with a warning
    rather than raising: a single bad overlay row must not take down a crawl.
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
