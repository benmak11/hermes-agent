# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Company management endpoints: the global pool, and this user's exclusions.

Two things are visible here and must not be conflated. The **pool** —
``data/companies/{known,unvetted,blocklist}.yaml`` — is global, git-shipped and
identical for every user; nothing in a running container writes it. The
**overlay** — ``users/{uid}/company_prefs`` — is one document per company this
one user has told us to stop fetching (see :mod:`tools.company_prefs`), and is
what ``POST /companies/action`` writes. Editing the YAML from a route does not
work: under ``QUEUE_MODE`` discovery runs on a different service, so the edit
lands on a filesystem the crawl never reads and is lost on the next deploy.

``GET /companies`` returns the pool annotated with the overlay rather than
filtered by it, so an excluded company stays in its group carrying
``excluded: true`` instead of silently vanishing.
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

# An async client, unlike the other route modules' sync-plus-to_thread pattern,
# so the read below is literally tools.company_prefs.load_exclusions — the same
# function the crawl uses, down to its tolerance of a malformed overlay row.
# What this endpoint shows is then what discovery will actually skip.
#
# Memoising is safe because nothing in api/ calls asyncio.run: every route runs
# on the one uvicorn loop for the life of the process, so this client is never
# handed to a second loop.
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
    ``excluded`` flag; ``blocklist`` is the global blocklist; ``excluded`` is
    this user's overlay, listed separately because an exclusion can outlive the
    pool entry it was made against.
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
    """``promote`` is deliberately absent: it never changed the fetch set
    (``all_active_companies`` fetches known *and* unvetted), and there is no
    global write path left for it. Promotion is a reviewed git edit to
    ``known.yaml``.
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
        # google_jobs/meta_jobs reuse the slug slot as a free-text search
        # query, so a '/' in it is reachable user input. A slash in a document
        # id addresses a different subcollection rather than failing, so it is
        # refused as a 422.
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
