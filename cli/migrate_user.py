# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Copy a user's Firestore data from one user_id to another.

The local dev flow writes everything under ``users/me`` (AUTH_DEV_USER), but a
real Firebase sign-in yields an opaque uid. This copies the ``jobs`` subcollection
(and the user doc fields) so the deployed app shows your ranked jobs after you
sign in for the first time.

Usage:
    python -m cli.migrate_user --from me --to <firebase-uid> [--dry-run]
"""

import argparse

from dotenv import load_dotenv
from google.cloud import firestore


def _copy_subcollection(
    db: firestore.Client, src_uid: str, dst_uid: str, name: str, dry_run: bool
) -> int:
    src = db.collection("users").document(src_uid).collection(name)
    dst = db.collection("users").document(dst_uid).collection(name)
    n = 0
    for snap in src.stream():
        if not dry_run:
            dst.document(snap.id).set(snap.to_dict())
        n += 1
    return n


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default="me", help="source user_id")
    ap.add_argument("--to", dest="dst", required=True, help="destination user_id (uid)")
    ap.add_argument("--dry-run", action="store_true", help="count docs without writing")
    args = ap.parse_args()

    if args.src == args.dst:
        raise SystemExit("--from and --to must differ")

    db = firestore.Client()

    # Copy the parent user doc fields (profile pointer, settings, etc.).
    src_doc = db.collection("users").document(args.src).get()
    if src_doc.exists and not args.dry_run:
        db.collection("users").document(args.dst).set(
            src_doc.to_dict() or {}, merge=True
        )

    jobs = _copy_subcollection(db, args.src, args.dst, "jobs", args.dry_run)
    verb = "would copy" if args.dry_run else "copied"
    print(f"{verb} {jobs} job(s) from users/{args.src} -> users/{args.dst}")


if __name__ == "__main__":
    main()
