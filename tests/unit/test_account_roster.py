# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``tools.account.roster``: one row per person, the right status, every
issue — and a loader that reads only what it must."""

from __future__ import annotations

import asyncio

import pytest
from auth_fakes import FakeAuth, auth_user
from firestore_fakes import FakeQueryDB

from tools.account import roster
from tools.account.roster import reconcile

SEAT_ADDED = "2026-08-01T00:00:00+00:00"  # before auth_user's default last sign-in


def seat(email, *, revoked=False, added_at=SEAT_ADDED, **extra):
    return {
        "email": email,
        "added_at": added_at,
        "added_by": "op",
        "note": None,
        "revoked": revoked,
        **extra,
    }


def wait(email, **extra):
    return {
        "email": email,
        "uid": "w",
        "source": "hero",
        "email_verified": True,
        "first_seen": "2026-09-20T12:00:00+00:00",
        "last_seen": "2026-09-21T12:00:00+00:00",
        **extra,
    }


def one(auth=(), seats=(), waitlist=(), docs=None, *, enforced=True):
    rows = reconcile(auth, seats, waitlist, docs or {}, enforced=enforced)
    assert len(rows) == 1, rows
    return rows[0]


# ------------------------------------------------------------------ statuses


def test_deleted():
    row = one(
        [auth_user("u1", "a@x.com")],
        docs={"u1": {"full_name": "A", "deleted_at": "2026-09-03T12:00:00+00:00"}},
    )
    assert row.status == "deleted"
    assert "deletion_unfinished" in row.issues
    assert "seat_on_deleted_account" not in row.issues


def test_deleted_beats_active_and_flags_the_seat():
    row = one(
        [auth_user("u1", "a@x.com")],
        [seat("a@x.com")],
        docs={"u1": {"deleted_at": "2026-09-03T12:00:00+00:00"}},
    )
    assert row.status == "deleted"
    assert {"deletion_unfinished", "seat_on_deleted_account"} <= set(row.issues)
    assert "login_without_seat" not in row.issues


def test_data_without_login():
    row = one(docs={"ghost": {"full_name": "G"}})
    assert row.key == "uid:ghost"
    assert row.status == "data_without_login"
    assert row.issues == ["data_without_login"]
    assert row.auth is None


def test_disabled():
    row = one([auth_user("u1", "a@x.com", disabled=True)])
    assert row.status == "disabled"
    assert "login_without_seat" not in row.issues


def test_active_with_a_seat_under_enforcement():
    row = one([auth_user("u1", "a@x.com")], [seat("a@x.com")])
    assert row.status == "active"
    assert row.issues == []


def test_active_without_a_seat_when_enforcement_is_off():
    row = one([auth_user("u1", "a@x.com")], enforced=False)
    assert row.status == "active"
    assert row.issues == ["login_without_seat"]


def test_no_seat_under_enforcement():
    row = one([auth_user("u1", "a@x.com")])
    assert row.status == "no_seat"
    assert row.issues == ["login_without_seat"]


def test_seat_without_login():
    row = one(seats=[seat("a@x.com")])
    assert row.key == "email:a@x.com"
    assert row.status == "seat_without_login"
    assert row.issues == ["seat_unused"]


def test_waitlisted():
    row = one([auth_user("u1", "a@x.com")], waitlist=[wait("a@x.com")])
    assert row.status == "waitlisted"
    assert row.issues == []
    assert row.waitlist is not None and row.waitlist.source == "hero"


def test_waitlist_without_login():
    row = one(waitlist=[wait("a@x.com")])
    assert row.status == "waitlisted"
    assert row.issues == ["waitlist_without_login"]


def test_revoked():
    row = one([auth_user("u1", "a@x.com")], [seat("a@x.com", revoked=True)])
    assert row.status == "revoked"
    assert row.issues == ["login_without_seat"]


def test_a_revoked_seat_with_no_login_is_revoked_and_quiet():
    row = one(seats=[seat("a@x.com", revoked=True)])
    assert row.status == "revoked"
    assert row.issues == []


def test_a_seat_and_waitlist_entry_with_no_login_are_one_row():
    row = one(seats=[seat("a@x.com")], waitlist=[wait("a@x.com")])
    assert row.status == "seat_without_login"
    assert set(row.issues) == {"seat_unused", "waitlist_without_login"}


# -------------------------------------------------------------------- issues


def test_seat_unused_when_last_sign_in_predates_the_seat():
    row = one(
        [auth_user("u1", "a@x.com")],
        [seat("a@x.com", added_at="2026-09-15T00:00:00+00:00")],
    )
    assert row.status == "active"
    assert row.issues == ["seat_unused"]


def test_seat_used_when_signed_in_after_the_grant():
    row = one([auth_user("u1", "a@x.com")], [seat("a@x.com")])
    assert "seat_unused" not in row.issues


def test_unverified_email():
    row = one([auth_user("u1", "a@x.com", email_verified=False)], [seat("a@x.com")])
    assert row.issues == ["unverified_email"]


def test_no_email_can_never_hold_a_seat():
    row = one([auth_user("u1", None)], [seat("")])
    assert row.email is None
    assert row.seat is None
    assert set(row.issues) == {"login_without_seat", "no_email"}


def test_duplicate_email_gives_both_rows_the_seat_and_the_flag():
    rows = reconcile(
        [auth_user("u1", "User@x.com"), auth_user("u2", "user@x.com ")],
        [seat("user@x.com")],
        [],
        {},
        enforced=True,
    )
    assert [r.uid for r in rows] == ["u1", "u2"]
    for row in rows:
        assert row.seat is not None
        assert row.status == "active"
        assert row.issues == ["duplicate_email"]


# --------------------------------------------------------------------- joins


def test_the_join_uses_the_auth_email_not_the_resume_email():
    """``users/{uid}.email`` is résumé-extracted: a seat on it is not this
    person's seat."""
    rows = reconcile(
        [auth_user("u1", "login@x.com")],
        [seat("resume@x.com")],
        [],
        {"u1": {"full_name": "A", "email": "resume@x.com"}},
        enforced=True,
    )
    by_key = {r.key: r for r in rows}
    assert by_key["uid:u1"].seat is None
    assert by_key["uid:u1"].status == "no_seat"
    assert by_key["email:resume@x.com"].status == "seat_without_login"


