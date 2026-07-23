# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Full data wipe for one user — for resetting a demo account to a clean slate.

Deletes: the `users/{uid}` doc and its `jobs`/`applications`/`discarded_jobs`
subcollections, that user's `batch_runs` docs (top-level collection, matched
by the `user_id` field), and their GCS resume/screenshot blobs under
`users/{uid}/` in the resumes bucket. Does NOT touch the Firebase Auth
account (they can log back in to an empty/onboarding state) or `jd_cache`
(shared, content-keyed, not user data).

Usage:
    python -m cli.reset_user --user-id S4nOcOgxTpMjAU6WbOc8MjhBpKD3       # dry run
    python -m cli.reset_user --user-id S4nOcOgxTpMjAU6WbOc8MjhBpKD3 --execute
"""

import argparse
import asyncio

from dotenv import load_dotenv
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from obs.logging import bind_run_context, get_logger
from tools.tailoring.render import resume_bucket_name

load_dotenv()

log = get_logger("cli.reset_user")

_WRITE_CHUNK = 500


async def _delete_subcollection(
    db: firestore.AsyncClient,
    coll: firestore.AsyncCollectionReference,
    *,
    execute: bool,
) -> int:
    refs = [snap.reference async for snap in coll.stream()]
    if execute:
        for start in range(0, len(refs), _WRITE_CHUNK):
            batch = db.batch()
            for ref in refs[start : start + _WRITE_CHUNK]:
                batch.delete(ref)
            await batch.commit()
    return len(refs)


async def _delete_batch_runs(
    db: firestore.AsyncClient, user_id: str, *, execute: bool
) -> int:
    query = db.collection("batch_runs").where(
        filter=FieldFilter("user_id", "==", user_id)
    )
    refs = [snap.reference async for snap in query.stream()]
    if execute:
        for start in range(0, len(refs), _WRITE_CHUNK):
            batch = db.batch()
            for ref in refs[start : start + _WRITE_CHUNK]:
                batch.delete(ref)
            await batch.commit()
    return len(refs)


def _delete_gcs_prefix(user_id: str, *, execute: bool) -> int:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(resume_bucket_name())
    blobs = list(bucket.list_blobs(prefix=f"users/{user_id}/"))
    if execute:
        for blob in blobs:
            blob.delete()
    return len(blobs)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete. Without this flag, only reports counts.",
    )
    args = parser.parse_args()
    bind_run_context("reset_user", user_id=args.user_id)

    db = firestore.AsyncClient()
    user_ref = db.collection("users").document(args.user_id)

    counts = {}
    for name in ("jobs", "applications", "discarded_jobs"):
        counts[name] = await _delete_subcollection(
            db, user_ref.collection(name), execute=args.execute
        )

    user_doc_existed = (await user_ref.get()).exists
    if args.execute and user_doc_existed:
        await user_ref.delete()

    counts["batch_runs"] = await _delete_batch_runs(
        db, args.user_id, execute=args.execute
    )
    counts["gcs_blobs"] = _delete_gcs_prefix(args.user_id, execute=args.execute)

    verb = "Deleted" if args.execute else "Would delete"
    print(f"{verb} for user {args.user_id}:")
    print(f"  jobs subcollection:           {counts['jobs']}")
    print(f"  applications subcollection:   {counts['applications']}")
    print(f"  discarded_jobs subcollection: {counts['discarded_jobs']}")
    print(
        f"  users/{{uid}} doc:              {'yes' if user_doc_existed else 'no (already absent)'}"
    )
    print(f"  batch_runs docs (user_id):    {counts['batch_runs']}")
    print(f"  GCS blobs (resumes bucket):   {counts['gcs_blobs']}")
    if not args.execute:
        print("\nDry run only — re-run with --execute to actually delete.")

    log.info(
        "reset_user.done",
        execute=args.execute,
        user_doc_existed=user_doc_existed,
        **counts,
    )


if __name__ == "__main__":
    asyncio.run(main())
