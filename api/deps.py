# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared FastAPI dependencies for the web API: auth, and the spend seam."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, HTTPException, Query, Request
from google.cloud import firestore
from pydantic import BaseModel

from obs.logging import bind_request_context, get_logger
from tools import allowlist, spend

log = get_logger("api.auth")

_firebase_ready = False

# An async client. Memoising is safe: one uvicorn loop for the life of the
# process. Only built once ``ALLOWLIST_ENFORCED`` is on — see
# :func:`_check_allowlist` — so it stays unbuilt while that flag is off.
_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


# In-process cache so a hot endpoint doesn't pay a Firestore read on every
# request. A cache, not a lock: a revocation can bite up to
# ``_ALLOWLIST_CHECK_EVERY`` late, which is acceptable for a seat gate.
_ALLOWLIST_CHECK_EVERY = timedelta(minutes=5)
_allowlist_cache: dict[str, tuple[datetime, bool]] = {}


def dev_mode() -> bool:
    """Is this process a developer's machine rather than a deployed service?

    ``AUTH_DEV_MODE=1`` is reliable in one direction: Cloud Run's environment
    comes from Terraform, which does not set this variable, so a deployed
    service never has it on, while a local process has it on because that is
    how a developer talks to the API without minting a Firebase token.

    Besides the auth bypass below, it is read by ``api.main`` (whether to
    publish ``/docs``) and by ``api.routes.discovery`` (whether to refuse to
    drive the real, billed discovery pipeline).

    Deliberately not "is this the production project?": there is one project
    and it is production, so a local process is always pointed at it.
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

    One initialisation for the process: ``firebase_admin.initialize_app()``
    raises if called twice. Exported because deleting an account has to reach
    the same Admin app token verification uses, and because a function is a
    seam a test can replace where an inline import is not.
    """
    _ensure_firebase()
    from firebase_admin import auth as fb_auth

    return fb_auth


