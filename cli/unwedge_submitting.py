# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Move applications wedged in ``submitting`` to ``failed`` so the user can act.

``submitting`` is the one status with no automatic way out. ``run_submission``
writes ``failed`` from its own ``except``, but that never fires if the process
dies — and ``run_submission`` is a FastAPI ``BackgroundTask`` on ``hermes-api``,
so an ordinary Cloud Run eviction mid-submit strands the document there.
Everything else then refuses to touch it, correctly: ``submit`` and
``regenerate`` return 409, the undo path declines to delete it, and the liveness
sweep skips it (``tools.ats.sweep.ACTIVE_APP_STATUSES``). Before the state
machine, ``regenerate`` rewrote the status unconditionally and was the accidental
escape hatch; closing that hole is what makes this tool necessary.

**This is a manual stopgap, not the reaper.** The real fix is a scheduled pass
that expires ``state.IN_PROGRESS`` leases; that is a later PR. This is the
operator lever for the interim, and it is deliberately a CLI rather than an
endpoint: a *user-facing* way out of ``submitting`` is exactly what risks a
duplicate real job application, which is the thing the compare-and-swap exists
to prevent.

**It never retries the submission.** All it does is unwedge the document. The
note it writes says the outcome is unknown, because it is: the browser may have
clicked Submit successfully a millisecond before the process died. The user must
check their email before resubmitting, and the note tells them so.

Dry-run by default, like ``cli.reset_user`` and ``cli.geo_resurrect``: without
``--execute`` it reports exactly what it would move and writes nothing.

**A live lease is never released, at any age.** ``tools.applications.state``
leases are written by the process actually running the work, so they are the
one first-hand signal here; everything else on this page is inference. The age
arithmetic below only decides documents that hold no lease — the in-process
submission path writes none, and neither did anything before the lease existed.

Age is measured from ``last_submitted_at`` (written atomically with the
``submitting`` status by ``POST /applications/{id}/submit``), falling back to the
newest timeline entry for documents predating it. The default floor is the
``submitting`` lease from ``tools.applications.state.IN_PROGRESS`` — which is
deliberately *longer* than the 1800s Cloud Tasks dispatch deadline, since a lock
that expires while its work can still be running is not a lock — so a submission
still legitimately in flight is never touched.

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
    status, so it is the authoritative answer. The timeline fallback covers
    documents written before that field existed.
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

    **A held lease wins over the age arithmetic, whatever the age says.** The
    ages here are inferred: ``last_submitted_at`` records when the *request*
    claimed the status, which is not when the run started — once submission goes
    through the queue, a task can sit enqueued for minutes before a worker picks
    it up, so an application can be older than the floor while its browser is
    still on the first page of the form. Releasing that one writes ``failed``
    and clears the lease under a working submitter, and the real outcome is then
    refused when it reports back: the user is told "failed" about an application
    that went out, with the confirmation screenshot dropped. The lease is the
    only signal that comes from the process actually doing the work, so it is
    the one that decides.

    A document whose age can't be determined *and* holds no lease is treated as
    wedged: it has no ``last_submitted_at`` and no usable timeline, which only
    happens to documents old enough that no submission of theirs is still
    running.
    """
    if doc.get("status") != WEDGED_STATUS:
        return False
    if state.lease_is_held(doc, now=now):
        return False
    age = age_minutes(doc, now=now)
    return age is None or age >= min_age_minutes


def main() -> None:
    # Synchronous, unlike the other CLIs: ``state.try_transition`` is sync
    # (every production caller holds a sync ``firestore.Client``), and going
    # through it is non-negotiable — it is the only writer of ``status``, and
    # this tool must not become a second one.
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
            # allowed_from re-checks inside the swap: if the real submission
            # was merely slow and reported back between the read above and this
            # write, it wins and this releases nothing.
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
