# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Usage:
    python -m cli.run_discovery --user-id me
"""

import argparse
import asyncio

from dotenv import load_dotenv

from obs.logging import bind_run_context
from tools.discovery.pipeline import persist_new_jobs, run_discovery

# Load GOOGLE_CLOUD_PROJECT (and friends) so the Firestore client targets the
# right project.
load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    bind_run_context("discovery", user_id=args.user_id)

    print("→ Running discovery...")
    summary = await run_discovery(args.user_id)
    jobs = summary["jobs"]
    print(f"  Fetched {len(jobs)} total jobs")
    print(
        f"  Failures: {len(summary['failures'])},"
        f" Empty boards: {len(summary['empty_boards'])}"
    )

    new = await persist_new_jobs(jobs)
    print(f"✓ {new} new jobs added to Firestore")


if __name__ == "__main__":
    asyncio.run(main())
