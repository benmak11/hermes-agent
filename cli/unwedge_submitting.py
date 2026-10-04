# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Move applications wedged in ``submitting`` to ``failed`` so the user can act.

``submitting`` is the one status with no automatic way out: ``run_submission``
writes ``failed`` from its own ``except``, which never fires if the process
dies, and every other path then refuses to touch the document. This is the
manual operator lever for that case. It is a CLI rather than an endpoint on
purpose — a user-facing escape from ``submitting`` is what risks a duplicate
real job application.

It never retries the submission; it only unwedges the document, and the note it
writes says the outcome is unknown, because the browser may have clicked Submit
a millisecond before the process died.

Dry-run by default: without ``--execute`` it reports what it would move and
writes nothing. With ``--execute`` it writes to Firestore through
``state.try_transition``.

A held ``tools.applications.state`` lease is never released, at any age — the
lease is written by the process actually doing the work, so the age arithmetic
below only decides documents that hold none. Age comes from
``last_submitted_at``, falling back to the newest timeline entry, and the
default floor is the ``submitting`` lease, which is deliberately longer than the
1800s Cloud Tasks dispatch deadline.

Usage:
    python -m cli.unwedge_submitting --user-id me                    # dry run
    python -m cli.unwedge_submitting --user-id me --execute
    python -m cli.unwedge_submitting --user-id me --app-id app-abc123 --execute
    python -m cli.unwedge_submitting --user-id me --older-than-minutes 60
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from obs.logging import bind_run_context, get_logger
from tools.applications import state

load_dotenv()

log = get_logger("cli.unwedge_submitting")

WEDGED_STATUS = "submitting"

#: Minutes a document must have sat in ``submitting`` before it counts as
#: wedged. Derived from the lease the state machine already defines for that
#: status, so the two can't drift.
DEFAULT_MIN_AGE_MINUTES = state.IN_PROGRESS[WEDGED_STATUS] // 60

NOTE = (
    "submission interrupted — the worker stopped without reporting an outcome. "
    "It is UNKNOWN whether this application was actually submitted; check your "
    "email for a confirmation from the employer before submitting again. "
    "Released by cli.unwedge_submitting."
)


def _parse_iso(value) -> datetime | None:
    """A stored ISO timestamp as an aware datetime, or ``None`` if unusable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def started_at(doc: dict) -> datetime | None:
    """When this submission began, or ``None`` if the document doesn't say.

    ``last_submitted_at`` is written in the same update as the ``submitting``
    status, so it is authoritative; the timeline fallback covers documents
    written before that field existed.
    """
    when = _parse_iso(doc.get("last_submitted_at"))
    if when is not None:
        return when
    stamps = [
        _parse_iso(event.get("at"))
        for event in (doc.get("timeline") or [])
        if isinstance(event, dict)
    ]
    usable = [s for s in stamps if s is not None]
    return max(usable) if usable else None


def age_minutes(doc: dict, *, now: datetime) -> float | None:
    """How long this document has been submitting, or ``None`` if unknown."""
    when = started_at(doc)
    if when is None:
        return None
    return (now - when).total_seconds() / 60


def is_wedged(doc: dict, *, now: datetime, min_age_minutes: float) -> bool:
    """Is this document stuck in ``submitting`` past the lease? Pure.

    A held lease wins over the age arithmetic whatever the age says: ages are
    inferred from when the *request* claimed the status, and a queued task can
    sit enqueued for minutes, so releasing a leased document would report
    "failed" for an application that is still being submitted.

    A document whose age can't be determined and holds no lease counts as
    wedged — that only happens to documents too old for a run to be live.
    """
    if doc.get("status") != WEDGED_STATUS:
        return False
    if state.lease_is_held(doc, now=now):
        return False
    age = age_minutes(doc, now=now)
    return age is None or age >= min_age_minutes


def main() -> None:
    # Synchronous, unlike the other CLIs: ``state.try_transition`` is sync, and
    # it must stay the only writer of ``status``.
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually release. Without this flag, only reports what would move.",
    )
    parser.add_argument(
        "--app-id",
        default=None,
        help="Release only this application instead of scanning the user's.",
    )
    parser.add_argument(
        "--older-than-minutes",
        type=float,
        default=DEFAULT_MIN_AGE_MINUTES,
        help=(
            "Minimum time in 'submitting' before a document counts as wedged "
            f"(default {DEFAULT_MIN_AGE_MINUTES}, the state machine's lease for "
            "that status). Lowering this risks releasing a submission that is "
            "still running."
        ),
    )
    args = parser.parse_args()
    bind_run_context("unwedge_submitting", user_id=args.user_id)

    db = firestore.Client()
    apps_ref = db.collection("users").document(args.user_id).collection("applications")
    if args.app_id:
        refs = [apps_ref.document(args.app_id)]
    else:
        refs = [
            snap.reference
            for snap in apps_ref.where(
                filter=FieldFilter("status", "==", WEDGED_STATUS)
            ).stream()
        ]

    now = datetime.now(UTC)
    tally: Counter = Counter()
    for ref in refs:
        snap = ref.get()
        if not snap.exists:
            tally["missing"] += 1
            print(f"  ! {ref.id}: no such application")
            continue
        doc = snap.to_dict() or {}
        tally["submitting"] += doc.get("status") == WEDGED_STATUS

        if not is_wedged(doc, now=now, min_age_minutes=args.older_than_minutes):
            tally["skipped"] += 1
            if doc.get("status") != WEDGED_STATUS:
                print(f"  - {ref.id}: status is {doc.get('status')!r}, not wedged")
            else:
                age = age_minutes(doc, now=now)
                print(f"  - {ref.id}: only {age:.1f}m in submitting — still in flight")
            continue

        age = age_minutes(doc, now=now)
        stamp = "unknown age" if age is None else f"{age:.1f}m"
        print(
            f"  release  {ref.id}  ({stamp})  "
            f"{doc.get('job_company') or '?'} - {(doc.get('job_title') or '?')[:50]}"
        )
        if args.execute:
            # allowed_from re-checks inside the swap, so a slow submission that
            # reported back since the read above wins and nothing is released.
            if state.try_transition(
                ref,
                snap,
                "failed",
                note=NOTE,
                lease=state.CLEAR_LEASE,
                allowed_from={WEDGED_STATUS},
            ):
                tally["released"] += 1
            else:
                tally["lost_race"] += 1
                print(f"  ! {ref.id}: status changed underneath us — left alone")
        else:
            tally["released"] += 1

    verb = "released" if args.execute else "would release"
    print(
        f"✓ {verb} {tally['released']} of {tally['submitting']} application(s) "
        f"in '{WEDGED_STATUS}'"
    )
    for key in ("skipped", "lost_race", "missing"):
        if tally[key]:
            print(f"  {key}: {tally[key]}")
    if tally["released"] and args.execute:
        print(
            "  NOTE: whether these were actually submitted is unknown — the user "
            "should check their email before retrying any of them."
        )
    log.info("unwedge_submitting.done", execute=args.execute, **dict(tally))


if __name__ == "__main__":
    main()
