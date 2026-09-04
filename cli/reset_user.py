# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Full data wipe for one user — for resetting a demo account to a clean slate.

Deletes: the `users/{uid}` doc and its `jobs`/`applications`/`discarded_jobs`/
`runs` (per-run cost ledger) subcollections, that user's `batch_runs` docs
(top-level collection, matched
by the `user_id` field), and their GCS resume/screenshot blobs under
`users/{uid}/` in the resumes bucket. Does NOT touch the Firebase Auth
account (they can log back in to an empty/onboarding state) or `jd_cache`
(shared, content-keyed, not user data).

The wipe itself now lives in `tools.account.delete`, which is also what
`POST /account/delete` runs — this stays the operator's door to it, and the
operator's door is the one that keeps the Auth account. A user deleting
themselves does want the login closed; an operator resetting a demo persona
wants to hand it straight back.

Usage:
    python -m cli.reset_user --user-id S4nOcOgxTpMjAU6WbOc8MjhBpKD3       # dry run
    python -m cli.reset_user --user-id S4nOcOgxTpMjAU6WbOc8MjhBpKD3 --execute
"""

import argparse
import asyncio

from dotenv import load_dotenv
from google.cloud import firestore

from obs.logging import bind_run_context, get_logger
from tools.account.delete import wipe_user_data

load_dotenv()

log = get_logger("cli.reset_user")


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
    counts = await wipe_user_data(db, args.user_id, execute=args.execute)

    verb = "Deleted" if args.execute else "Would delete"
    print(f"{verb} for user {args.user_id}:")
    print(f"  jobs subcollection:           {counts.jobs}")
    print(f"  applications subcollection:   {counts.applications}")
    print(f"  discarded_jobs subcollection: {counts.discarded_jobs}")
    print(f"  runs subcollection (costs):   {counts.runs}")
    print(f"  company_prefs subcollection:  {counts.company_prefs}")
    print(
        f"  users/{{uid}} doc:              "
        f"{'yes' if counts.user_doc_existed else 'no (already absent)'}"
    )
    print(f"  batch_runs docs (user_id):    {counts.batch_runs}")
    print(f"  GCS blobs (resumes bucket):   {counts.gcs_blobs}")
    if not args.execute:
        print("\nDry run only — re-run with --execute to actually delete.")

    log.info("reset_user.done", execute=args.execute, **counts.as_dict())


if __name__ == "__main__":
    asyncio.run(main())
