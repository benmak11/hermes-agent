# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Which plan a user is on, and what the trial allows.

Stored as one map on the user doc, ``users/{uid}.plan``::

    tier: "trial" | "paid"
    trial_started_at: iso      # when the trial clock started

Everything here reads off a user document the caller already holds, so asking
"which plan?" costs no Firestore read. Anything that is not exactly ``paid``
reads as ``trial``: an account with no plan, a garbled tier, a missing map. The
paid tier is only ever written by the paywall; nothing here sets it or takes it
away.

The trial clock starts at the first onboarding completion
(:func:`onboarding_fields`). Accounts that onboarded before plans existed have
no start; :func:`ensure_trial_start` stamps "now" the first time it matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from google.cloud import firestore

from obs.logging import get_logger

log = get_logger("tools.account")

FIELD = "plan"

TRIAL = "trial"
PAID = "paid"

#: How long a trial runs auto-discovery and the liveness sweep unattended.
TRIAL_AUTO_DAYS = 7


@dataclass(frozen=True)
class Plan:
    tier: str
    trial_started_at: datetime | None


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def plan_of(doc: dict | None) -> Plan:
    """The plan a user document describes. Pure; never raises."""
    raw = (doc or {}).get(FIELD)
    raw = raw if isinstance(raw, dict) else {}
    tier = PAID if raw.get("tier") == PAID else TRIAL
    return Plan(tier=tier, trial_started_at=_parse_ts(raw.get("trial_started_at")))


def tier_of(doc: dict | None) -> str:
    return plan_of(doc).tier


def is_onboarded(doc: dict | None) -> bool:
    """Has this account a profile? The same test ``GET /profile`` uses."""
    return bool((doc or {}).get("full_name"))


def auto_until(doc: dict | None, *, now: datetime) -> datetime | None:
    """When a trial's unattended runs stop; ``None`` for a paid plan.

    A trial with no stored start counts from ``now`` — what
    :func:`ensure_trial_start` would stamp — so a display never shows a week
    that has already ended for an account whose clock has not started.
    """
    plan = plan_of(doc)
    if plan.tier == PAID:
        return None
    start = plan.trial_started_at or now
    return start + timedelta(days=TRIAL_AUTO_DAYS)


def auto_allowed(doc: dict | None, *, now: datetime) -> bool:
    """May the scheduler run this user's loops unattended right now?

    The stored toggles are not consulted: they say what the user wants, this
    says what the plan allows, and the tick needs both.
    """
    until = auto_until(doc, now=now)
    return until is None or now < until


def view(doc: dict | None, *, now: datetime, scoring_per_day: int) -> dict:
    """The plan as the web app reads it."""
    plan = plan_of(doc)
    until = auto_until(doc, now=now)
    return {
        "tier": plan.tier,
        "trial_started_at": (
            plan.trial_started_at.isoformat() if plan.trial_started_at else None
        ),
        "auto_until": until.isoformat() if until else None,
        "auto_active": until is None or now < until,
        "scoring_per_day": scoring_per_day,
    }


def onboarding_fields(existing: dict | None, *, now: datetime) -> dict:
    """Plan fields to merge into the first onboarding-completion write.

    Empty when a trial start is already stored, so a re-completion (after a
    failed kickoff, or a re-uploaded résumé) never restarts the clock. A
    stored ``paid`` tier is carried over, never downgraded.
    """
    plan = plan_of(existing)
    if plan.trial_started_at is not None:
        return {}
    return {FIELD: {"tier": plan.tier, "trial_started_at": now.isoformat()}}


async def ensure_trial_start(db, user_id: str, *, now: datetime) -> datetime | None:
    """Stamp ``plan.trial_started_at`` if it is absent; return the stored start.

    For accounts that onboarded before plans existed. One transaction, once
    per account: it writes only the start (leaving any tier alone, so it
    cannot downgrade a concurrent ``paid``), declines on a missing document
    rather than recreating it, and keeps a start another writer got in first.
    ``None`` only when the document is gone. Errors propagate.
    """
    ref = db.collection("users").document(user_id)

    @firestore.async_transactional
    async def _stamp(transaction) -> datetime | None:
        snap = await ref.get(transaction=transaction)
        if not snap.exists:
            return None
        stored = plan_of(snap.to_dict()).trial_started_at
        if stored is not None:
            return stored
        transaction.set(ref, {FIELD: {"trial_started_at": now.isoformat()}}, merge=True)
        return now

    started = await _stamp(db.transaction())
    if started == now:
        log.info("plan.trial_start_stamped", user_id=user_id)
    return started
