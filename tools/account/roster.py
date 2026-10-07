# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Every account across Firebase Auth, the allowlist, the waitlist and
``users/``, joined into one row per person with a status and any issues.

Joined on the **Firebase Auth** email through :func:`tools.allowlist._key`,
never on ``users/{uid}.email``, which is résumé-extracted. Only top-level
``users`` documents are read, projected to a handful of fields: no profile
content and no subcollection leaves Firestore.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from tools import allowlist
from tools.account.delete import DELETED_AT

#: Status precedence: a row takes the first that applies.
STATUSES = (
    "deleted",
    "data_without_login",
    "disabled",
    "active",
    "seat_without_login",
    "waitlisted",
    "revoked",
    "no_seat",
)
Status = Literal[
    "deleted",
    "data_without_login",
    "disabled",
    "active",
    "seat_without_login",
    "waitlisted",
    "revoked",
    "no_seat",
]

#: The ``users/{uid}`` fields the roster reads; nothing else is fetched.
USER_FIELDS = (
    "full_name",
    "onboarding_complete",
    "signup_source",
    "signed_up_at",
    DELETED_AT,
)


class SeatInfo(BaseModel):
    revoked: bool
    added_at: str | None = None
    added_by: str | None = None
    note: str | None = None
    revoked_at: str | None = None
    revoked_by: str | None = None


class WaitlistInfo(BaseModel):
    first_seen: str | None = None
    last_seen: str | None = None
    source: str | None = None


class AuthInfo(BaseModel):
    created_at: str | None = None
    last_sign_in_at: str | None = None
    email_verified: bool
    disabled: bool


class ProfileInfo(BaseModel):
    onboarded: bool
    signup_source: str | None = None
    signed_up_at: str | None = None
    deleted_at: str | None = None


class AccountRow(BaseModel):
    key: str
    uid: str | None = None
    email: str | None = None
    display_name: str | None = None
    status: Status
    issues: list[str]
    seat: SeatInfo | None = None
    waitlist: WaitlistInfo | None = None
    auth: AuthInfo | None = None
    profile: ProfileInfo | None = None


class Summary(BaseModel):
    total: int
    needs_attention: int
    seats_active: int
    by_status: dict[str, int]
    #: ``MAX_USERS``; ``None`` when unset or invalid, which refuses grants.
    seat_cap: int | None = None


class Roster(BaseModel):
    generated_at: str
    enforced: bool
    summary: Summary
    accounts: list[AccountRow]


class RosterSourceError(Exception):
    """One of the four reads failed; ``source`` names which.

    ``cause_type`` is the failure's class name only: upstream messages can
    name a user, so callers log this rather than ``str(self)``.
    """

    def __init__(self, source: str, cause: BaseException):
        super().__init__(f"{source}: {cause}")
        self.source = source
        self.cause_type = type(cause).__name__


def onboarded(doc: Mapping[str, Any]) -> bool:
    """The ``GET /profile`` rule: a ``full_name``, and ``onboarding_complete``
    not explicitly ``False`` (CLI-synced profiles predate the flag)."""
    return (
        bool(doc.get("full_name")) and doc.get("onboarding_complete", True) is not False
    )


def _ms_iso(ms: int | None) -> str | None:
    """Firebase Auth's epoch milliseconds as an ISO-8601 UTC string."""
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seat_active(seat: Mapping[str, Any] | None) -> bool:
    return seat is not None and not seat.get("revoked")


def _status(auth, seat, waitlist, doc, *, enforced: bool) -> Status:
    if doc is not None and doc.get(DELETED_AT):
        return "deleted"
    if doc is not None and auth is None:
        return "data_without_login"
    if auth is not None and auth.disabled:
        return "disabled"
    if auth is not None and (not enforced or _seat_active(seat)):
        return "active"
    if auth is None and _seat_active(seat):
        return "seat_without_login"
    if waitlist is not None:
        return "waitlisted"
    if seat is not None:
        return "revoked"
    return "no_seat"


def _issues(auth, seat, waitlist, status: Status, *, duplicate: bool) -> list[str]:
    issues: list[str] = []
    active_seat = _seat_active(seat)
    if (
        auth is not None
        and not active_seat
        and waitlist is None
        and status not in ("deleted", "disabled")
    ):
        issues.append("login_without_seat")
    if active_seat:
        assert seat is not None
        if auth is None:
            issues.append("seat_unused")
        else:
            added = _parse_iso(seat.get("added_at"))
            last_ms = auth.user_metadata.last_sign_in_timestamp
            if added is not None and (
                not last_ms or datetime.fromtimestamp(last_ms / 1000, UTC) < added
            ):
                issues.append("seat_unused")
        if status == "deleted":
            issues.append("seat_on_deleted_account")
    if status == "deleted":
        issues.append("deletion_unfinished")
    if status == "data_without_login":
        issues.append("data_without_login")
    if waitlist is not None and auth is None:
        issues.append("waitlist_without_login")
    if auth is not None and not auth.email_verified:
        issues.append("unverified_email")
    if auth is not None and not allowlist._key(auth.email):
        issues.append("no_email")
    if duplicate:
        issues.append("duplicate_email")
    return issues


