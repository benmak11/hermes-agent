# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Operator-only routes: the accounts roster, and granting and revoking seats.

Everything but ``/admin/me`` sits behind :func:`api.deps.verify_admin`, which
answers anyone but the admin with a plain 404. ``/admin/me`` is on the normal
user gate and answers ``{"admin": bool}``, so the web app can decide whether to
show the link without a 404 probe.

Granting needs ``MAX_USERS`` on ``hermes-api``; unset or invalid, every grant is
refused (503), never treated as unlimited. A revoke takes up to
``api.deps._ALLOWLIST_CHECK_EVERY`` (5 minutes) to lock the account out, and the
admin cannot revoke their own seat. Audit lines carry uids, never emails; the
durable record is the allowlist document's ``added_by`` / ``revoked_by``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from google.cloud import firestore
from pydantic import BaseModel, Field

from api.deps import (
    _ALLOWLIST_CHECK_EVERY,
    Identity,
    firebase_auth,
    is_admin,
    verify_admin,
    verify_user_identity,
)
from api.routes.account import _confirms
from obs.logging import get_logger
from tools import allowlist
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


_NO_STORE = {"Cache-Control": "no-store"}
_REVOKE_LAG_MINUTES = int(_ALLOWLIST_CHECK_EVERY.total_seconds() // 60)


class GrantSeat(BaseModel):
    email: str
    note: str | None = Field(default=None, max_length=500)


class RevokeSeat(BaseModel):
    """``confirm`` is ``email`` typed out again."""

    email: str
    confirm: str


def _refuse(event: str, status: int, detail: str, **fields: Any) -> HTTPException:
    """Log one refusal line (``reason=`` and uids only) and build the error."""
    log.warning(event, **fields)
    return HTTPException(status_code=status, detail=detail, headers=_NO_STORE)


def _get_user_by_email(email: str):
    """The Firebase Auth record for ``email``, or ``None`` if there is none.

    Raises ``ValueError`` for a malformed address; anything else is an Auth
    outage and propagates.
    """
    fb_auth = firebase_auth()
    try:
        return fb_auth.get_user_by_email(email.strip())
    except fb_auth.UserNotFoundError:
        return None


async def _auth_record(email: str, event: str):
    try:
        return await asyncio.to_thread(_get_user_by_email, email)
    except ValueError:
        raise _refuse(
            event, 422, "not a valid email address", reason="invalid_email"
        ) from None
    except Exception as e:
        raise _refuse(
            event,
            503,
            "could not read firebase_auth",
            reason="auth_lookup_failed",
            error_type=type(e).__name__,
        ) from e


@router.post("/seats")
async def grant_seat(
    body: GrantSeat,
    response: Response,
    admin_ident: Annotated[Identity, Depends(verify_admin)],
) -> dict:
    """Give an existing login a seat, subject to ``MAX_USERS``.

    The address must belong to a Firebase Auth account (422 otherwise), and
    what is written is that record's own email, as ``cli.allowlist add`` does.
    503 when the cap is unconfigured; 409 when it is full. Already active is a
    success. The waitlist doc is left alone, as the CLI leaves it: it clears
    on the grantee's next sign-in.
    """
    response.headers["Cache-Control"] = "no-store"
    event = "admin.seat_grant_refused"
    cap = allowlist.seat_cap()
    if cap is None:
        raise _refuse(
            event,
            503,
            "seat cap not configured: set MAX_USERS on the API to grant seats",
            reason="cap_not_configured",
        )
    record = await _auth_record(body.email, event)
    if record is None:
        raise _refuse(
            event,
            422,
            "no sign-in account uses that email; they must sign in once first",
            reason="no_auth_account",
        )
    email = (record.email or "").strip()
    if not email:
        raise _refuse(
            event,
            422,
            "that sign-in account has no email address",
            reason="no_email",
            target_uid=record.uid,
        )
    granted = await allowlist.add(
        _client(),
        email,
        added_by=f"admin:{admin_ident.uid}",
        note=(body.note or "").strip() or None,
        max_users=cap,
    )
    if not granted:
        raise _refuse(
            event,
            409,
            f"seat cap reached: all {cap} seats are in use. "
            "Revoke one or raise MAX_USERS first.",
            reason="cap_reached",
            target_uid=record.uid,
            seat_cap=cap,
        )
    log.info("admin.seat_granted", target_uid=record.uid, seat_cap=cap)
    return {"granted": True, "email": email, "seat_cap": cap}


@router.post("/seats/revoke")
async def revoke_seat(
    body: RevokeSeat,
    response: Response,
    admin_ident: Annotated[Identity, Depends(verify_admin)],
) -> dict:
    """Revoke a seat. Takes effect within 5 minutes (the allowlist cache).

    400 unless ``confirm`` matches ``email``; 409 for the admin's own seat
    (revoking it would lock them out of this page) or for an address with no
    seat. An already-revoked seat answers success without re-stamping it.
    """
    response.headers["Cache-Control"] = "no-store"
    event = "admin.seat_revoke_refused"
    if not _confirms(body.confirm, body.email):
        raise _refuse(
            event,
            400,
            "type the email exactly to confirm the revoke",
            reason="confirm_mismatch",
        )
    key = allowlist._key(body.email)
    try:
        record = await asyncio.to_thread(_get_user_by_email, key)
    except ValueError:
        record = None  # a malformed address has no login; it may still have a seat
    except Exception as e:
        raise _refuse(
            event,
            503,
            "could not read firebase_auth",
            reason="auth_lookup_failed",
            error_type=type(e).__name__,
        ) from e
    target_uid = record.uid if record is not None else None
    if key == allowlist._key(admin_ident.email) or target_uid == admin_ident.uid:
        raise _refuse(
            event,
            409,
            "you cannot revoke your own seat: this page needs it",
            reason="self_revoke",
            target_uid=target_uid,
        )

    db = _client()
    snap = await db.collection(allowlist.COLLECTION).document(key).get()
    seat = snap.to_dict() if snap.exists else None
    if not isinstance(seat, dict):
        raise _refuse(
            event,
            409,
            "that email has no seat to revoke",
            reason="no_seat",
            target_uid=target_uid,
        )
    lag = (
        f"It takes effect within {_REVOKE_LAG_MINUTES} minutes: the API caches "
        "each allowlist check for that long."
    )
    if seat.get("revoked"):
        log.info("admin.seat_revoke_noop", target_uid=target_uid)
        return {"revoked": True, "already_revoked": True, "message": lag}

    await allowlist.revoke(db, key, revoked_by=f"admin:{admin_ident.uid}")
    log.info("admin.seat_revoked", target_uid=target_uid)
    return {
        "revoked": True,
        "already_revoked": False,
        "message": f"Seat revoked. {lag}",
    }
