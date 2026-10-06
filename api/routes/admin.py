# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Operator-only views. View-only: nothing here writes.

``/admin/accounts`` sits behind :func:`api.deps.verify_admin`, which answers
anyone but the admin with a plain 404. ``/admin/me`` is on the normal user gate
and answers ``{"admin": bool}``, so the web app can decide whether to show the
link without a 404 probe.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from google.cloud import firestore

from api.deps import (
    Identity,
    firebase_auth,
    is_admin,
    verify_admin,
    verify_user_identity,
)
from obs.logging import get_logger
from tools.account.roster import Roster, RosterSourceError, load_roster

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)
log = get_logger("api.admin")

_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


@router.get("/me")
async def me(ident: Annotated[Identity, Depends(verify_user_identity)]) -> dict:
    """Whether the caller is the admin. Always ``False`` under the dev bypass."""
    return {"admin": is_admin(ident)}


@router.get("/accounts")
async def accounts(
    response: Response, _admin: Annotated[Identity, Depends(verify_admin)]
) -> Roster:
    """Every account, reconciled across Auth, the allowlist, the waitlist and
    ``users/``. 503 naming the source if any read fails; never a partial list."""
    response.headers["Cache-Control"] = "no-store"
    try:
        roster = await load_roster(_client(), firebase_auth())
    except RosterSourceError as e:
        log.error("admin.accounts_failed", source=e.source, error_type=e.cause_type)
        raise HTTPException(
            status_code=503,
            detail=f"could not read {e.source}",
            headers={"Cache-Control": "no-store"},
        ) from e
    log.info(
        "admin.accounts_viewed",
        rows=roster.summary.total,
        needs_attention=roster.summary.needs_attention,
    )
    return roster