def test_the_join_is_case_insensitive():
    row = one([auth_user("u1", "User@Example.com")], [seat("user@example.com")])
    assert row.seat is not None
    assert row.status == "active"


def test_auth_joins_users_docs_by_uid():
    row = one(
        [auth_user("u1", "a@x.com")],
        [seat("a@x.com")],
        docs={"u1": {"full_name": "A", "signup_source": "nav"}},
    )
    assert row.profile is not None
    assert row.profile.onboarded is True
    assert row.profile.signup_source == "nav"


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ({"full_name": "A"}, True),
        ({"full_name": "A", "onboarding_complete": True}, True),
        ({"full_name": "A", "onboarding_complete": False}, False),
        ({"onboarding_complete": True}, False),
        ({"full_name": ""}, False),
    ],
)
def test_onboarded_matches_the_profile_route(doc, expected):
    assert roster.onboarded(doc) is expected


def test_auth_timestamps_become_iso_utc():
    row = one([auth_user("u1", "a@x.com", last_sign_in_ms=None)], [seat("a@x.com")])
    assert row.auth is not None
    assert row.auth.created_at == "2026-01-01T00:00:00+00:00"
    assert row.auth.last_sign_in_at is None


def test_needs_attention_sorts_first_then_status_then_email():
    rows = reconcile(
        [
            auth_user("ok-b", "b@x.com"),
            auth_user("ok-a", "a@x.com"),
            auth_user("bad", "z@x.com"),
        ],
        [seat("a@x.com"), seat("b@x.com")],
        [wait("w@x.com")],
        {},
        enforced=True,
    )
    assert [r.key for r in rows] == [
        "email:w@x.com",  # waitlisted, issue: waitlist_without_login
        "uid:bad",  # no_seat, issue: login_without_seat
        "uid:ok-a",
        "uid:ok-b",
    ]


# -------------------------------------------------------------------- loader


def _db(**extra):
    return FakeQueryDB(
        {
            "allowlist": {"a@x.com": seat("a@x.com")},
            "waitlist": {"w@x.com": wait("w@x.com")},
            "users": {
                "u1": {
                    "full_name": "A",
                    "email": "junk@resume.com",
                    "skills": ["python"],
                    "onboarding_complete": True,
                }
            },
            "users/u1/jobs": {"j1": {"title": "x"}},
            **extra,
        }
    )


def test_the_fake_select_projects():
    """Guards the fake itself: a ``select`` that returned whole documents
    would let the loader test below pass while reading everything."""
    db = FakeQueryDB({"c": {"d": {"keep": 1, "drop": 2}}})

    async def read():
        return [s.to_dict() async for s in db.collection("c").select(["keep"]).stream()]

    assert asyncio.run(read()) == [{"keep": 1}]


def test_the_users_read_is_projected():
    db = _db()
    docs = asyncio.run(roster._users(db))
    assert docs == {"u1": {"full_name": "A", "onboarding_complete": True}}
    assert db.selects == [("users", roster.USER_FIELDS)]


def test_the_loader_reads_exactly_the_three_collections_and_no_subcollection():
    db = _db()
    asyncio.run(roster.load_roster(db, FakeAuth([auth_user("u1", "a@x.com")])))
    assert sorted(q[0] for q in db.queries) == ["allowlist", "users", "waitlist"]
    assert db.gets == []


def test_a_page_two_auth_user_appears():
    auth = FakeAuth(
        [auth_user("u1", "a@x.com")],
        [auth_user("u2", "b@x.com")],
    )
    result = asyncio.run(roster.load_roster(_db(), auth))
    uids = {r.uid for r in result.accounts if r.auth is not None}
    assert uids == {"u1", "u2"}


def test_the_loader_builds_a_summary(monkeypatch):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", "1")
    result = asyncio.run(
        roster.load_roster(_db(), FakeAuth([auth_user("u1", "a@x.com")]))
    )
    assert result.enforced is True
    assert result.summary.total == 2
    assert result.summary.seats_active == 1
    assert result.summary.by_status["active"] == 1
    assert result.summary.by_status["waitlisted"] == 1
    assert result.summary.needs_attention == 1


@pytest.mark.parametrize("failing", ["firebase_auth", "allowlist", "waitlist", "users"])
def test_any_failed_source_fails_the_whole_load(monkeypatch, failing):
    auth = FakeAuth(
        [auth_user("u1", "a@x.com")],
        fail=RuntimeError("boom") if failing == "firebase_auth" else None,
    )

    async def broken(db):
        raise RuntimeError("boom")

    if failing == "allowlist":
        monkeypatch.setattr(roster.allowlist, "list_entries", broken)
    elif failing == "waitlist":
        monkeypatch.setattr(roster.allowlist, "list_waitlist", broken)
    elif failing == "users":
        monkeypatch.setattr(roster, "_users", broken)

    with pytest.raises(roster.RosterSourceError) as err:
        asyncio.run(roster.load_roster(_db(), auth))
    assert err.value.source == failing
