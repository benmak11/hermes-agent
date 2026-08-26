# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Undo geo-gate tombstones the current gate no longer agrees with.

This is the other half of ``GEO_GATE_ENFORCE``. When the gate skips a Pro call
it writes a ``discarded_jobs`` tombstone, and ``discarded_jobs`` is not a log —
it is discovery's dedupe key (``tools/discovery/pipeline.py`` checks the
tombstone *before* the job doc). So an enforced skip does not cost the user one
job; it suppresses that posting on every future re-discovery, silently, forever.
The whole enforcement design rests on that being reversible, and this is the
thing that reverses it.

Dry-run by default, like ``cli.reset_user``: without ``--execute`` it reports
exactly what it would move and writes nothing.

**Free and offline.** ``score.restore_payload`` stores the complete ``Job`` —
``jd_raw``, ``jd_parsed``, and every identifying field — on each enforced
tombstone, so a ``geo.GATE_VERSION`` bump resolves by streaming those
tombstones, re-running the current gate over the stored parse, and resurrecting
exactly the ones whose verdict changed. No re-parse, no LLM call, no dependence
on the posting still being live on a board months later, and the whole thing is
deterministic enough to unit-test.

Selects only ``geo_gate.enforced == true``. Everything else in that collection
is a *Pro* decision — reversing one of those would mean re-running the call, not
reading a stored copy — and the ``OUT_OF_FAMILY`` tombstones sitting beside
these share their score of 0, which is exactly why the flag and not the score is
the selector.

**This is not ``cli.purge_discarded``.** Same collection, opposite direction,
different inputs: that one moves scored jobs *into* tombstones from the `jobs`
collection; this one moves gate-rejected postings back out. Merging them would
put a restore path and a discard path behind one set of flags.

Usage:
    python -m cli.geo_resurrect --user-id me                        # dry run
    python -m cli.geo_resurrect --user-id me --execute
    python -m cli.geo_resurrect --user-id me --below-version 2 --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from typing import Literal

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.profile import MasterProfile
from obs.logging import bind_run_context, get_logger
from tools.matching import geo

load_dotenv()

log = get_logger("cli.geo_resurrect")


async def resurrect_one(user_ref, job: Job) -> None:
    """Put the job doc back, then drop the tombstone. **Order is load-bearing.**

    Discovery checks the tombstone before the job doc, so the two orderings fail
    very differently:

    - job first, then tombstone — a crash in between leaves the posting
      *suppressed but present*: the scorer picks the job doc up on its next run,
      and discovery still sees the tombstone so it never re-persists a duplicate.
      Nothing is lost and nothing is doubled; the next pass of this tool deletes
      the stranded tombstone.
    - tombstone first, then job — a crash in between, or merely a discovery
      cycle running concurrently, leaves a window in which the posting is neither
      tombstoned nor present, so discovery re-persists a *stale* copy fetched
      from the board and this tool's ``set`` then races it.

    So: write, then delete. Never the reverse.
    """
    await user_ref.collection("jobs").document(job.id).set(job.model_dump(mode="json"))
    await user_ref.collection("discarded_jobs").document(job.id).delete()


def _restored_job(doc: dict) -> Job | None:
    """The ``Job`` a tombstone's ``restore`` payload rebuilds, or ``None``.

    ``None`` covers tombstones written before ``restore`` existed and any whose
    payload no longer satisfies the current ``models.job.Job`` — both are
    unresurrectable by this tool and both must be *reported*, never skipped
    quietly, because the posting stays suppressed either way.
    """
    restore = doc.get("restore")
    if not isinstance(restore, dict):
        return None
    try:
        return Job.model_validate(restore)
    except Exception:
        return None


#: What classifying one enforced tombstone can conclude. Every one of these is
#: counted and printed — a tombstone this tool declines to act on leaves a
#: posting suppressed, so silence is never the right report.
Outcome = Literal[
    "resurrect",
    "still_ineligible",
    "current_version",
    "unrestorable",
    "no_parse",
]


