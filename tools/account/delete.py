# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deleting one user: what gets erased, what must not be, and in what order.

Both ``cli/reset_user.py`` and the API's ``POST /account/delete`` are thin
wrappers over this module, so "delete my account" and "reset this demo
account" cannot drift apart. It is an extraction rather than an import because
``cli/reset_user.py`` calls ``load_dotenv()`` at import time, and nothing in
``api/`` or ``tools/`` may import from ``cli/``.

Erased for ``users/{uid}``: the per-user subcollections
(:data:`USER_SUBCOLLECTIONS`), that user's ``batch_runs`` documents (top-level,
matched on ``user_id``), their GCS blobs under ``users/{uid}/`` in the resumes
bucket, and the user document itself.

Deliberately left alone: ``jd_cache`` and ``board_cache/``. Both are
content-keyed and shared across users, hold no personal data, and evicting
them would charge every remaining user a re-parse. ``board_cache/`` sits
outside the ``users/{uid}/`` prefix so this wipe cannot reach it; keep it
there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from obs.logging import get_logger
from tools import allowlist
from tools.company_prefs import COLLECTION as COMPANY_PREFS
from tools.decisions import COLLECTION as DECISIONS
from tools.exposures import COLLECTION as EXPOSURES
from tools.journeys import COLLECTION as JOURNEYS
from tools.run_costs import COLLECTION as RUN_COSTS
from tools.spend.consent import COLLECTION as SPEND_CONSENTS
from tools.tailoring.render import resume_bucket_name

log = get_logger("tools.account.delete")

#: Field written on ``users/{uid}`` **before** anything is destroyed, and the
#: one thing that stops the background loops picking the account back up. See
#: :func:`delete_account` for the window it does and does not close.
DELETED_AT = "deleted_at"

#: Every subcollection under ``users/{uid}``. ``runs`` is the per-run cost
#: ledger; it goes with the rest, because it is per-user billing detail and
#: nothing aggregates it across accounts.
#:
#: Everything after the first three is named by importing the constant the
#: owning module exports rather than repeating the string. Anything that adds
#: a subcollection under ``users/{uid}`` must be added here, or a deleted
#: account keeps that data — this list has been missed three times.
#:
#: The guard is
#: ``tests/unit/test_account_delete.py::test_every_subcollection_the_code_writes_is_one_the_wipe_deletes``,
#: which discovers the exporting modules rather than restating them.
USER_SUBCOLLECTIONS = (
    "jobs",
    "applications",
    "discarded_jobs",
    RUN_COSTS,
    COMPANY_PREFS,
    JOURNEYS,
    DECISIONS,
    EXPOSURES,
    SPEND_CONSENTS,
)

#: Firestore's hard cap on writes per batch.
_WRITE_CHUNK = 500


@dataclass(frozen=True)
class WipeCounts:
    """What a wipe deleted (or, on a dry run, what it *would* delete)."""

    jobs: int = 0
    applications: int = 0
    discarded_jobs: int = 0
    runs: int = 0
    company_prefs: int = 0
    journeys: int = 0
    decisions: int = 0
    exposures: int = 0
    spend_consents: int = 0
    batch_runs: int = 0
    gcs_blobs: int = 0
    #: Whether ``users/{uid}`` was there to delete. False on a re-run of a wipe
    #: that already finished, which is a normal outcome rather than an error.
    user_doc_existed: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def is_deleted(doc: Mapping | None) -> bool:
    """Has this ``users/{uid}`` document been tombstoned?

    Pure, and takes the document rather than a user id, so a caller that
    already holds one pays no extra read.
    """
    return bool(doc and doc.get(DELETED_AT))


def _now() -> datetime:
    return datetime.now(UTC)


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
    """Delete one user's GCS blobs. Blocking; reached through
    ``asyncio.to_thread``.

    The ``users/{uid}/`` prefix is the whole guarantee that this stays inside
    one user's data; the shared ``board_cache/`` is a sibling of ``users/``,
    not a child.
    """
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(resume_bucket_name())
    blobs = list(bucket.list_blobs(prefix=f"users/{user_id}/"))
    if execute:
        for blob in blobs:
            blob.delete()
    return len(blobs)


