# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Score pending, unscored jobs against the user's profile and persist the result.

Usage:
    python -m cli.run_matching --user-id me [--limit N] [--concurrency K]
"""

import argparse
import asyncio

from dotenv import load_dotenv

from models.job import Job
from models.match import JobMatch
from obs.logging import bind_run_context
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
    args = parser.parse_args()
    bind_run_context("matching", user_id=args.user_id)

    print(f"→ Scoring unscored pending jobs (concurrency={args.concurrency})...")
    try:
        counts = await score_pending_jobs(
            args.user_id,
            limit=args.limit,
            concurrency=args.concurrency,
            on_result=_print_result,
        )
    except ValueError as e:
        raise SystemExit(f"{e} Run `cli.sync_profile` first.") from None
    print(f"✓ Scored {counts['scored']}, failed {counts['failed']}")


if __name__ == "__main__":
    asyncio.run(main())
