# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Run the application reaper by hand: recover applications whose worker died.

The scheduled pass lives on the cron tick (``api/routes/discovery.cron_tick``);
this is the operator's copy of it, for legacy documents, for a user whose tick
is not running, and for seeing what the hourly job *would* do before it does it.

Dry-run by default, like ``cli.reset_user``, ``cli.geo_resurrect`` and
``cli.unwedge_submitting``: without ``--execute`` it classifies every in-progress
application and reports the verdicts, taking no lease, writing nothing and
dispatching nothing. (``cli.purge_discarded`` is the exception in this
directory; do not copy it.)

**This is not ``cli.unwedge_submitting``, and it does not replace it.** The two
divide ``submitting`` between them along the line ``state.IN_PROGRESS`` draws:
this tool acts only on documents holding an *expired lease*, which is first-hand
evidence that a run started and stopped. A ``submitting`` document with **no**
lease is ambiguous — the window between the submit request writing the status
and the run claiming it is real — so it is reported here as ``ambiguous`` and
left for ``unwedge_submitting``'s age arithmetic, which is a judgement call an
operator should make rather than an hourly job.

What it never does: re-submit. A ``submitting`` document that died with
``submit_attempted_at`` set is failed and flagged ``submission_uncertain``, never
re-enqueued, because the browser may already have filed a real application.

Usage:
    python -m cli.reap_applications --user-id me                    # dry run
    python -m cli.reap_applications --user-id me --execute
    python -m cli.reap_applications --user-id me --app-id app-abc123 --execute
    python -m cli.reap_applications --user-id me --max-attempts 1
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from dotenv import load_dotenv
from google.cloud import firestore

from api.routes.applications import dispatch_tailor
from obs.logging import bind_run_context, get_logger
from tools import queues
from tools.applications import reaper, state

load_dotenv()

log = get_logger("cli.reap_applications")

#: The order verdicts are printed in: what moved, then what didn't.
REPORT_KEYS = (
    "redispatch",
    "requeue",
    "give_up",
    "release_unstarted",
    "release_uncertain",
    "ambiguous",
    "alive",
    "lost_race",
    "not_dispatched",
    "errors",
    "truncated",
)


def dispatch(user_id: str, job_id: str) -> bool:
    """Re-dispatch tailoring, queue only.

    A CLI has no request to hang a FastAPI background task on, so with
    ``QUEUE_MODE`` off there is nowhere for the work to run and
    ``dispatch_tailor`` says so by returning False rather than dropping it on
    the floor. The recovery still happened either way — the document is back in
    ``queued`` — so the next cron tick or the user's own Regenerate picks it up.
    """
    return dispatch_tailor(user_id, job_id, background_tasks=None)


def main() -> None:
    # Synchronous for the same reason cli.unwedge_submitting is: every write
    # goes through tools.applications.state, which is synchronous on purpose,
    # and this tool must not become a second writer of ``status``.
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually recover. Without this flag, only reports the verdicts.",
    )
    parser.add_argument(
        "--app-id",
        default=None,
        help="Inspect only this application instead of scanning the user's.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=reaper.MAX_ATTEMPTS,
        help=(
            "Automatic recoveries allowed before an application is failed for "
            f"the user to retry by hand (default {reaper.MAX_ATTEMPTS})."
        ),
    )
    args = parser.parse_args()
    bind_run_context("reap_applications", user_id=args.user_id)

    db = firestore.Client()
    now = datetime.now(UTC)
    if not args.execute:
        print("  dry run — nothing will be written (pass --execute to act)")
    if not queues.enabled():
        print("  note: QUEUE_MODE is off, so re-dispatch cannot be scheduled here")

    if args.app_id:
        tally = _reap_one_by_id(db, args, now)
    else:
        tally = reaper.reap_applications(
            args.user_id,
            dispatch=dispatch,
            db=db,
            now=now,
            execute=args.execute,
            max_attempts=args.max_attempts,
        )

    verb = "recovered" if args.execute else "would recover"
    print(
        f"✓ {verb} {tally['recovered']} of {tally['scanned']} in-progress "
        "application(s)"
    )
    for key in REPORT_KEYS:
        if tally.get(key):
            print(f"  {key}: {tally[key]}")
    if tally.get("release_uncertain"):
        print(
            "  NOTE: an uncertain submission may already have reached the "
            "employer — the user should check their email before retrying it."
        )
    if tally.get("truncated"):
        print(
            f"  NOTE: stopped at the {reaper.MAX_PER_PASS}-document budget — "
            "there is more to do; run again."
        )
    if tally.get("ambiguous"):
        print(
            "  NOTE: 'ambiguous' means submitting with no lease, which is not "
            "the same as dead — use cli.unwedge_submitting to adjudicate those."
        )
    log.info("reap_applications.done", execute=args.execute, **tally)


def _reap_one_by_id(db, args, now: datetime) -> dict[str, int]:
    """The ``--app-id`` path: the same classify-then-act, on one document.

    Deliberately shares :func:`reaper.classify` and :func:`reaper.reap_one`
    rather than re-deriving anything — a targeted operator run must not be able
    to reach a verdict the hourly pass would not, least of all on the apply
    fork.
    """
    ref = (
        db.collection("users")
        .document(args.user_id)
        .collection("applications")
        .document(args.app_id)
    )
    snap = ref.get()
    tally = dict.fromkeys(("scanned", "recovered"), 0)
    if not snap.exists:
        print(f"  ! {args.app_id}: no such application")
        return tally

    doc = snap.to_dict() or {}
    tally["scanned"] = 1
    verdict = reaper.classify(doc, now=now, max_attempts=args.max_attempts)
    print(f"  {verdict}  {args.app_id}  (status={doc.get(state.STATUS_FIELD)!r})")
    if verdict in ("alive", "ambiguous"):
        tally[verdict] = 1
        return tally
    if not args.execute:
        tally[verdict] = 1
        tally["recovered"] = 1
        return tally

    outcome = reaper.reap_one(
        ref,
        snap,
        doc,
        verdict,
        user_id=args.user_id,
        dispatch=dispatch,
        now=now,
    )
    tally[outcome] = 1
    tally["recovered"] = int(outcome not in ("lost_race", "not_dispatched"))
    return tally


if __name__ == "__main__":
    main()
