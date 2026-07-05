# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Tailor approved jobs into ready-for-review Applications and persist them.

Tailors every approved job that does not yet have an Application, or a single
job with --job-id.

Usage:
    python -m cli.run_tailoring --user-id me [--job-id <id>] [--limit N] [--no-upload]
"""

import argparse
import asyncio

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models.job import Job
from models.profile import MasterProfile
from obs.logging import bind_run_context, get_logger
from tools.tailoring.pipeline import application_id, tailor_application

load_dotenv()
log = get_logger("cli.run_tailoring")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--job-id", default=None, help="Tailor just this job")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--no-upload", action="store_true", help="Render locally, skip GCS upload"
    )
    args = parser.parse_args()
    bind_run_context("tailoring", user_id=args.user_id)

    db = firestore.AsyncClient()
    user_ref = db.collection("users").document(args.user_id)

    profile_doc = await user_ref.get()
    if not profile_doc.exists:
        raise SystemExit(f"No profile at users/{args.user_id}. Run cli.sync_profile.")
    profile = MasterProfile.model_validate(profile_doc.to_dict())

    jobs_ref = user_ref.collection("jobs")
    apps_ref = user_ref.collection("applications")

    targets: list[Job] = []
    if args.job_id:
        snap = await jobs_ref.document(args.job_id).get()
        if not snap.exists:
            raise SystemExit(f"No job {args.job_id} for users/{args.user_id}.")
        targets.append(Job.model_validate(snap.to_dict()))
    else:
        query = jobs_ref.where(filter=FieldFilter("user_decision", "==", "approved"))
        async for snap in query.stream():
            job = Job.model_validate(snap.to_dict())
            # Skip jobs that already have an application.
            existing = await apps_ref.document(application_id(job.id)).get()
            if existing.exists:
                continue
            targets.append(job)
            if args.limit and len(targets) >= args.limit:
                break

    print(f"→ Tailoring {len(targets)} approved job(s)...")
    counts = {"ok": 0, "failed": 0}
    for job in targets:
        try:
            app = await tailor_application(job, profile, upload=not args.no_upload)
            await apps_ref.document(app.id).set(app.model_dump(mode="json"))
            counts["ok"] += 1
            print(f"  ✓ {job.company} - {job.title[:50]} -> {app.resume_variant_uri}")
        except Exception as e:  # report and continue with the next job
            counts["failed"] += 1
            log.exception("tailor.failed", job_id=job.id, company=job.company)
            print(f"  ✗ {job.company} - {job.title[:50]}: {e}")

    print(f"✓ Tailored {counts['ok']}, failed {counts['failed']}")


if __name__ == "__main__":
    asyncio.run(main())
