# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Score pending, unscored jobs against the user's profile and persist the result.

**This costs money**: every run drives real Gemini calls on the live project.
Say what a run will cost before starting one.

Usage:
    python -m cli.run_matching --user-id me [--limit N] [--concurrency K]
    python -m cli.run_matching --user-id me --batch [--poll-seconds S]
    python -m cli.run_matching --user-id me --batch-async
    python -m cli.run_matching --user-id me --batch-resume
    python -m cli.run_matching --user-id me --ignore-budget --limit 5000

Every path is capped by the per-user scoring budget
(``tools.matching.budget``), so a plain run scores at most one cycle's worth.
``--ignore-budget`` is the operator escape hatch for hand-scoring a backlog and
exists only here, never on the HTTP surface; it is not unbounded, since without
``--limit`` every mode is still capped at ``SCORE_LIMIT_CEILING`` (300).

``--batch`` runs the LLM legs as Vertex batch prediction jobs: half price on
both models, but minutes to hours before results land. ``--batch-async``
submits a resumable run (tracked in ``batch_runs``) and exits, leaving the
worker's hourly ticks to poll and ingest it; ``--batch-resume`` runs one such
poll-and-ingest pass locally.
"""

import argparse
import asyncio
from datetime import UTC, datetime

from dotenv import load_dotenv
from google.cloud import firestore

from models.job import Job
from models.match import JobMatch
from obs.logging import bind_run_context
from tools.matching import batch_runs
from tools.matching.batch import batch_score_pending_jobs
from tools.matching.score import score_pending_jobs
from tools.run_costs import DONE, FAILED, RUNNING, open_run, persist_run_cost

load_dotenv()


def _print_result(job: Job, match: JobMatch | None, error: str | None) -> None:
    if match:
        print(
            f"  {match.overall_score:5.0f}  {match.recommendation:12}  "
            f"{job.company} - {job.title[:50]}"
        )
    else:
        print(f"  ✗ {job.company} - {job.title[:50]}: {error}")


async def _score(args: argparse.Namespace) -> None:
    """Do whatever the flags asked for.

    Split out of ``main`` so the cost flush there wraps every path in one
    ``finally``, including the batch modes' early returns.
    """
    if args.batch_resume:
        summary = await batch_runs.resume(user_id=args.user_id)
        print(
            f"✓ Checked {summary['checked']} run(s): {summary['running']} still "
            f"running, {summary['advanced']} advanced to scoring, "
            f"{summary['completed']} completed, {summary['failed']} failed"
        )
        return

    if args.batch_async:
        result = await batch_runs.start(
            args.user_id, limit=args.limit, ignore_budget=args.ignore_budget
        )
        if not result.get("started"):
            # The only way here without --min-pending: the scoring budget for
            # this cycle/day is spent.
            print(
                "✓ Nothing submitted — the scoring budget is exhausted "
                f"(remaining this cycle: {result.get('budget_remaining_cycle')}, "
                f"today: {result.get('budget_remaining_day')}). Raise "
                "SCORING_BUDGET_PER_CYCLE / _PER_DAY, or pass --ignore-budget."
            )
            return
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
                ignore_budget=args.ignore_budget,
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
                ignore_budget=args.ignore_budget,
            )
    except ValueError as e:
        # Only the missing-profile ValueError gets the friendly exit;
        # JSONDecodeError is also a ValueError and must surface as itself.
        if "No profile" not in str(e):
            raise
        raise SystemExit(f"{e} Run `cli.sync_profile` first.") from None
    print(
        f"✓ Scored {counts['scored']}, discarded {counts['discarded']},"
        f" failed {counts['failed']}"
    )


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
    parser.add_argument(
        "--ignore-budget",
        action="store_true",
        help=(
            "Skip the per-user scoring budget (operator backlog runs only — "
            "this is real money). Still capped at 300 jobs unless --limit is "
            "given, on every mode"
        ),
    )
    args = parser.parse_args()
    run_id = bind_run_context("matching", user_id=args.user_id)
    started_at = datetime.now(UTC).isoformat()
    # ``running`` until a leg below decides. A CLI run killed with Ctrl-C or
    # SIGKILL leaves the doc open, and the activity contract ages it into
    # ``stalled`` rather than reporting nothing at all.
    ledger_state = RUNNING
    await open_run(
        firestore.AsyncClient,
        args.user_id,
        run_id,
        runner="matching",
        trigger="cli",
        started_at=started_at,
    )

    try:
        await _score(args)
        ledger_state = DONE
    except Exception:
        # Re-raised untouched; this clause only records the outcome.
        ledger_state = FAILED
        raise
    finally:
        # In a finally: a run killed mid-scoring still paid for every call it
        # got through, and that spend reaches the ledger only from here.
        await persist_run_cost(
            firestore.AsyncClient,
            args.user_id,
            run_id,
            runner="matching",
            state=ledger_state,
        )


if __name__ == "__main__":
    asyncio.run(main())