async def wipe_user_data(
    db: firestore.AsyncClient, user_id: str, *, execute: bool
) -> WipeCounts:
    """Erase everything belonging to ``user_id``. Reports counts either way.

    Destroys Firestore documents and GCS blobs. With ``execute=False``
    nothing is written, but every branch still streams what it would delete,
    so a dry run is an inventory rather than an estimate.

    ``users/{uid}`` goes last, because it is the index into everything else:
    an interrupted wipe then leaves an account that is still findable and
    still refusing work, and a re-run finishes the job. Firestore keeps
    subcollections alive when their parent is deleted, so deleting it first
    would orphan them.
    """
    user_ref = db.collection("users").document(user_id)

    counts: dict[str, int] = {}
    for name in USER_SUBCOLLECTIONS:
        counts[name] = await _delete_subcollection(
            db, user_ref.collection(name), execute=execute
        )
    counts["batch_runs"] = await _delete_batch_runs(db, user_id, execute=execute)
    # Blocking google-cloud-storage calls, off the event loop: this runs inside
    # a request on the API and beside other tasks on the worker.
    counts["gcs_blobs"] = await asyncio.to_thread(
        _delete_gcs_prefix, user_id, execute=execute
    )

    user_doc_existed = (await user_ref.get()).exists
    if execute and user_doc_existed:
        await user_ref.delete()

    return WipeCounts(
        jobs=counts["jobs"],
        applications=counts["applications"],
        discarded_jobs=counts["discarded_jobs"],
        runs=counts[RUN_COSTS],
        company_prefs=counts[COMPANY_PREFS],
        journeys=counts[JOURNEYS],
        decisions=counts[DECISIONS],
        exposures=counts[EXPOSURES],
        spend_consents=counts[SPEND_CONSENTS],
        batch_runs=counts["batch_runs"],
        gcs_blobs=counts["gcs_blobs"],
        user_doc_existed=user_doc_existed,
    )


async def delete_account(
    db: firestore.AsyncClient,
    user_id: str,
    *,
    close_auth: Callable[[str], None],
    email: str | None = None,
    now: datetime | None = None,
) -> WipeCounts:
    """Close the account, then erase it. Irreversible. The order is the design:

    1. Write the :data:`DELETED_AT` tombstone, which every background loop
       reads and refuses on, so no new cycle starts.
    2. ``close_auth(user_id)`` — delete the Firebase Auth user, so no new
       request can create data behind the wipe. The caller must make this
       idempotent: an ID token stays verifiable for up to an hour after the
       account is deleted, so retrying a half-finished deletion is a real path.
    3. Free the user's allowlist seat and drop their ``waitlist/{email}`` doc,
       if any. The waitlist drop is not gated on ``enforced()``, since the doc
       can predate a flag flip.
    4. The wipe, ending with ``users/{uid}`` itself.

    This cannot make deletion atomic against a cycle already in flight:
    ``run_discovery_cycle`` finishes with a ``set(..., merge=True)`` that
    recreates a deleted document, and ``persist_new_jobs`` writes jobs without
    consulting the user doc. The tombstone bounds the window to one
    already-dispatched cycle rather than closing it; both entry points are
    re-runnable, so an operator who sees residue re-runs the wipe.

    ``email`` is the Auth email the caller resolved *before* calling this,
    because step 2 may already have deleted the record it would be looked up
    from. Pass ``None`` when there is none. Seat accounting is a no-op while
    ``tools.allowlist.enforced()`` is off.
    """
    user_ref = db.collection("users").document(user_id)
    deleted_at = (now or _now()).isoformat()

    # Merged rather than set: a cycle in flight may still be reading the
    # document, and this write only adds the field the loops check. It creates
    # the document if the account has none, keeping the ordering unconditional.
    await user_ref.set({DELETED_AT: deleted_at}, merge=True)
    log.info("account.tombstoned", user_id=user_id, deleted_at=deleted_at)

    # Blocking Firebase Admin call, off the event loop.
    await asyncio.to_thread(close_auth, user_id)
    log.info("account.auth_closed", user_id=user_id)

    if allowlist.enforced():
        if email:
            freed = await allowlist.revoke(db, email, revoked_by="account_deletion")
            log.info("account.seat_freed", user_id=user_id, freed=freed)
        else:
            log.info("account.seat_free_skipped_no_email", user_id=user_id)

    if email:
        await allowlist.remove_from_waitlist(db, email)
        log.info("account.waitlist_cleared", user_id=user_id)

    counts = await wipe_user_data(db, user_id, execute=True)
    log.info("account.deleted", user_id=user_id, **counts.as_dict())
    return counts
