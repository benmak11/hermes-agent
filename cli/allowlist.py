# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Manage the sign-up allowlist — Phase 4 D1's machinery.

``tools.allowlist`` is the module this operates; read its docstring for the
document shape and the transactional seat cap. This CLI ships useful before
``ALLOWLIST_ENFORCED`` is ever turned on: seeding the allowlist with the three
real accounts is exactly the prerequisite Phase 4 D2 (a separate PR) needs
before it can flip that flag without locking anyone out.

**``add`` resolves the email from Firebase Admin, never from
``users/{uid}.email``** — the same reasoning ``tools.allowlist.is_allowed``
documents for itself: the profile field is résumé-extracted by onboarding and
is not guaranteed to be the address the account signs in with, where Firebase
Auth's own record is. Identify the account by ``--uid`` or by ``--email``;
either way, what gets written to the allowlist is ``fb_auth.get_user(...)``'s
own ``.email``, not the string you typed.

Dry-run by default, like ``cli.reset_user`` and ``cli.geo_resurrect``: without
``--execute``, ``add`` and ``revoke`` report what they would do and write
nothing. (``cli.purge_discarded`` is the one exception to this project's
convention — do not follow that one.)

Usage:
    python -m cli.allowlist add --uid U6WbOc8MjhBpKD3 --note "beta"             # dry run
    python -m cli.allowlist add --uid U6WbOc8MjhBpKD3 --note "beta" --execute
    python -m cli.allowlist add --email user@example.com --max-users 10 --execute
    python -m cli.allowlist list
    python -m cli.allowlist revoke --email user@example.com --execute
"""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv
from google.cloud import firestore

from obs.logging import bind_run_context, get_logger
from tools import allowlist

load_dotenv()

log = get_logger("cli.allowlist")


def _resolve_email(fb_auth, *, uid: str | None, email: str | None) -> str:
    """The Firebase Auth record's own email for ``uid`` or ``email``.

    Never ``users/{uid}.email`` — see the module docstring. Raises
    ``SystemExit`` (not caught) rather than returning a fabricated value: a
    lookup that fails or comes back with no address is a reason to stop, not
    to guess.
    """
    try:
        record = fb_auth.get_user(uid) if uid else fb_auth.get_user_by_email(email)
    except Exception as e:
        raise SystemExit(
            f"Firebase Auth lookup for {uid or email!r} failed: {e}"
        ) from e
    resolved = (record.email or "").strip()
    if not resolved:
        raise SystemExit(
            f"Firebase Auth record for {uid or email!r} has no email address"
        )
    return resolved


def _firebase_auth():
    """A minimal, lazy Firebase Admin init — this CLI runs standalone, outside
    the API process, so it cannot reuse ``api.deps.firebase_auth``."""
    import firebase_admin
    from firebase_admin import auth as fb_auth

    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    return fb_auth


async def _cmd_add(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    max_users = args.max_users
    if max_users is None:
        raw = os.getenv("MAX_USERS", "").strip()
        if not raw:
            parser.error("--max-users or the MAX_USERS env var is required")
        try:
            max_users = int(raw)
        except ValueError:
            parser.error(f"MAX_USERS is not an integer: {raw!r}")

    email = _resolve_email(_firebase_auth(), uid=args.uid, email=args.email)
    db = firestore.AsyncClient()

    if not args.execute:
        entries = await allowlist.list_entries(db)
        seats = sum(1 for e in entries if not e.get("revoked"))
        print(f"Would add {email!r} (seats in use: {seats} of {max_users})")
        print("\nDry run only — re-run with --execute to actually add.")
        return

    added = await allowlist.add(
        db, email, added_by=args.added_by, note=args.note, max_users=max_users
    )
    if added:
        print(f"Added {email!r} to the allowlist.")
    else:
        print(f"Did NOT add {email!r} — the seat cap ({max_users}) is full.")
    log.info("cli.allowlist_add", email=email, added=added, max_users=max_users)


async def _cmd_list(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    db = firestore.AsyncClient()
    entries = await allowlist.list_entries(db)
    entries.sort(key=lambda e: e.get("email", ""))
    active = [e for e in entries if not e.get("revoked")]
    revoked = [e for e in entries if e.get("revoked")]

    for e in active:
        print(
            f"  {e['email']:40s} added_by={e.get('added_by')!r} note={e.get('note')!r}"
        )
    for e in revoked:
        print(f"  {e['email']:40s} REVOKED by={e.get('revoked_by')!r}")

    print(f"\n{len(active)} active, {len(revoked)} revoked, {len(entries)} total.")


async def _cmd_revoke(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    email = _resolve_email(_firebase_auth(), uid=args.uid, email=args.email)
    db = firestore.AsyncClient()

    if not args.execute:
        allowed = await allowlist.is_allowed(db, email)
        print(f"Would revoke {email!r} (currently allowed: {allowed})")
        print("\nDry run only — re-run with --execute to actually revoke.")
        return

    revoked = await allowlist.revoke(db, email, revoked_by=args.revoked_by)
    if revoked:
        print(f"Revoked {email!r}.")
    else:
        print(f"{email!r} was not on the allowlist — nothing to revoke.")
    log.info("cli.allowlist_revoke", email=email, revoked=revoked)


def _identifier_args(sub: argparse.ArgumentParser) -> None:
    group = sub.add_mutually_exclusive_group(required=True)
    group.add_argument("--uid", help="Firebase Auth uid")
    group.add_argument("--email", help="Firebase Auth email (looked up by address)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Grant a seat, subject to the seat cap.")
    _identifier_args(add)
    add.add_argument("--note", default=None)
    add.add_argument(
        "--added-by", default=os.getenv("USER", "cli"), help="Recorded on the doc."
    )
    add.add_argument(
        "--max-users",
        type=int,
        default=None,
        help="Seat cap. Falls back to the MAX_USERS env var if omitted.",
    )
    add.add_argument(
        "--execute",
        action="store_true",
        help="Actually add. Without this flag, only reports what would happen.",
    )

    sub.add_parser("list", help="List every allowlist entry.")

    revoke = sub.add_parser("revoke", help="Free a seat.")
    _identifier_args(revoke)
    revoke.add_argument(
        "--revoked-by", default=os.getenv("USER", "cli"), help="Recorded on the doc."
    )
    revoke.add_argument(
        "--execute",
        action="store_true",
        help="Actually revoke. Without this flag, only reports what would happen.",
    )

    args = parser.parse_args()
    bind_run_context("allowlist_cli", command=args.command)

    handler = {"add": _cmd_add, "list": _cmd_list, "revoke": _cmd_revoke}[args.command]
    asyncio.run(handler(args, parser))


if __name__ == "__main__":
    main()