def _row(key, uid, email, auth, seat, waitlist, doc, *, enforced, duplicate):
    status = _status(auth, seat, waitlist, doc, enforced=enforced)
    display_name = (getattr(auth, "display_name", None) if auth else None) or (
        doc.get("full_name") if doc else None
    )
    return AccountRow(
        key=key,
        uid=uid,
        email=email,
        display_name=display_name or None,
        status=status,
        issues=_issues(auth, seat, waitlist, status, duplicate=duplicate),
        seat=None
        if seat is None
        else SeatInfo(
            revoked=bool(seat.get("revoked")),
            added_at=seat.get("added_at"),
            added_by=seat.get("added_by"),
            note=seat.get("note"),
            revoked_at=seat.get("revoked_at"),
            revoked_by=seat.get("revoked_by"),
        ),
        waitlist=None
        if waitlist is None
        else WaitlistInfo(
            first_seen=waitlist.get("first_seen"),
            last_seen=waitlist.get("last_seen"),
            source=waitlist.get("source"),
        ),
        auth=None
        if auth is None
        else AuthInfo(
            created_at=_ms_iso(auth.user_metadata.creation_timestamp),
            last_sign_in_at=_ms_iso(auth.user_metadata.last_sign_in_timestamp),
            email_verified=bool(auth.email_verified),
            disabled=bool(auth.disabled),
        ),
        profile=None
        if doc is None
        else ProfileInfo(
            onboarded=onboarded(doc),
            signup_source=doc.get("signup_source"),
            signed_up_at=doc.get("signed_up_at"),
            deleted_at=doc.get(DELETED_AT),
        ),
    )


def _sort_key(row: AccountRow):
    return (
        not row.issues,
        STATUSES.index(row.status),
        row.email or "",
        row.key,
    )


def reconcile(
    auth_users: Iterable[Any],
    seats: Iterable[Mapping[str, Any]],
    waitlist: Iterable[Mapping[str, Any]],
    user_docs: Mapping[str, Mapping[str, Any]],
    *,
    enforced: bool,
) -> list[AccountRow]:
    """Join the four sources into sorted rows. Pure: no I/O.

    ``seats`` and ``waitlist`` are :func:`tools.allowlist.list_entries` /
    :func:`~tools.allowlist.list_waitlist` output (``email`` is the doc id);
    ``user_docs`` maps uid to its projected ``users`` document.
    """
    seat_by_key = {allowlist._key(s.get("email")): s for s in seats}
    wait_by_key = {allowlist._key(w.get("email")): w for w in waitlist}
    auth_list = list(auth_users)

    uids_per_key: dict[str, int] = {}
    for record in auth_list:
        key = allowlist._key(record.email)
        if key:
            uids_per_key[key] = uids_per_key.get(key, 0) + 1

    rows: list[AccountRow] = []
    joined_keys: set[str] = set()
    auth_uids: set[str] = set()
    for record in auth_list:
        key = allowlist._key(record.email)
        auth_uids.add(record.uid)
        if key:
            joined_keys.add(key)
        rows.append(
            _row(
                f"uid:{record.uid}",
                record.uid,
                record.email or None,
                record,
                seat_by_key.get(key) if key else None,
                wait_by_key.get(key) if key else None,
                user_docs.get(record.uid),
                enforced=enforced,
                duplicate=bool(key) and uids_per_key[key] > 1,
            )
        )

    for uid, doc in user_docs.items():
        if uid not in auth_uids:
            rows.append(
                _row(
                    f"uid:{uid}",
                    uid,
                    None,
                    None,
                    None,
                    None,
                    doc,
                    enforced=enforced,
                    duplicate=False,
                )
            )

    # A seat and a waitlist entry for the same address, with no login, are
    # one person.
    for key in sorted((seat_by_key.keys() | wait_by_key.keys()) - joined_keys):
        if not key:
            continue
        rows.append(
            _row(
                f"email:{key}",
                None,
                key,
                None,
                seat_by_key.get(key),
                wait_by_key.get(key),
                None,
                enforced=enforced,
                duplicate=False,
            )
        )

    rows.sort(key=_sort_key)
    return rows


def summarize(
    rows: list[AccountRow],
    seats: Iterable[Mapping[str, Any]],
    *,
    seat_cap: int | None = None,
) -> Summary:
    by_status = dict.fromkeys(STATUSES, 0)
    for row in rows:
        by_status[row.status] += 1
    return Summary(
        total=len(rows),
        needs_attention=sum(1 for r in rows if r.issues),
        seats_active=sum(1 for s in seats if _seat_active(s)),
        seat_cap=seat_cap,
        by_status=by_status,
    )


async def _users(db) -> dict[str, dict]:
    query = db.collection("users").select(list(USER_FIELDS))
    return {snap.id: snap.to_dict() or {} async for snap in query.stream()}


async def _auth_users(fb_auth) -> list:
    return await asyncio.to_thread(lambda: list(fb_auth.list_users().iterate_all()))


async def load_roster(db, fb_auth) -> Roster:
    """Read all four sources concurrently and reconcile them.

    Raises :class:`RosterSourceError` if any read fails: a partial roster
    would misreport, e.g. a failed Auth read makes every seat look unused.
    """
    sources = ("firebase_auth", "allowlist", "waitlist", "users")
    results = await asyncio.gather(
        _auth_users(fb_auth),
        allowlist.list_entries(db),
        allowlist.list_waitlist(db),
        _users(db),
        return_exceptions=True,
    )
    for source, result in zip(sources, results, strict=True):
        if isinstance(result, Exception):
            raise RosterSourceError(source, result) from result
        if isinstance(result, BaseException):
            raise result
    auth_users, seats, waitlist, user_docs = results
    assert isinstance(auth_users, list) and isinstance(seats, list)
    assert isinstance(waitlist, list) and isinstance(user_docs, dict)
    enforced = allowlist.enforced()
    rows = reconcile(auth_users, seats, waitlist, user_docs, enforced=enforced)
    return Roster(
        generated_at=datetime.now(UTC).isoformat(),
        enforced=enforced,
        summary=summarize(rows, seats, seat_cap=allowlist.seat_cap()),
        accounts=rows,
    )
