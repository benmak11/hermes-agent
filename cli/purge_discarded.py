# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
One-off cleanup: move already-scored pending jobs at/below the discard
threshold out of `jobs` and into `discarded_jobs` tombstones. Newly scored
jobs are discarded inline by `score_pending_jobs`; this backfills the rule
for docs persisted before it existed. Only touches `user_decision ==
"pending"` docs — anything the user acted on is left alone.

Usage:
    python -m cli.purge_discarded --user-id me [--dry-run]
"""

import argparse
import asyncio

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.match import JobMatch
from obs.logging import bind_run_context, get_logger
from tools.matching.score import discard_tombstone, should_discard

load_dotenv()

log = get_logger("cli.purge_discarded")


def backfill_tombstone(job: Job, match: JobMatch, doc: dict) -> dict:
    """The tombstone for a job this purge is moving, carrying the doc's own
    history rather than this run's.

    Both fields come off the job document because **this purge spends
    nothing**. It makes no model call and takes no budget reservation, so
    anything it stamps from the ambient context describes the purge, not the
    score.

    - ``scored_run_id``: stamping this run's would misattribute the tombstone,
      and tombstones are where most of a cycle's spend lands.
    - ``scored_with``: the job was scored by a real model under a real prompt,
      and the record of that is sitting right here in ``doc``. Dropping it
      would be worse than never having had it: ``discard_tombstone`` treats an
      absent ``scored_with`` as "no scoring model ran" (that is what
      ``batch_runs._persist_prefiltered`` means by it), so a Pro-scored job
      demoted by a threshold change would land in ``discarded_jobs`` asserting
      the exact opposite of the truth. ``None`` for a job scored before the
      field existed, which is the honest answer and the one the key already
      has everywhere else.
    """
    return discard_tombstone(
        job,
        match,
        scored_run_id=doc.get("scored_run_id"),
        provenance=doc.get("scored_with"),
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would move, change nothing"
    )
    args = parser.parse_args()
    bind_run_context("purge_discarded", user_id=args.user_id)

    db = firestore.AsyncClient()
    user_ref = db.collection("users").document(args.user_id)
    query = user_ref.collection("jobs").where(
        filter=FieldFilter("user_decision", "==", "pending")
    )

    kept = moved = unscored = 0
    async for snap in query.stream():
        d = snap.to_dict()
        if "match" not in d:
            unscored += 1
            continue
        match = JobMatch.model_validate(d["match"])
        if not should_discard(match):
            kept += 1
            continue
        job = Job.model_validate(d)
        print(
            f"  discard  {match.overall_score:5.0f}  {job.company} - {job.title[:50]}"
        )
        if not args.dry_run:
            await (
                user_ref.collection("discarded_jobs")
                .document(job.id)
                .set(backfill_tombstone(job, match, d))
            )
            await snap.reference.delete()
        moved += 1

    verb = "would move" if args.dry_run else "moved"
    print(f"✓ {verb} {moved} to discarded_jobs; kept {kept}, unscored {unscored}")
    log.info(
        "purge_discarded.done",
        moved=moved,
        kept=kept,
        unscored=unscored,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    asyncio.run(main())