def classify(
    doc: dict, profile: MasterProfile, *, below_version: int | None
) -> tuple[Outcome, Job | None, geo.GeoDecision | None]:
    """Decide what to do with one enforced tombstone. Pure — no I/O, no clock.

    Everything that decides whether a posting comes back lives here so the whole
    matrix is unit-testable without Firestore, which matters more than usual: the
    failure mode of getting it wrong is invisible by construction (a job the user
    never sees, that no future run will surface either).
    """
    gate = doc.get("geo_gate") or {}
    if below_version is not None:
        try:
            version = int(gate.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= below_version:
            return "current_version", None, None

    job = _restored_job(doc)
    if job is None:
        return "unrestorable", None, None
    if job.jd_parsed is None:
        # The gate takes a parse, not raw text. Re-parsing would cost a Flash
        # call, which is exactly what this tool promises not to do.
        return "no_parse", job, None

    decision = geo.evaluate(job.jd_parsed, profile)
    if decision.verdict == "ineligible":
        # Still unreachable under the current gate: leave it exactly as it is.
        # Resurrecting it would buy the Pro call the gate exists to avoid, and
        # the next enforcing run would tombstone it straight back.
        return "still_ineligible", job, decision
    return "resurrect", job, decision


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually resurrect. Without this flag, only reports what would move.",
    )
    parser.add_argument(
        "--below-version",
        type=int,
        default=None,
        help=(
            "Only consider tombstones written by a gate older than this "
            f"(current geo.GATE_VERSION is {geo.GATE_VERSION}). Omit to "
            "re-evaluate every enforced tombstone."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Stop after this many resurrections. Each one re-opens a Pro call "
            "(~$0.016) competing for the next cycle's budget slots, so a "
            "thousand-job sweep is a real spend decision — make it deliberately."
        ),
    )
    args = parser.parse_args()
    bind_run_context("geo_resurrect", user_id=args.user_id)

    db = firestore.AsyncClient()
    user_ref = db.collection("users").document(args.user_id)
    snap = await user_ref.get()
    if not snap.exists:
        parser.error(f"no profile at users/{args.user_id}")
    profile = MasterProfile.model_validate(snap.to_dict())

    # The gate is re-run against the profile as it stands *now*, which is the
    # second thing that can change a verdict: a user who moves country makes
    # every one of these decisions stale without GATE_VERSION moving at all.
    residence = geo.normalize_country(
        profile.residence.country if profile.residence else None
    )
    print(f"  residence={residence}  gate v{geo.GATE_VERSION}")

    query = user_ref.collection("discarded_jobs").where(
        filter=FieldFilter("geo_gate.enforced", "==", True)
    )
    tally: Counter = Counter()
    async for tomb in query.stream():
        doc = tomb.to_dict() or {}
        gate = doc.get("geo_gate") or {}
        tally["enforced"] += 1

        outcome, job, decision = classify(
            doc, profile, below_version=args.below_version
        )
        if outcome in ("unrestorable", "no_parse"):
            tally[outcome] += 1
            print(f"  ! {tomb.id}: {outcome} — left tombstoned")
            continue
        if outcome != "resurrect":
            tally[outcome] += 1
            continue
        assert job is not None and decision is not None  # narrowed by "resurrect"

        if args.limit is not None and tally["resurrected"] >= args.limit:
            tally["over_limit"] += 1
            continue

        print(
            f"  resurrect  {gate.get('rule', '?')} → {decision.verdict}"
            f"/{decision.rule}  {job.company} - {job.title[:50]}"
        )
        if args.execute:
            await resurrect_one(user_ref, job)
        tally["resurrected"] += 1

    verb = "resurrected" if args.execute else "would resurrect"
    print(
        f"✓ {verb} {tally['resurrected']} of {tally['enforced']} enforced "
        f"tombstone(s); {tally['still_ineligible']} still ineligible"
    )
    for key in ("current_version", "over_limit", "unrestorable", "no_parse"):
        if tally[key]:
            print(f"  {key}: {tally[key]}")
    log.info("geo_resurrect.done", execute=args.execute, **dict(tally))


if __name__ == "__main__":
    asyncio.run(main())
