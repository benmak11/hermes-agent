# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Score pending, unscored jobs against the user's profile and persist the result.

Usage:
    python -m cli.run_matching --user-id me [--limit N] [--concurrency K]
    python -m cli.run_matching --user-id me --batch [--poll-seconds S]

``--batch`` runs the LLM legs as Vertex batch prediction jobs — half price on
both models, but async: expect minutes to hours before results land. Right
for big backlogs, wrong for "score what discovery just found".
"""

import argparse
import asyncio

from dotenv import load_dotenv

from models.job import Job
from models.match import JobMatch
from obs.logging import bind_run_context
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
    args = parser.parse_args()
    bind_run_context("matching", user_id=args.user_id)

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
