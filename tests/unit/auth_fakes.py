# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""A Firebase Auth stand-in whose ``list_users()`` is paginated the way the
real one is: ``.users`` is the first page only, ``iterate_all()`` is every
page. Code that reads ``.users`` misses page two and a test sees it.

``get_user_by_email`` searches every page and raises like the real one:
``UserNotFoundError`` for no match, ``ValueError`` for a malformed address."""

from __future__ import annotations

from types import SimpleNamespace


def auth_user(
    uid: str,
    email: str | None,
    *,
    email_verified: bool = True,
    disabled: bool = False,
    display_name: str | None = None,
    created_ms: int = 1_767_225_600_000,  # 2026-01-01T00:00:00Z
    last_sign_in_ms: int | None = 1_788_220_800_000,  # 2026-09-01T00:00:00Z
    providers: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid,
        email=email,
        email_verified=email_verified,
        disabled=disabled,
        display_name=display_name,
        provider_data=[SimpleNamespace(provider_id=p) for p in providers],
        user_metadata=SimpleNamespace(
            creation_timestamp=created_ms, last_sign_in_timestamp=last_sign_in_ms
        ),
    )


class UserNotFoundError(Exception):
    """Stands in for ``firebase_admin.auth.UserNotFoundError``."""


class _Page:
    def __init__(self, pages: list[list]):
        self._pages = pages
        self.users = list(pages[0]) if pages else []

    def iterate_all(self):
        for page in self._pages:
            yield from page


class FakeAuth:
    """``pages`` is a list of pages of user records. ``fail`` makes every
    ``list_users`` and ``get_user_by_email`` call raise."""

    UserNotFoundError = UserNotFoundError

    def __init__(self, *pages: list, fail: Exception | None = None):
        self._pages = [list(p) for p in pages]
        self._fail = fail

    def list_users(self):
        if self._fail is not None:
            raise self._fail
        return _Page(self._pages)

    def get_user_by_email(self, email):
        if self._fail is not None:
            raise self._fail
        local, _, domain = (
            email.partition("@") if isinstance(email, str) else ("", "", "")
        )
        if not local or not domain:
            raise ValueError(f"Malformed email address string: {email!r}")
        # Firebase Auth stores and matches addresses case-insensitively.
        for page in self._pages:
            for record in page:
                if (record.email or "").casefold() == email.casefold():
                    return record
        raise UserNotFoundError(f"No user record found for the provided email: {email}")
