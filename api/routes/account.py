# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``POST /account/signup`` and ``POST /account/delete`` — the doors in and out.

Signup is the one route that answers a stranger: it runs on
:func:`api.deps.verify_identity` (token verified, allowlist not consulted),
records where the visitor came from, and answers ``{"allowed": bool}``. An
email/password account with an unverified address is answered
``{"allowed": false, "reason": "verify_email"}`` and nothing is written. An
allowlisted account gets ``users/{uid}`` stamped once with ``signup_source``
and ``signed_up_at``; anyone else gets a ``waitlist/{email}`` doc and nothing
under ``users/``. Every other route still 403s a stranger through
:func:`api.deps.verify_user`.

**Delete irreversibly destroys the user's data.** It runs
:func:`tools.account.delete.delete_account` — the same wipe ``cli/reset_user``
performs, in the order that matters: tombstone, close the login, then destroy.
That module's docstring says what is erased, what is left alone, and that it
cannot promise atomicity against an already-dispatched discovery cycle.

The confirmation is a typed email, not a flag: ``{"confirm": "<your email>"}``
must match the address the caller signs in with, so a mis-click, a
double-submitted form or a cross-origin guess cannot reach it. It proves
intent; the bearer token proves identity.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore
from pydantic import BaseModel

from api.deps import (
    Identity,
    firebase_auth,
    verification_required,
    verify_identity,
    verify_user,
)
from obs.logging import get_logger
from tools import allowlist
from tools.account.delete import delete_account, is_deleted

router = APIRouter(tags=["account"])
log = get_logger("api.account")

# An async client, like ``api.routes.companies``, so this route runs literally
# the function the CLI runs rather than a second implementation of the wipe.
# Memoising is safe: one uvicorn loop for the life of the process.
_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


#: The four marketing CTAs (web/src/lib/nav.ts SIGNUP_SOURCES). Anything else
#: is stored as null — the client is not trusted to name its own source.
SIGNUP_SOURCES = frozenset({"hero", "closing", "nav", "footer"})


class Signup(BaseModel):
    """``source`` is which marketing CTA the visitor came in through, if any."""

    source: str | None = None


@router.post("/account/signup")
async def signup(
    body: Signup,
    # ``Annotated`` rather than ``= Depends(...)``: ruff's B008 only exempts a
    # ``Depends`` default when the annotation is a builtin like ``str``.
    ident: Annotated[Identity, Depends(verify_identity)],
) -> dict:
    """Admit or waitlist the account behind this token. See the module docstring.

    Timestamps are ISO-8601 strings, not ``SERVER_TIMESTAMP``. The allowed
    branch is read-then-merge, so a repeat sign-in costs one read and no write,
    ``signup_source`` records the first entry point, and a doc tombstoned by
    ``POST /account/delete`` is never recreated by a token that outlives the
    account (up to an hour).

    The allowlist read here is deliberately not the 5-minute TTL cache
    ``api.deps`` keeps for the other routes: a fresh grant must be visible on
    the very next sign-in.
    """
    if verification_required() and ident.needs_verification:
        # Ahead of everything, and writes nothing: an unverified address may
        # not be the caller's, so it must not hold a seat, a waitlist place or
        # a ``signup_source``. The client verifies and calls again.
        log.info("account.signup.verify_email", user_id=ident.uid)
        return {"allowed": False, "reason": "verify_email"}

    source = body.source if body.source in SIGNUP_SOURCES else None
    db = _client()

    if allowlist.enforced():
        if not ident.email:
            log.warning("account.signup.no_email", user_id=ident.uid)
            raise HTTPException(
                status_code=403,
                detail="this account has no email claim to check against the allowlist",
            )
        allowed = await allowlist.is_allowed(db, ident.email)
    else:
        allowed = True  # mirrors _check_allowlist: unenforced == everyone in

    if not allowed:
        created = await allowlist.record_waitlist(
            db,
            uid=ident.uid,
            email=ident.email,
            source=source,
            email_verified=ident.email_verified,
        )
        log.info(
            "account.signup.waitlisted",
            user_id=ident.uid,
            source=source,
            created=created,
        )
        return {"allowed": False}

    user_ref = db.collection("users").document(ident.uid)
    existing = (await user_ref.get()).to_dict() or {}
    if not existing.get("signed_up_at") and not is_deleted(existing):
        await user_ref.set(
            {"signup_source": source, "signed_up_at": datetime.now(UTC).isoformat()},
            merge=True,
        )
        log.info("account.signup.admitted", user_id=ident.uid, source=source)
    # Outside the first-time branch on purpose: a revoke -> waitlisted sign-in
    # -> re-grant sequence would otherwise leave a stale waitlist doc for
    # someone who is in. One cheap delete per allowed sign-in.
    if ident.email:
        await allowlist.remove_from_waitlist(db, ident.email)
    return {"allowed": True}


