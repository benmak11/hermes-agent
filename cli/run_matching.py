# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Score pending, unscored jobs against the user's profile and persist the result.

Usage:
    python -m cli.run_matching --user-id me [--limit N] [--concurrency K]
    python -m cli.run_matching --user-id me --batch [--poll-seconds S]
    python -m cli.run_matching --user-id me --batch-async
    python -m cli.run_matching --user-id me --batch-resume

``--batch`` runs the LLM legs as Vertex batch prediction jobs — half price on
both models, but async: expect minutes to hours before results land. Right
for big backlogs, wrong for "score what discovery just found".

``--batch-async`` is the fire-and-forget version: submit a resumable run
(tracked in the ``batch_runs`` collection) and exit; the hermes-worker's
hourly ticks poll and ingest it. ``--batch-resume`` runs one such
poll-and-ingest pass locally, for watching a run land without waiting on the
worker.
"""

import argparse
import asyncio

from dotenv import load_dotenv

from models.job import Job
from models.match import JobMatch
from obs.logging import bind_run_context
from tools.matching import batch_runs
from tools.matching.batch import batch_score_pending_jobs
from tools.matching.score import score_pending_jobs

load_dotenv()


def _print_result(job: Job, match: JobMatch | None, error: str | None) -> None:
    if match:
        print(
            f"  {match.overall_score:5.0f}  {match.recommendation:12}  "
            f"{job.company} - {job.title[:50]}"
        )
    else:
        print(f"  ✗ {job.company} - {job.title[:50]}: {error}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--limit", type=int, default=None, help="Max jobs to score this run"
    )
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Use Vertex batch prediction: 50%% cheaper, async turnaround",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="Batch mode: how often to poll the batch job state",
    )
    parser.add_argument(
        "--batch-async",
        action="store_true",
        help="Submit a resumable batch run and exit; worker ticks ingest it",
    )
    parser.add_argument(
        "--batch-resume",
        action="store_true",
        help="One resume pass over in-flight batch runs, then exit",
    )
    args = parser.parse_args()
    bind_run_context("matching", user_id=args.user_id)

    if args.batch_resume:
        summary = await batch_runs.resume(user_id=args.user_id)
        print(
            f"✓ Checked {summary['checked']} run(s): {summary['running']} still "
            f"running, {summary['advanced']} advanced to scoring, "
            f"{summary['completed']} completed, {summary['failed']} failed"
        )
        return

    if args.batch_async:
        result = await batch_runs.start(args.user_id, limit=args.limit)
        counts = result["counts"]
        print(
            f"✓ Batch run {result['run']} submitted at stage "
            f"{result['stage']!r} for {result['pending']} pending job(s)"
            f" ({counts['discarded']} tombstoned pre-submit)."
        )
        print(
            "  The worker's hourly ticks will ingest it; or run "
            "--batch-resume to poll now."
        )
        return

    try:
        if args.batch:
            print("→ Scoring unscored pending jobs via batch prediction...")
            counts = await batch_score_pending_jobs(
                args.user_id,
                limit=args.limit,
                poll_seconds=args.poll_seconds,
                on_result=_print_result,
            )
        else:
            print(
                f"→ Scoring unscored pending jobs (concurrency={args.concurrency})..."
            )
            counts = await score_pending_jobs(
                args.user_id,
                limit=args.limit,
                concurrency=args.concurrency,
                on_result=_print_result,
            )
    except ValueError as e:
        # Only the missing-profile ValueError gets the friendly exit;
        # JSONDecodeError is also a ValueError and must surface as itself
        # (a batch run once died mid-poll and was misreported as "no
        # profile" by this handler).
        if "No profile" not in str(e):
            raise
        raise SystemExit(f"{e} Run `cli.sync_profile` first.") from None
    print(
        f"✓ Scored {counts['scored']}, discarded {counts['discarded']},"
        f" failed {counts['failed']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
