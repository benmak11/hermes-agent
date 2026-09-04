# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deleting one user: what gets erased, what must not be, and in what order.

Lifted verbatim out of ``cli/reset_user.py``, which had performed this wipe for
demo resets since 2026-07-22 and has been run in anger. The CLI is now a thin
wrapper over this module and the API's ``POST /account/delete`` is another, so
"delete my account" and "reset this demo account" cannot drift apart.

**Why an extraction rather than an import.** ``cli/reset_user.py`` calls
``load_dotenv()`` at import time and parses ``argparse`` flags. Neither belongs
in the API's import graph — a route module that pulled in that CLI would load
the developer's ``.env`` (including ``AUTH_DEV_MODE=1``) into the API process
just by being imported, which is the exact leak ``tests/unit/conftest.py``
documents. Nothing in ``api/`` or ``tools/`` imports from ``cli/``; this does
not become the first thing that does.

What is erased for ``users/{uid}``: the four per-user subcollections
(:data:`USER_SUBCOLLECTIONS`), that user's ``batch_runs`` documents (a
top-level collection, matched on the ``user_id`` field), their GCS blobs under
``users/{uid}/`` in the resumes bucket, and the user document itself.

**What is deliberately left alone: ``jd_cache`` and ``board_cache/``.** Both are
content-keyed and shared across every user — a job description parsed once is
reused by whoever sees that posting next. They hold no personal data (a JD is
the employer's public text), and evicting them because one account closed would
charge every remaining user a re-parse. ``board_cache/`` sits outside the
``users/{uid}/`` prefix precisely so that this wipe cannot reach it; keep it
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
from tools.company_prefs import COLLECTION as COMPANY_PREFS
from tools.run_costs import COLLECTION as RUN_COSTS
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
#: The last two are named by importing the constant the owning module already
#: exports, not by repeating the string — ``company_prefs`` was added by a
#: later PR than the wipe and was missed here precisely because this list was
#: hand-maintained. Anything that adds a subcollection under ``users/{uid}``
#: must be added here, or a deleted account leaves it behind.
USER_SUBCOLLECTIONS = (
    "jobs",
    "applications",
    "discarded_jobs",
    RUN_COSTS,
    COMPANY_PREFS,
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
    batch_runs: int = 0
    gcs_blobs: int = 0
    #: Whether ``users/{uid}`` was there to delete. False on a re-run of a wipe
    #: that already finished, which is a normal outcome rather than an error.
    user_doc_existed: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def is_deleted(doc: Mapping | None) -> bool:
    """Has this ``users/{uid}`` document been tombstoned?

    Pure, and takes the document rather than a user id, so the callers that
    already hold one — ``cron_tick``'s fan-out streams every user document —
    pay no extra read to ask.
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
    """Blocking; reached through ``asyncio.to_thread``.

    The prefix is the whole guarantee that this stays inside one user's data:
    ``users/{uid}/`` is where resumes and application screenshots are written,
    and ``board_cache/`` — shared, content-keyed — is a sibling of ``users/``
    rather than a child of it.
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

    With ``execute=False`` nothing is written: every branch still streams what
    it would delete, so a dry run is an honest inventory rather than an
    estimate. That is the CLI's default and the reason it is safe to type.

    **``users/{uid}`` goes last.** The document is the index into everything
    else — the profile, the settings, the tombstone the loops read — so a wipe
    interrupted halfway (a killed container, a GCS error) leaves an account
    that is still findable and still refusing work, and a re-run finishes the
    job. Deleting it first would leave orphaned subcollections that only a
    collection-group query could ever find again. (Firestore keeps
    subcollections alive when their parent document is deleted, so the order
    changes what a failure leaves behind, never what a success does.)
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
        batch_runs=counts["batch_runs"],
        gcs_blobs=counts["gcs_blobs"],
        user_doc_existed=user_doc_existed,
    )


async def delete_account(
    db: firestore.AsyncClient,
    user_id: str,
    *,
    close_auth: Callable[[str], None],
    now: datetime | None = None,
) -> WipeCounts:
    """Close the account, then erase it. **The order is the design.**

    1. Write the :data:`DELETED_AT` tombstone. Every background loop reads it
       and refuses (see :func:`is_deleted`), so no *new* cycle starts.
    2. ``close_auth(user_id)`` — delete the Firebase Auth user. From here the
       account cannot sign in, so no new request can create data behind the
       wipe. It is the caller's job to make this idempotent: a re-run must
       treat "already gone" as success, because a Firebase ID token stays
       verifiable for up to an hour after the account it names is deleted, so
       the user retrying a half-finished deletion is a reachable path and not
       an exotic one.
    3. The wipe, ending with ``users/{uid}`` itself.

    **What this does not do, and cannot: make deletion atomic against a cycle
    that is already in flight.** ``api.routes.discovery.run_discovery_cycle``
    finishes with ``_user_ref(user_id).set(..., merge=True)``, which *recreates*
    a deleted document, and ``tools.discovery.pipeline.persist_new_jobs`` writes
    job documents with an unconditional ``set()`` that never consults the user
    doc. A cycle dispatched a second before the tombstone landed will therefore
    still write both. Closing that would mean putting preconditions through
    shipped Phase 2/3 machinery, which is a different and much larger change.

    So: **the tombstone bounds the window to one already-dispatched cycle; it
    does not close it.** The mitigation is that both entry points — the endpoint
    and ``cli/reset_user.py`` — are re-runnable and cost nothing but a few
    Firestore reads, so an operator who sees residue re-runs the wipe.

    Leaves no seat accounting behind: when the allowlist lands (Phase 4 D1/D2),
    freeing this user's seat belongs here, between step 2 and step 3.
    """
    user_ref = db.collection("users").document(user_id)
    deleted_at = (now or _now()).isoformat()

    # First, and merged rather than set: the document may still be being read
    # by a cycle in flight, and this write is only about adding the field the
    # loops check. It creates the document if the account has none, which costs
    # one write on an account that never onboarded and keeps the ordering
    # unconditional.
    await user_ref.set({DELETED_AT: deleted_at}, merge=True)
    log.info("account.tombstoned", user_id=user_id, deleted_at=deleted_at)

    # Blocking Firebase Admin call, off the event loop.
    await asyncio.to_thread(close_auth, user_id)
    log.info("account.auth_closed", user_id=user_id)

    counts = await wipe_user_data(db, user_id, execute=True)
    log.info("account.deleted", user_id=user_id, **counts.as_dict())
    return counts
