# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared FastAPI dependencies for the web API (auth)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import Header, HTTPException, Query
from google.cloud import firestore

from obs.logging import bind_request_context, get_logger
from tools import allowlist

log = get_logger("api.auth")

_firebase_ready = False

# An async client, like ``api.routes.account`` and for the same reason: this
# runs on the one uvicorn loop for the life of the process, so memoising it is
# safe. Only built at all once ``ALLOWLIST_ENFORCED`` is on — see
# :func:`_check_allowlist` — so this stays unused, and unbuilt, on every
# deployment until Phase 4 D2 flips the flag.
_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


# In-process cache so a hot endpoint doesn't pay a Firestore read on every
# request. Same shape as ``api.routes.discovery._last_tick_check`` and the
# same justification: this is a cache, not a lock — a revocation can bite up
# to ``_ALLOWLIST_CHECK_EVERY`` late, which is acceptable for a seat gate, and
# nothing here needs to be correct across more than one process at a time.
_ALLOWLIST_CHECK_EVERY = timedelta(minutes=5)
_allowlist_cache: dict[str, tuple[datetime, bool]] = {}


def dev_mode() -> bool:
    """Is this process a developer's machine rather than a deployed service?

    ``AUTH_DEV_MODE=1`` is the codebase's existing answer to that question, and
    it is a reliable one in *one* direction: Cloud Run's environment comes from
    Terraform, which does not set this variable, so **a deployed service never
    has it on**. A local process, on the other hand, has it on precisely because
    that is how a developer talks to the API without minting a Firebase token.

    Read by two things besides the auth bypass below, both of which want that
    exact question answered and neither of which should invent its own signal:

    - ``api.main`` — whether to publish ``/docs`` and ``/openapi.json``.
    - ``api.routes.discovery`` — whether to refuse to drive the real, billed
      discovery pipeline (see the guard there; a local harness once ran a
      198-board crawl against production this way).

    Deliberately not "is this the production project?". There is one project,
    and it is production, so a local process is *always* pointed at it — the
    dangerous half of the combination is the only half worth testing for.
    """
    return os.getenv("AUTH_DEV_MODE") == "1"


def _ensure_firebase() -> None:
    """Lazily initialize the Firebase Admin SDK (uses ADC)."""
    global _firebase_ready
    if _firebase_ready:
        return
    import firebase_admin

    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    _firebase_ready = True


def firebase_auth():
    """The ``firebase_admin.auth`` module, with the Admin SDK initialised.

    One initialisation for the process, shared with :func:`_verify_token` —
    ``firebase_admin.initialize_app()`` raises if it is called twice, so a
    second caller must not run its own. Exported because deleting an account
    (``api.routes.account``) has to reach the *same* Admin app the token
    verification uses, and because a function is a seam a test can replace,
    where ``from firebase_admin import auth`` inside a route body is not.
    """
    _ensure_firebase()
    from firebase_admin import auth as fb_auth

    return fb_auth


async def _check_allowlist(uid: str, email: str | None) -> None:
    """Refuse with a 403 iff enforcement is on and ``uid`` isn't allowed in.

    A no-op read straight through while ``ALLOWLIST_ENFORCED`` is unset — the
    whole point of D1 shipping before D2 flips it. Called only from the branch
    of :func:`_verify_token` that has a real decoded token; the dev bypass
    returns before this is ever reached, by construction (see that function).

    **Fails closed on an absent email claim** rather than falling through to a
    500: a token with no ``email`` is a shape a caller could produce, and
    enforcement being on means "prove you're allowed", not "crash if you
    can't prove it".
    """
    if not allowlist.enforced():
        return
    if not email:
        log.warning("auth.allowlist_no_email", user_id=uid)
        raise HTTPException(
            status_code=403,
            detail="this account has no email claim to check against the allowlist",
        )

    now = datetime.now(UTC)
    cached = _allowlist_cache.get(uid)
    if cached is not None and now - cached[0] < _ALLOWLIST_CHECK_EVERY:
        allowed = cached[1]
    else:
        allowed = await allowlist.is_allowed(_client(), email)
        _allowlist_cache[uid] = (now, allowed)

    if not allowed:
        log.warning("auth.allowlist_denied", user_id=uid)
        raise HTTPException(
            status_code=403, detail="this account is not on the allowlist"
        )


async def _verify_token(token: str | None) -> str:
    """Verify a Firebase ID token (or honor the dev bypass) and return the uid.

    Binds the resolved ``user_id`` into the log context so every subsequent line
    for this request (route, background task, tools) carries it.

    The allowlist check runs **after** the dev bypass, never before it: the
    bypass returns before this function does anything else, so a local
    process with ``AUTH_DEV_USER`` set reaches no Firestore at all, allowlist
    included — local dev and the ``me`` demo account must keep working exactly
    as before this PR.
    """
    if dev_mode() and os.getenv("AUTH_DEV_USER"):
        uid = os.environ["AUTH_DEV_USER"]
        bind_request_context(user_id=uid, auth="dev")
        return uid

    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    _ensure_firebase()
    from firebase_admin import auth as fb_auth

    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception as e:
        log.warning("auth.verify_failed", error=str(e))
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e

    uid = decoded["uid"]
    email = decoded.get("email")
    bind_request_context(user_id=uid)
    await _check_allowlist(uid, email)
    return uid


async def verify_user(authorization: str | None = Header(default=None)) -> str:
    """Return the verified user_id from a Firebase ID token in the Authorization header.

    Local dev bypass: when AUTH_DEV_MODE=1 and AUTH_DEV_USER is set, skips token
    verification and returns AUTH_DEV_USER. NEVER enable AUTH_DEV_MODE in
    production — it is gated on an explicit env var precisely so it can't be on
    by accident (Cloud Run env is set via Terraform, which does not set it).
    """
    token = (
        authorization.removeprefix("Bearer ")
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    return await _verify_token(token)


async def verify_user_query(token: str | None = Query(default=None)) -> str:
    """Like verify_user but reads the token from a ?token= query param.

    For SSE (EventSource) endpoints, where the browser cannot set an
    Authorization header.
    """
    return await _verify_token(token)
