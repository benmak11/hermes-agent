# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Single-use consent tokens for actions that spend money.

**A token is a Firestore document, not an HMAC.** Two Firestore operations are
free next to a Vertex batch; it works across Cloud Run instances with no
secret to provision and no key rotation to get wrong; single-use falls out of
the delete rather than needing a replay cache; and the estimate the user was
actually shown travels *with* the token, so the route that spends can record
what was agreed rather than re-deriving it and hoping it matches.

The lifecycle is:

    preflight(...) -> token      # stored under users/{uid}/spend_consents
    ... the UI shows the estimate and the user clicks confirm ...
    consume(..., token) -> Estimate    # checked, deleted, returned

Four things are checked, and each one is a way the seam could fail open:

- the document exists (an invented token is not a yes);
- it is under **this user's** document — the lookup is a path, never a
  collection-group query, so one user's token cannot authorise another's spend;
- the ``action`` matches — a yes to "find jobs" is not a yes to "score 200";
- it has not expired, **checked here in code**. Firestore's TTL policy is a
  per-collection-group GCP setting that lives in no file in this repo (see
  ``tools.run_costs``' Retention note for the same trap); until someone enables
  it nothing is ever deleted, and even after that TTL collection is best-effort
  and lags by hours. TTL is garbage collection. This is the expiry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from obs.logging import get_logger
from tools.spend.estimate import Estimate

log = get_logger("tools.spend")

COLLECTION = "spend_consents"

#: How long a quote stands. Long enough to read a confirm sheet and think
#: about it; short enough that the budget counters behind the quoted number
#: have not moved underneath it.
_TTL = timedelta(minutes=15)


class ConsentRequired(Exception):
    """No valid token for this action — the caller owes the user a question."""


def _consents(db, user_id: str):
    """The user's consent collection. **A path, deliberately.**

    Resolving a token by collection-group query would find it wherever it
    lives, which is precisely the bug: token scope would then be "anyone who
    knows the uuid" instead of "the account that minted it".
    """
    return db.collection("users").document(user_id).collection(COLLECTION)


def _now() -> datetime:
    return datetime.now(UTC)


async def preflight(db, user_id: str, action: str, estimate: Estimate) -> str:
    """Mint a token for ``action``, with the quoted estimate attached."""
    token = uuid.uuid4().hex
    issued = _now()
    await (
        _consents(db, user_id)
        .document(token)
        .set(
            {
                "action": action,
                "estimate": estimate.as_dict(),
                "issued_at": issued.isoformat(),
                # For Firestore's TTL policy to collect, if and when it is
                # enabled. Never read by :func:`consume` — see the module note.
                "expires_at": issued + _TTL,
            }
        )
    )
    log.info(
        "spend.consent_issued",
        user_id=user_id,
        action=action,
        units=estimate.units,
        usd_low=estimate.usd_low,
        usd_high=estimate.usd_high,
    )
    return token


async def consume(db, user_id: str, action: str, token: str | None) -> Estimate:
    """Spend the token: validate, delete, return the estimate it carried.

    Raises :class:`ConsentRequired` for every failure mode, indistinguishably
    — an invalid token and a missing one are the same answer to the caller
    ("ask again"), and telling them apart would let a caller probe which
    tokens exist.

    Deleted **before** the paid work starts, not after. A crash mid-run then
    costs the user a second confirmation rather than leaving a token that a
    retry could spend again.
    """
    if not token:
        raise ConsentRequired("no confirmation token")

    ref = _consents(db, user_id).document(token)
    snap = await ref.get()
    if not snap.exists:
        raise ConsentRequired("unknown or already-used confirmation token")
    doc = snap.to_dict() or {}

    # Deleted before either check below, because a token that failed one is
    # still burnt: it stops a mismatched-action token being retried against
    # the route it *would* fit.
    await ref.delete()

    if doc.get("action") != action:
        log.warning(
            "spend.consent_action_mismatch",
            user_id=user_id,
            wanted=action,
            got=doc.get("action"),
        )
        raise ConsentRequired("confirmation was for a different action")

    issued = _parse_iso(doc.get("issued_at"))
    if issued is None or _now() - issued > _TTL:
        log.info("spend.consent_expired", user_id=user_id, action=action)
        raise ConsentRequired("confirmation expired")

    try:
        estimate = Estimate.from_dict(doc.get("estimate") or {})
    except (KeyError, TypeError):
        raise ConsentRequired("confirmation carried no usable estimate") from None

    log.info(
        "spend.consent_consumed",
        user_id=user_id,
        action=action,
        units=estimate.units,
        usd_low=estimate.usd_low,
        usd_high=estimate.usd_high,
    )
    return estimate


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
