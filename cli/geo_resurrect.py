# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Undo geo-gate tombstones the current gate no longer agrees with.

The other half of ``GEO_GATE_ENFORCE``. ``discarded_jobs`` is not a log, it is
discovery's dedupe key (checked *before* the job doc), so an enforced skip
suppresses that posting on every future re-discovery. This is what reverses
that, and the enforcement design depends on it existing.

Dry-run by default: without ``--execute`` it reports what it would move and
writes nothing. With ``--execute`` it writes to Firestore.

Free and offline. ``score.restore_payload`` stores the complete ``Job`` on each
enforced tombstone, so a ``geo.GATE_VERSION`` bump resolves by re-running the
current gate over the stored parse — no re-parse, no LLM call, no dependence on
the posting still being live.

Selects only ``geo_gate.enforced == true``. Everything else in the collection
is a Pro decision, which reversing would mean re-running; the ``OUT_OF_FAMILY``
tombstones beside these share their score of 0, which is why the flag and not
the score is the selector.

Not ``cli.purge_discarded``, which moves scored jobs the other way, out of
``jobs`` and into tombstones.

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
    """Put the job doc back, then drop the tombstone. The order is load-bearing.

    Discovery checks the tombstone before the job doc. Writing the job first
    means a crash in between leaves the posting present but still suppressed,
    which the next pass cleans up. Deleting the tombstone first opens a window
    where the posting is neither tombstoned nor present, so a concurrent
    discovery cycle re-persists a stale copy and races this write.
    """
    await user_ref.collection("jobs").document(job.id).set(job.model_dump(mode="json"))
    await user_ref.collection("discarded_jobs").document(job.id).delete()


def _restored_job(doc: dict) -> Job | None:
    """The ``Job`` a tombstone's ``restore`` payload rebuilds, or ``None``.

    ``None`` covers tombstones written before ``restore`` existed and payloads
    the current ``models.job.Job`` no longer accepts. Both must be reported
    rather than skipped quietly, because the posting stays suppressed.
    """
    restore = doc.get("restore")
    if not isinstance(restore, dict):
        return None
    try:
        return Job.model_validate(restore)
    except Exception:
        return None


#: What classifying one enforced tombstone can conclude. Every one is counted
#: and printed: a tombstone left alone leaves a posting suppressed.
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

    Kept pure so the whole matrix is unit-testable without Firestore; getting
    it wrong is invisible by construction, since the cost is a job the user
    never sees and no future run surfaces.
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
        # The gate takes a parse, not raw text, and re-parsing would cost a
        # Flash call this tool promises not to make.
        return "no_parse", job, None

    decision = geo.evaluate(job.jd_parsed, profile)
    if decision.verdict == "ineligible":
        # Still unreachable under the current gate: resurrecting it would buy
        # the Pro call the gate exists to avoid, and the next run would
        # tombstone it again.
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

    # Re-run against the profile as it stands now: a user who moves country
    # makes every one of these decisions stale without GATE_VERSION moving.
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
