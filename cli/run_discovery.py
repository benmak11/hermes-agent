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
from tools.run_costs import DONE, FAILED, RUNNING, open_run, persist_run_cost

# Load GOOGLE_CLOUD_PROJECT (and friends) so the Firestore client targets the
# right project.
load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    run_id = bind_run_context("discovery", user_id=args.user_id)
    started_at = datetime.now(UTC).isoformat()
    # ``running`` until a leg below decides. A CLI run killed with Ctrl-C or
    # SIGKILL leaves the doc open, and the activity contract ages it into
    # ``stalled`` rather than reporting nothing at all.
    ledger_state = RUNNING
    await open_run(
        firestore.AsyncClient,
        args.user_id,
        run_id,
        runner="discovery",
        trigger="cli",
        started_at=started_at,
    )

    try:
        print("→ Running discovery...")
        summary = await run_discovery(args.user_id)
        jobs = summary["jobs"]
        print(f"  Fetched {len(jobs)} total jobs")
        print(
            f"  Failures: {len(summary['failures'])},"
            f" Empty boards: {len(summary['empty_boards'])}"
        )
        print(
            f"  Boards fetched: {summary['boards_fetched']},"
            f" served from cache: {summary['boards_cached']}"
        )
        print(
            f"  Boards not found (404): {summary['boards_not_found']},"
            f" failing: {summary['boards_failing']}"
        )
        print(
            f"  Dead boards skipped: {summary['boards_skipped_dead']},"
            f" rerouted: {summary['boards_rerouted']},"
            f" would reroute: {summary['boards_would_reroute']}"
        )

        preferences = await load_job_preferences(args.user_id)
        jobs, dropped = prefilter_jobs(jobs, preferences)
        if dropped:
            print(
                f"  Title pre-filter dropped {sum(dropped.values())}: {dict(dropped)}"
            )

        new = await persist_new_jobs(jobs)
        print(f"✓ {new} new jobs added to Firestore")
        ledger_state = DONE
    except Exception:
        # Re-raised untouched; this clause only records the outcome.
        ledger_state = FAILED
        raise
    finally:
        # In a finally: a run that dies partway still spent what it spent, and
        # that reaches the ledger only from here.
        await persist_run_cost(
            firestore.AsyncClient,
            args.user_id,
            run_id,
            runner="discovery",
            state=ledger_state,
        )


if __name__ == "__main__":
    asyncio.run(main())
