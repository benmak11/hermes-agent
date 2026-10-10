# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Count Firebase Auth users by sign-in provider and verified status. Read-only.

Answers "who sees the verify-your-email screen once ``REQUIRE_VERIFIED_EMAIL``
is on?": an email/password account (a ``password`` entry in its provider data)
whose address is not verified. Those are counted, and listed by uid only with
``--list-uids``. No email address is ever printed.

A user with both Google and a password linked passes the server check when
they sign in with Google (the token's ``sign_in_provider`` is ``google.com``)
and is refused only when they sign in with the password, so they are counted
separately.

Pinned to ``GOOGLE_CLOUD_PROJECT`` from the project-root ``.env``: ADC's
default quota project on a developer machine is a different project.

Usage:
    python -m cli.auth_audit
    python -m cli.auth_audit --list-uids
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from collections.abc import Iterable

from dotenv import load_dotenv

load_dotenv()

PASSWORD = "password"


def _providers(record) -> list[str]:
    return sorted({p.provider_id for p in (record.provider_data or [])})


def audit(records: Iterable) -> dict:
    """Tally ``records`` (Firebase ``UserRecord``-shaped). Pure.

    Returns counts plus the uids of the accounts the verify screen will block.
    """
    by_provider: Counter[str] = Counter()
    total = 0
    disabled = 0
    password_only_unverified: list[str] = []
    linked_unverified: list[str] = []
    password_verified = 0
    for record in records:
        total += 1
        if record.disabled:
            disabled += 1
        providers = _providers(record)
        by_provider["+".join(providers) or "(none)"] += 1
        if PASSWORD not in providers:
            continue
        if record.email_verified:
            password_verified += 1
        elif providers == [PASSWORD]:
            password_only_unverified.append(record.uid)
        else:
            linked_unverified.append(record.uid)
    return {
        "total": total,
        "disabled": disabled,
        "by_provider": dict(sorted(by_provider.items())),
        "password_verified": password_verified,
        "password_only_unverified": sorted(password_only_unverified),
        "linked_unverified": sorted(linked_unverified),
    }


def render(report: dict, *, list_uids: bool) -> list[str]:
    """The report as printable lines: counts, and uids only on request."""
    lines = [
        f"total users:                    {report['total']}",
        f"disabled:                       {report['disabled']}",
        "by provider:",
        *(f"  {name:<28}  {n}" for name, n in report["by_provider"].items()),
        f"password, verified:             {report['password_verified']}",
        f"password only, UNVERIFIED:      {len(report['password_only_unverified'])}"
        "   <- will see the verify screen",
        f"password + other, UNVERIFIED:   {len(report['linked_unverified'])}"
        "   <- verify screen only when signing in with the password",
    ]
    if list_uids:
        lines.append("uids, password only, unverified:")
        lines.extend(f"  {uid}" for uid in report["password_only_unverified"])
        lines.append("uids, password + other, unverified:")
        lines.extend(f"  {uid}" for uid in report["linked_unverified"])
    return lines


def _firebase_auth(project: str):
    """``api.deps.firebase_auth()``, with the Admin app pinned to ``project``.

    Initialising here first, with an explicit ``projectId``, means the deps
    helper reuses this app rather than inferring a project from ADC.
    """
    import firebase_admin

    if not firebase_admin._apps:
        firebase_admin.initialize_app(options={"projectId": project})
    app_project = firebase_admin.get_app().project_id
    assert app_project == project, (
        f"Firebase Admin is bound to {app_project!r}, not {project!r}"
    )
    from api.deps import firebase_auth

    return firebase_auth()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--list-uids",
        action="store_true",
        help="also print the uids of unverified password accounts (never emails)",
    )
    args = parser.parse_args(argv)

    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    assert project, "GOOGLE_CLOUD_PROJECT is unset; cannot pin the GCP project."

    fb_auth = _firebase_auth(project)
    report = audit(fb_auth.list_users().iterate_all())
    print(f"project: {project}")
    for line in render(report, list_uids=args.list_uids):
        print(line)


if __name__ == "__main__":
    main()
