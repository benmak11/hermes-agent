# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Usage:
    python -m cli.run_discovery --user-id me
"""

import argparse
import asyncio
from datetime import UTC, datetime

from dotenv import load_dotenv
from google.cloud import firestore

from obs.logging import bind_run_context
from tools.discovery.pipeline import persist_new_jobs, run_discovery
from tools.discovery.title_filter import load_job_preferences, prefilter_jobs
from tools.run_costs import persist_run_cost

# Load GOOGLE_CLOUD_PROJECT (and friends) so the Firestore client targets the
# right project.
load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    run_id = bind_run_context("discovery", user_id=args.user_id)
    started_at = datetime.now(UTC).isoformat()

    try:
        print("→ Running discovery...")
        summary = await run_discovery(args.user_id)
        jobs = summary["jobs"]
        print(f"  Fetched {len(jobs)} total jobs")
        print(
            f"  Failures: {len(summary['failures'])},"
            f" Empty boards: {len(summary['empty_boards'])}"
        )

        preferences = await load_job_preferences(args.user_id)
        jobs, dropped = prefilter_jobs(jobs, preferences)
        if dropped:
            print(
                f"  Title pre-filter dropped {sum(dropped.values())}: {dict(dropped)}"
            )

        new = await persist_new_jobs(jobs)
        print(f"✓ {new} new jobs added to Firestore")
    finally:
        # In a finally: a run that dies partway through still spent whatever
        # it spent, and that only reaches the ledger from here.
        await persist_run_cost(
            firestore.AsyncClient,
            args.user_id,
            run_id,
            runner="discovery",
            started_at=started_at,
        )


if __name__ == "__main__":
    asyncio.run(main())