async def _check_allowlist(uid: str, email: str | None) -> None:
    """Refuse with a 403 iff enforcement is on and ``uid`` isn't allowed in.

    A no-op while ``ALLOWLIST_ENFORCED`` is unset. Called only from the branch
    of :func:`_verify_token` that has a real decoded token; the dev bypass
    returns before this is reached.

    Fails closed on an absent email claim rather than 500ing: a token with no
    ``email`` is a shape a caller can produce, and enforcement means "prove
    you're allowed", not "crash if you can't".
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


@dataclass(frozen=True)
class Identity:
    """What a verified token says, before the allowlist has been consulted.

    ``provider`` is the token's ``firebase.sign_in_provider`` (``"password"``,
    ``"google.com"``, ...); ``None`` on the dev bypass.
    """

    uid: str
    email: str | None
    email_verified: bool
    provider: str | None = None

    @property
    def needs_verification(self) -> bool:
        """An email/password account whose address is not verified yet.

        Only password accounts: an OAuth provider vouches for the address, and
        Google tokens do not always carry ``email_verified``.
        """
        return self.provider == "password" and not self.email_verified


def verification_required() -> bool:
    """Is ``REQUIRE_VERIFIED_EMAIL`` on? Read per call, default on.

    ``0`` / ``false`` / ``no`` / ``off`` is the emergency kill switch. It is
    independent of ``ALLOWLIST_ENFORCED`` on purpose: the allowlist lifts at
    launch, and this check has to outlive it.
    """
    raw = os.getenv("REQUIRE_VERIFIED_EMAIL", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _check_verified(ident: Identity) -> None:
    """403 ``{"reason": "email_unverified"}`` for an unverified password account.

    A dict detail, unlike the allowlist's string, so the web app can tell the
    two refusals apart and show the verify screen rather than the invite one.
    """
    if verification_required() and ident.needs_verification:
        log.warning("auth.email_unverified", user_id=ident.uid)
        raise HTTPException(status_code=403, detail={"reason": "email_unverified"})


def _bearer(authorization: str | None) -> str | None:
    """The token inside an ``Authorization: Bearer …`` header, else ``None``."""
    return (
        authorization.removeprefix("Bearer ")
        if authorization and authorization.startswith("Bearer ")
        else None
    )


def _dev_bypass_uid() -> str | None:
    """The uid a local process is impersonating, or ``None`` on a real
    deployment (or locally with no ``AUTH_DEV_USER``). The one place the bypass
    condition is spelled out, so :func:`_verify_identity` and
    :func:`_verify_token` cannot disagree about it."""
    if dev_mode() and os.getenv("AUTH_DEV_USER"):
        return os.environ["AUTH_DEV_USER"]
    return None


async def _verify_identity(token: str | None) -> Identity:
    """Verify a Firebase ID token (or honor the dev bypass) and say who it is.

    Binds the resolved ``user_id`` into the log context so every subsequent line
    for this request (route, background task, tools) carries it.

    The dev bypass returns first, so a local process with ``AUTH_DEV_USER``
    set reaches no Firestore at all. It yields an :class:`Identity` with no
    email, so callers that need one must treat ``email=None`` as a real shape.
    """
    dev_uid = _dev_bypass_uid()
    if dev_uid:
        bind_request_context(user_id=dev_uid, auth="dev")
        return Identity(uid=dev_uid, email=None, email_verified=False)

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
    bind_request_context(user_id=uid)
    firebase_claim = decoded.get("firebase")
    provider = (
        firebase_claim.get("sign_in_provider")
        if isinstance(firebase_claim, dict)
        else None
    )
    return Identity(
        uid=uid,
        email=decoded.get("email"),
        email_verified=bool(decoded.get("email_verified")),
        provider=provider,
    )


async def _verified_user(token: str | None) -> Identity:
    """Verify the token, the email-verification rule and the allowlist,
    returning the caller's identity.

    Both checks run after the dev bypass and are skipped entirely on it, so
    local dev and the ``me`` demo account keep working even with
    ``ALLOWLIST_ENFORCED=1`` — the bypass identity carries no email, which
    :func:`_check_allowlist` would refuse.

    Verification runs first and whether or not the allowlist is enforced, so an
    unverified account sees the verify screen and costs no allowlist read.
    """
    ident = await _verify_identity(token)
    if _dev_bypass_uid() is None:
        _check_verified(ident)
        await _check_allowlist(ident.uid, ident.email)
    return ident


async def _verify_token(token: str | None) -> str:
    """:func:`_verified_user`, returning just the uid."""
    return (await _verified_user(token)).uid


async def verify_identity(
    authorization: str | None = Header(default=None),
) -> Identity:
    """Token verified, allowlist and email verification not consulted.

    Only for routes that must answer a stranger — today just
    ``POST /account/signup``, where a non-allowlisted account is told it is
    waitlisted, and an unverified one to verify, rather than 403'd. Every
    other route uses :func:`verify_user`.
    """
    return await _verify_identity(_bearer(authorization))


async def verify_user(authorization: str | None = Header(default=None)) -> str:
    """The verified user_id from the Firebase ID token in the Authorization
    header.

    Local dev bypass: with AUTH_DEV_MODE=1 and AUTH_DEV_USER set, token
    verification is skipped and AUTH_DEV_USER is returned. Never enable
    AUTH_DEV_MODE in production; Terraform does not set it, which is what keeps
    it off by construction.
    """
    return await _verify_token(_bearer(authorization))


async def verify_user_identity(
    authorization: str | None = Header(default=None),
) -> Identity:
    """:func:`verify_user`, returning the whole :class:`Identity` rather than the uid."""
    return await _verified_user(_bearer(authorization))


async def verify_user_query(token: str | None = Query(default=None)) -> str:
    """Like verify_user but reads the token from a ?token= query param.

    For SSE (EventSource) endpoints, where the browser cannot set an
    Authorization header.
    """
    return await _verify_token(token)


# ---------------------------------------------------------------------------
# The admin gate
# ---------------------------------------------------------------------------


def admin_uids() -> frozenset[str]:
    """Firebase uids in ``ADMIN_UIDS`` (comma-separated), read on every call.

    Unset or blank means nobody is admin.
    """
    return frozenset(
        uid.strip() for uid in os.getenv("ADMIN_UIDS", "").split(",") if uid.strip()
    )


def _admin_refusal(ident: Identity) -> str | None:
    """Why ``ident`` is not the admin, or ``None`` if it is. Matches on uid,
    never email, because an email can change hands."""
    if ident.uid not in admin_uids():
        return "not_admin"
    if not ident.email_verified:
        return "email_unverified"
    return None


def is_admin(ident: Identity) -> bool:
    """Is an already-verified, already-allowlisted caller the admin?

    Always ``False`` in dev mode, so the local bypass can never act as admin.
    """
    return not dev_mode() and _admin_refusal(ident) is None


def _admin_denied(reason: str) -> None:
    log.warning("admin.denied", reason=reason)


async def _admin_identity(token: str | None) -> Identity | None:
    """The caller's identity iff they are the admin, else ``None``.

    Refuses in dev mode before looking at the token, so even ``AUTH_DEV_USER``
    set to an admin uid is not admin. Every refusal logs ``admin.denied``.
    """
    if dev_mode():
        _admin_denied("dev_mode")
        return None
    try:
        ident = await _verify_identity(token)
    except HTTPException:
        _admin_denied("missing_token" if not token else "invalid_token")
        return None
    reason = _admin_refusal(ident)
    if reason:
        _admin_denied(reason)
        return None
    try:
        await _check_allowlist(ident.uid, ident.email)
    except HTTPException:
        _admin_denied("not_allowlisted")
        return None
    return ident


async def verify_admin(authorization: str | None = Header(default=None)) -> Identity:
    """The admin's identity; anyone else gets the plain 404 an unknown path gets.

    Never 401 or 403, even for a missing token, so a scan cannot tell the
    route exists.
    """
    ident = await _admin_identity(_bearer(authorization))
    if ident is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return ident


# ---------------------------------------------------------------------------
# The spend consent seam
#
# ``tools.spend`` holds the logic and imports nothing from FastAPI or from
# ``api/``; this is the HTTP half, and it lives here for the same reason
# ``verify_user`` does — it is a dependency, and dependencies are what this
# module is.
#
# The seam goes on user-facing routes only, never on ``/tasks/*``: a task
# handler executes an action already consented to at the click, and gating it
# would break the queue path and invite a bypass.
# ---------------------------------------------------------------------------


class SpendConfirm(BaseModel):
    """Optional body on a route that can spend: the token from a prior 402."""

    confirm: str | None = None


def spend_client() -> firestore.AsyncClient:
    """Async client for the consent documents.

    Its own memo rather than ``_client()`` above, which exists only for the
    allowlist and stays unbuilt while that flag is off; this one is built as
    soon as anybody clicks a paid button.
    """
    global _spend_db
    if _spend_db is None:
        _spend_db = firestore.AsyncClient()
    return _spend_db


_spend_db: firestore.AsyncClient | None = None


async def _confirm_token(request: Request) -> str | None:
    """The ``confirm`` field out of the request body, if there is one.

    Read off the raw request rather than declared as a body parameter, so one
    dependency fits every route regardless of what else that route's body
    carries. Starlette caches the body on the request, so the route's own
    model still parses it afterwards.
    """
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    token = body.get("confirm")
    return token if isinstance(token, str) and token else None


async def spend_402(db, user_id: str, action: str) -> HTTPException:
    """The "I need to ask you first" answer, with a fresh quote and token.

    402, not 409: 409 already means "wrong application state" on ``/submit``
    and ``/regenerate``, and a client that cannot tell them apart will retry
    the wrong one.
    """
    estimate = await spend.build(db, user_id, action)
    token = await spend.preflight(db, user_id, action, estimate)
    return HTTPException(
        status_code=402,
        detail={
            "needs_confirmation": True,
            "action": action,
            "estimate": estimate.as_dict(),
            "confirm_token": token,
        },
    )


def required(action: str):
    """Dependency factory: this route spends, so it needs a token for ``action``.

    Returns the :class:`~tools.spend.estimate.Estimate` the user agreed to, so
    the route can record what was quoted rather than re-deriving it.
    """
    if action not in spend.ACTIONS:
        raise ValueError(f"unknown spend action {action!r}")

    async def dependency(
        request: Request, user_id: str = Depends(verify_user)
    ) -> spend.Estimate:
        db = spend_client()
        token = await _confirm_token(request)
        try:
            return await spend.consume(db, user_id, action, token)
        except spend.ConsentRequired:
            raise await spend_402(db, user_id, action) from None

    return dependency
