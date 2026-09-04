# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``POST /account/delete`` — the user's own door out of the product.

Runs :func:`tools.account.delete.delete_account`, which is the same wipe
``cli/reset_user.py`` performs, in the order that matters: tombstone, close the
login, *then* destroy. Read that module's docstring for what is erased, what is
deliberately left alone (``jd_cache`` and ``board_cache/`` are shared and
content-keyed), and for the one thing this cannot promise — atomicity against a
discovery cycle that was already dispatched when the tombstone landed.

**The confirmation is a typed email, not a flag.** ``{"confirm": "<your
email>"}`` has to match the address the caller signs in with, so this endpoint
cannot be reached by a mis-click, a double-submitted form, or a cross-origin
page that guesses the path — none of which can produce the string. It is not a
password and is not treated as one: it proves intent, and the bearer token
proves identity.
"""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException
from google.cloud import firestore
from pydantic import BaseModel

from api.deps import firebase_auth, verify_user
from obs.logging import get_logger
from tools.account.delete import delete_account

router = APIRouter(tags=["account"])
log = get_logger("api.account")

# An async client, like ``api.routes.companies`` and for the same reason: what
# this route runs is *literally* the function the CLI runs, rather than a second
# implementation of the wipe that agrees until it doesn't. See the comment there
# for why memoising it is safe (one uvicorn loop for the life of the process).
_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


class DeleteAccount(BaseModel):
    """``confirm`` is the caller's own email address, typed out."""

    confirm: str


def _auth_email(user_id: str) -> str | None:
    """The address this account signs in with, per Firebase Auth.

    ``None`` when the account is already gone — which is a state a *caller* can
    legitimately be in, because a Firebase ID token stays verifiable for up to
    an hour after the user it names is deleted. That is exactly the window in
    which someone retries a deletion that failed partway, so it is answered by
    falling back to the profile document rather than by refusing.
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
    they read off the screen. Two things it must *not* do: match on empty (the
    default value of an untouched input, and what a missing address would
    otherwise compare equal to), and short-circuit on the first differing byte —
    hence ``compare_digest``. Neither is load-bearing security on its own; both
    are one line.
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
    """Delete the caller's account and everything belonging to it.

    Returns the counts of what was erased, so the UI (and the log line behind
    it) can say what actually happened rather than "ok". Safe to call twice: the
    second call finds nothing left and answers 404.
    """
    snap = await _client().collection("users").document(user_id).get()
    doc = snap.to_dict() or {}
    doc_email = doc.get("email")
    # ``or None`` at the end, not just on the auth record: a profile whose
    # ``email`` is blank or whitespace has no address either, and letting ""
    # through as the expected value would leave ``_confirms`` comparing two
    # empty strings — a deletion nobody typed anything to get.
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
    counts = await delete_account(_client(), user_id, close_auth=_close_auth)
    return {"ok": True, "deleted": counts.as_dict()}
