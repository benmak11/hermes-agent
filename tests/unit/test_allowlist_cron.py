# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cron_tick``'s allowlist skip — the seam PR C's ``is_deleted`` check also
lives at.

``cron_tick`` streams every ``users/{uid}`` document without ever calling
``verify_user``, so a de-allowlisted user's background loops have to be
stopped *here* too, or removing them from the allowlist bounds none of their
spend. Modelled on the ``is_deleted`` skip immediately above it in the loop:
same ``continue``, same counter shape — see ``test_account_delete.py``'s
``test_the_cron_fan_out_skips_a_deleted_account_whole`` for that one's twin.

Gated on ``ALLOWLIST_ENFORCED`` and off by default; every test below that
leaves the flag unset is part of the proof this ships inert.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

import api.routes.discovery as discovery
from tools import allowlist


def _users(docs: dict[str, dict]):
    return SimpleNamespace(
        collection=lambda name: SimpleNamespace(
            stream=lambda: [
                SimpleNamespace(id=uid, to_dict=lambda d=d: d)
                for uid, d in docs.items()
            ]
        )
    )


@pytest.fixture
def cron(monkeypatch):
    """``cron_tick`` over a single live, non-deleted user, ``u1``."""
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.setenv("QUEUE_MODE", "1")
    ticked: list[str] = []
    reaped: list[str] = []

    async def fake_tick(user_id, *, force_check=False, doc=None):
        ticked.append(user_id)

    async def fake_reap(user_id, *, background_tasks):
        reaped.append(user_id)
        return {"recovered": 0, "truncated": 0}

    monkeypatch.setattr(discovery, "tick_user", fake_tick)
    monkeypatch.setattr(discovery, "reap_user", fake_reap)
    monkeypatch.setattr(discovery, "maybe_enqueue_batch_resume", lambda: False)
    monkeypatch.setattr(discovery, "_client", lambda: _users({"u1": {}}))
    # Never actually built while ``_allowlisted`` itself is faked below.
    monkeypatch.setattr(discovery, "_async_client", lambda: object())
    return SimpleNamespace(ticked=ticked, reaped=reaped)


async def _explode_async(*args, **kwargs):
    raise AssertionError("must not have checked the allowlist")


# ---------------------------------------------------------------------------
# ALLOWLIST_ENFORCED unset: a full no-op
# ---------------------------------------------------------------------------


def test_enforcement_off_never_checks_the_allowlist(cron, monkeypatch):
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.setattr(discovery, "_allowlisted", _explode_async)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert cron.ticked == ["u1"]
    assert cron.reaped == ["u1"]
    assert result["not_allowlisted"] == 0


# ---------------------------------------------------------------------------
# Enforced: the skip itself
# ---------------------------------------------------------------------------


def test_a_de_allowlisted_user_gets_no_tick_or_reap(cron, monkeypatch):
    """Nothing here is free: a claimed slot is a write and a dispatched cycle
    is a crawl — same reasoning ``_account_deleted`` documents for itself."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")

    async def not_allowed(uid):
        return False

    monkeypatch.setattr(discovery, "_allowlisted", not_allowed)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert cron.ticked == []
    assert cron.reaped == []
    assert result["not_allowlisted"] == 1
    assert result["users"] == 1


def test_an_allowlisted_user_still_ticks(cron, monkeypatch):
    """Positive control: the guard is the allowlist check, not the cycle."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")

    async def allowed(uid):
        return True

    monkeypatch.setattr(discovery, "_allowlisted", allowed)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert cron.ticked == ["u1"]
    assert cron.reaped == ["u1"]
    assert result["not_allowlisted"] == 0


def test_a_fan_out_of_only_de_allowlisted_users_does_not_answer_500(cron, monkeypatch):
    """``attempted`` excludes both tombstoned *and* de-allowlisted users — the
    same reasoning the ``deleted`` counter already gets. Getting this wrong in
    the other direction would make ``failed == attempted`` true for zero
    attempts and raise a 500 the scheduler would then hammer retrying."""
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")

    async def not_allowed(uid):
        return False

    monkeypatch.setattr(discovery, "_allowlisted", not_allowed)

    result = asyncio.run(discovery.cron_tick(BackgroundTasks()))

    assert result["ok"] is True
    assert result["failed"] == 0


# ---------------------------------------------------------------------------
# _allowlisted: the Auth email, never users/{uid}.email
# ---------------------------------------------------------------------------


def test_allowlisted_reads_the_auth_email_not_the_profile_field(monkeypatch):
    """The profile document's ``email`` is résumé-extracted and might not even
    be present; the Auth record is what the allowlist is keyed on everywhere
    else in this PR."""
    seen: dict = {}

    class _FakeAuth:
        def get_user(self, uid):
            return SimpleNamespace(email="auth@example.com")

    async def fake_is_allowed(db, email):
        seen["email"] = email
        return True

    monkeypatch.setattr(discovery, "firebase_auth", lambda: _FakeAuth())
    monkeypatch.setattr(discovery, "_async_client", lambda: object())
    monkeypatch.setattr(allowlist, "is_allowed", fake_is_allowed)

    result = asyncio.run(discovery._allowlisted("u1"))

    assert result is True
    assert seen["email"] == "auth@example.com"


def test_allowlisted_fails_closed_on_an_auth_lookup_error(monkeypatch):
    """The same fail-closed bias ``tools.allowlist.is_allowed`` documents for
    itself: a lookup that can't answer is read as "not allowed", not skipped."""

    class _FakeAuth:
        def get_user(self, uid):
            raise RuntimeError("Admin SDK unreachable")

    monkeypatch.setattr(discovery, "firebase_auth", lambda: _FakeAuth())
    monkeypatch.setattr(allowlist, "is_allowed", _explode_async)

    result = asyncio.run(discovery._allowlisted("u1"))

    assert result is False
