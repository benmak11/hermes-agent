# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Company management endpoints: the global pool, and this user's exclusions.

Two different things are visible here and they must not be conflated:

- **The pool** — ``data/companies/{known,unvetted,blocklist}.yaml``. Global,
  git-shipped, reviewed, identical for every user. It grows through
  ``cli.discover_companies`` and shrinks through a pull request; nothing in a
  running container writes it.
- **The overlay** — ``users/{uid}/company_prefs``. One document per company this
  *one* user has told us to stop fetching. See :mod:`tools.company_prefs`.

``POST /companies/action`` used to call ``apply_company_action``, which edited
the YAML on whichever container happened to serve the request. Under
``QUEUE_MODE=1`` discovery runs on ``hermes-worker`` and the API runs on
``hermes-api``, so that edit landed on a filesystem the crawl never reads —
and was lost on the next deploy regardless. It now writes the overlay.

``GET /companies`` returns the pool *annotated* with the overlay rather than
filtered by it: an excluded company stays in its ``known``/``unvetted`` group
carrying ``excluded: true``, so the UI can show the user what they excluded
instead of the row silently vanishing. ``blocklist`` stays exactly what it was
— the global list, which applies to everyone — and the user's own exclusions
are a separate ``excluded`` array.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore
from pydantic import BaseModel

from api.deps import verify_user
from obs.logging import get_logger
from tools.companies import (
    CompanyEntry,
    Platform,
    load_blocklist_detailed,
    load_known,
    load_unvetted,
)
from tools.company_prefs import ExclusionAction, load_exclusions, set_exclusion

router = APIRouter(tags=["companies"])
log = get_logger("api.companies")

# An async client, unlike the other route modules' sync-client-plus-to_thread
# pattern, so that the read below is *literally*
# tools.company_prefs.load_exclusions — the same function the crawl uses. What
# this endpoint shows a user is then what discovery will actually skip,
# including its tolerance of a malformed overlay row, rather than a second
# implementation that agrees until it doesn't. hermes-worker already runs both
# client flavours in one process (the discovery pipeline is async, its routes
# are sync), so this is not new ground.
#
# Memoising it is safe for the same reason the sync ones are: nothing in api/
# calls asyncio.run, so every route runs on the one uvicorn loop for the life of
# the process, and this client is never handed to a second loop. (That is the
# failure tools.genai_client memoises per-loop to avoid; the condition differs.)
_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


def _annotate(
    groups: dict[Platform, list[CompanyEntry]],
    exclusions: frozenset[tuple[Platform, str]],
) -> dict[str, list[dict]]:
    """The pool group, each entry flagged with whether *this* user excluded it."""
    return {
        platform: [
            {
                **entry.model_dump(mode="json"),
                "excluded": (platform, entry.slug) in exclusions,
            }
            for entry in entries
        ]
        for platform, entries in groups.items()
    }


@router.get("/companies")
async def list_companies(user_id: str = Depends(verify_user)) -> dict:
    """The global company pool as *this* user sees it.

    ``known``/``unvetted`` are the global pool with an added per-entry
    ``excluded`` flag; ``blocklist`` is the global blocklist (everyone's);
    ``excluded`` is this user's overlay, listed separately because an exclusion
    can outlive the pool entry it was made against — a company dropped from
    ``unvetted.yaml`` after the fact would otherwise be invisible.
    """
    exclusions = await load_exclusions(_client(), user_id)
    return {
        "known": _annotate(load_known(), exclusions),
        "unvetted": _annotate(load_unvetted(), exclusions),
        "blocklist": load_blocklist_detailed(),
        "excluded": [
            {"platform": platform, "slug": slug}
            for platform, slug in sorted(exclusions)
        ],
    }


class CompanyAction(BaseModel):
    """``promote`` is deliberately absent.

    It was a global operator action — move a slug from ``unvetted.yaml`` to
    ``known.yaml`` — and it never changed the fetch set, because
    ``all_active_companies`` fetches known *and* unvetted. With the YAML
    mutators gone there is no global write path left for it to use. Promotion
    is a git edit to ``known.yaml``, reviewed like the rest of the pool.
    """

    platform: Platform
    slug: str
    action: ExclusionAction
    reason: str | None = None


@router.post("/companies/action")
async def company_action(
    body: CompanyAction, user_id: str = Depends(verify_user)
) -> dict:
    """Exclude a company from *this user's* fetch set.

    All three actions produce the same ``state`` — the pipeline only ever asks
    "is this excluded?" — and differ only in the ``action`` recorded alongside.

    This does not touch already-scored jobs. An exclusion narrows what gets
    crawled next cycle; hiding jobs the user has already paid to have scored is
    a different (and unasked-for) feature.
    """
    try:
        key = await set_exclusion(
            _client(),
            user_id,
            body.platform,
            body.slug,
            action=body.action,
            reason=body.reason,
        )
    except ValueError as e:
        # google_jobs/meta_jobs reuse the slug slot as a free-text search query,
        # so a '/' in it is reachable user input, not an impossible state. A
        # slash in a document id addresses a different subcollection rather than
        # failing, so this is refused outright — as a 422, the same code this
        # route already returns for a body that fails validation.
        log.warning(
            "company.action.rejected",
            platform=body.platform,
            slug=body.slug,
            error=str(e),
        )
        raise HTTPException(status_code=422, detail=str(e)) from e

    log.info(
        "company.action",
        platform=body.platform,
        slug=body.slug,
        action=body.action,
        reason=body.reason,
        doc_id=key,
    )
    return {"ok": True}