class DeleteAccount(BaseModel):
    """``confirm`` is the caller's own email address, typed out."""

    confirm: str


def _auth_email(user_id: str) -> str | None:
    """The address this account signs in with, per Firebase Auth.

    ``None`` when the account is already gone, which a caller can legitimately
    be: a Firebase ID token stays verifiable for up to an hour after the user
    it names is deleted, and that is the window in which someone retries a
    deletion that failed partway. The caller falls back to the profile document.
    """
    try:
        record = firebase_auth().get_user(user_id)
    except Exception as e:
        log.info("account.auth_lookup_failed", user_id=user_id, error=str(e))
        return None
    return (record.email or "").strip() or None


def _close_auth(user_id: str) -> None:
    """Delete the Firebase Auth user; treat "already gone" as done.

    A half-finished deletion must be finishable. If this raised on the second
    pass, the wipe behind it would never run and the account would sit
    tombstoned with its data intact.
    """
    fb_auth = firebase_auth()
    try:
        fb_auth.delete_user(user_id)
    except fb_auth.UserNotFoundError:
        log.info("account.auth_already_gone", user_id=user_id)


def _confirms(typed: str, expected: str) -> bool:
    """Does the typed confirmation match the caller's address?

    Case- and whitespace-insensitive, because the user is retyping something
    off the screen. It must not match on empty — an untouched input, or a
    missing address, would otherwise compare equal — and uses
    ``compare_digest`` rather than short-circuiting on the first differing byte.
    """
    a = typed.strip().casefold()
    b = expected.strip().casefold()
    if not a or not b:
        return False
    return secrets.compare_digest(a.encode(), b.encode())


@router.post("/account/delete")
async def delete_my_account(
    body: DeleteAccount, user_id: str = Depends(verify_user)
) -> dict:
    """Delete the caller's account and everything belonging to it. Irreversible.

    Returns the counts of what was erased, so the UI can say what actually
    happened rather than "ok". Safe to call twice: the second call finds
    nothing left and answers 404.
    """
    snap = await _client().collection("users").document(user_id).get()
    doc = snap.to_dict() or {}
    doc_email = doc.get("email")
    # ``or None`` at the end, not just on the auth record: a profile whose
    # ``email`` is blank has no address either, and letting "" through would
    # leave ``_confirms`` comparing two empty strings — a deletion nobody
    # typed anything to get.
    fallback = doc_email.strip() if isinstance(doc_email, str) else ""
    expected = await asyncio.to_thread(_auth_email, user_id) or fallback or None

    if expected is None:
        if not snap.exists:
            # No login and no document: a previous pass got all the way through.
            log.info("account.delete.nothing_left", user_id=user_id)
            raise HTTPException(status_code=404, detail="no account to delete")
        # An account with no address anywhere (a provider that carries none)
        # has nothing to type, so this door does not open for it. Refusing is
        # the honest answer; the operator's CLI is the other door.
        log.warning("account.delete.no_email", user_id=user_id)
        raise HTTPException(
            status_code=400,
            detail="this account has no email address to confirm against",
        )

    if not _confirms(body.confirm, expected):
        log.warning("account.delete.confirmation_mismatch", user_id=user_id)
        raise HTTPException(
            status_code=400,
            detail="type your account email exactly to confirm deletion",
        )

    log.info("account.delete.requested", user_id=user_id)
    # ``expected`` is the Auth-preferred email resolved above, captured before
    # ``_close_auth`` runs — the only window in which Firebase Admin can still
    # answer for it. ``delete_account`` uses it to free this user's allowlist
    # seat, and is a no-op where that flag is off.
    counts = await delete_account(
        _client(), user_id, close_auth=_close_auth, email=expected
    )
    return {"ok": True, "deleted": counts.as_dict()}
