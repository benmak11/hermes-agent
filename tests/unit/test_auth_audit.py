# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.auth_audit``: counts who the verify screen will block, prints no
email address, and reads every page of the Firebase Auth listing."""

from __future__ import annotations

import pytest
from auth_fakes import FakeAuth, auth_user

import cli.auth_audit as auth_audit

USERS = [
    [
        auth_user("u1", "a@example.com", providers=("google.com",)),
        auth_user("u2", "b@example.com", email_verified=False, providers=("password",)),
        auth_user("u3", "c@example.com", providers=("password",)),
    ],
    [
        auth_user(
            "u4",
            "d@example.com",
            email_verified=False,
            providers=("google.com", "password"),
        ),
        auth_user(
            "u5", "e@example.com", email_verified=False, providers=("google.com",)
        ),
        auth_user(
            "u6",
            "f@example.com",
            email_verified=False,
            disabled=True,
            providers=("password",),
        ),
    ],
]


def test_audit_counts_every_page():
    report = auth_audit.audit(FakeAuth(*USERS).list_users().iterate_all())
    assert report["total"] == 6
    assert report["disabled"] == 1
    assert report["by_provider"] == {
        "google.com": 2,
        "google.com+password": 1,
        "password": 3,
    }
    assert report["password_verified"] == 1
    assert report["password_only_unverified"] == ["u2", "u6"]
    assert report["linked_unverified"] == ["u4"]


def test_an_unverified_google_only_account_is_not_counted_as_blocked():
    report = auth_audit.audit([USERS[1][1]])
    assert report["password_only_unverified"] == []
    assert report["linked_unverified"] == []


@pytest.mark.parametrize("list_uids", [False, True])
def test_main_prints_no_email(monkeypatch, capsys, list_uids):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(auth_audit, "_firebase_auth", lambda project: FakeAuth(*USERS))

    auth_audit.main(["--list-uids"] if list_uids else [])

    out = capsys.readouterr().out
    assert "@" not in out
    assert "password only, UNVERIFIED:      2" in out
    assert ("  u2\n" in out) is list_uids


def test_main_refuses_without_a_pinned_project(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setattr(
        auth_audit,
        "_firebase_auth",
        lambda project: pytest.fail("reached Firebase without a project"),
    )
    with pytest.raises(AssertionError, match="GOOGLE_CLOUD_PROJECT"):
        auth_audit.main([])
