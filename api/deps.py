# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared FastAPI dependencies for the web API (auth)."""

from __future__ import annotations

import os

from fastapi import Header, HTTPException, Query

_firebase_ready = False


def _ensure_firebase() -> None:
    """Lazily initialize the Firebase Admin SDK (uses ADC)."""
    global _firebase_ready
    if _firebase_ready:
        return
    import firebase_admin

    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    _firebase_ready = True


def _verify_token(token: str | None) -> str:
    """Verify a Firebase ID token (or honor the dev bypass) and return the uid."""
    if os.getenv("AUTH_DEV_MODE") == "1" and os.getenv("AUTH_DEV_USER"):
        return os.environ["AUTH_DEV_USER"]

    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    _ensure_firebase()
    from firebase_admin import auth as fb_auth

    try:
        decoded = fb_auth.verify_id_token(token)
        return decoded["uid"]
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e


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
    return _verify_token(token)


async def verify_user_query(token: str | None = Query(default=None)) -> str:
    """Like verify_user but reads the token from a ?token= query param.

    For SSE (EventSource) endpoints, where the browser cannot set an
    Authorization header.
    """
    return _verify_token(token)
