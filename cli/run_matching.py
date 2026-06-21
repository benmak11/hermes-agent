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
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.profile import MasterProfile
from tools.matching.pipeline import match_job

load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--limit", type=int, default=None, help="Max jobs to score this run"
    )
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    db = firestore.AsyncClient()

    profile_doc = await db.collection("users").document(args.user_id).get()
    if not profile_doc.exists:
        raise SystemExit(
            f"No profile at users/{args.user_id}. Run `cli.sync_profile` first."
        )
    profile = MasterProfile.model_validate(profile_doc.to_dict())

    jobs_ref = db.collection("users").document(args.user_id).collection("jobs")
    query = jobs_ref.where(filter=FieldFilter("user_decision", "==", "pending"))

    pending: list[tuple] = []
    async for snap in query.stream():
        d = snap.to_dict()
        if "match" in d:  # already scored
            continue
        pending.append((snap.reference, Job.model_validate(d)))
        if args.limit and len(pending) >= args.limit:
            break

    print(
        f"→ Scoring {len(pending)} unscored pending jobs "
        f"(concurrency={args.concurrency})..."
    )
    sem = asyncio.Semaphore(args.concurrency)
    counts = {"scored": 0, "failed": 0}

    async def _score(ref, job: Job) -> None:
        async with sem:
            try:
                match = await match_job(job, profile)
                await ref.update(
                    {
                        "match": match.model_dump(mode="json"),
                        "jd_parsed": (
                            job.jd_parsed.model_dump(mode="json")
                            if job.jd_parsed
                            else None
                        ),
                    }
                )
                counts["scored"] += 1
                print(
                    f"  {match.overall_score:5.0f}  {match.recommendation:12}  "
                    f"{job.company} - {job.title[:50]}"
                )
            except Exception as e:
                counts["failed"] += 1
                print(f"  ✗ {job.company} - {job.title[:50]}: {e}")

    await asyncio.gather(*(_score(ref, job) for ref, job in pending))
    print(f"✓ Scored {counts['scored']}, failed {counts['failed']}")


if __name__ == "__main__":
    asyncio.run(main())
