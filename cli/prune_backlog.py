# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Prune the unscored backlog by prerank, reversibly.

Moves pending, unscored jobs whose prerank is at or below ``--cutoff`` out of
``jobs`` and into ``discarded_jobs`` tombstones (:func:`score.prune_tombstone`),
each carrying its full ``restore`` payload. Discovery's seen-check treats the
tombstone like any other, so a pruned posting is not re-persisted while it
stays live; ``--undo`` is the way back.

Free: no model call. Dry run by default; ``--write`` writes. Pick the cutoff
from ``cli.prerank_eval``'s section 7, which scores the backlog the same way.

Never touches a job that is scored, decided (starred, approved, ...), or has an
application document.

Usage:
    python -m cli.prune_backlog --user-id <uid> --cutoff -20               # dry run
    python -m cli.prune_backlog --user-id <uid> --cutoff -20 --write
    python -m cli.prune_backlog --user-id <uid> --undo                     # dry run
    python -m cli.prune_backlog --user-id <uid> --undo --cutoff -20 --write
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter

from cli.eval_scoring import firestore_client
from cli.prerank_eval import is_pending_unscored
from models.job import Job
from obs.logging import bind_run_context, get_logger
from tools.matching.pipeline import geo_enforce_enabled
from tools.matching.prerank import (
    PRERANK_VERSION,
    preferences_from,
    prerank,
    residence_country,
)
from tools.matching.score import is_pruned, prune_tombstone
from tools.matching.selection import POOL_FIELDS

# Pins GOOGLE_CLOUD_PROJECT; ``firestore_client`` asserts it is set.
load_dotenv()

log = get_logger("cli.prune_backlog")

JOBS = "jobs"
TOMBSTONES = "discarded_jobs"
APPLICATIONS = "applications"

#: What selection's prerank reads, plus what this tool reports and re-checks.
PRUNE_FIELDS = [*POOL_FIELDS, "source", "user_decision"]

#: Writes per committed batch; Firestore refuses more than 500.
BATCH_WRITES = 450

SAMPLE_SIZE = 15


@dataclass(frozen=True)
class Candidate:
    job_id: str
    score: float
    source: str
    title: str
    company: str


@dataclass
class Plan:
    """What a prune would remove. ``prune`` is lowest prerank first."""

    prune: list[Candidate] = field(default_factory=list)
    kept: int = 0
    applied: int = 0
    over_limit: int = 0


@dataclass
class Outcome:
    tombstoned: int = 0
    deleted: int = 0
    stale: int = 0
    unrestorable: int = 0
    normalized: int = 0


def _chunks(items: list, size: int = BATCH_WRITES):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def applied_job_ids(user_ref) -> set[str]:
    """Job ids with an application document, by its ``job_id`` field and by
    the ``app-{job_id}`` id convention."""
    ids: set[str] = set()
    async for snap in user_ref.collection(APPLICATIONS).select(["job_id"]).stream():
        doc = snap.to_dict() or {}
        if isinstance(doc.get("job_id"), str):
            ids.add(doc["job_id"])
        if snap.id.startswith("app-"):
            ids.add(snap.id.removeprefix("app-"))
    return ids


async def plan_prune(
    db, user_id: str, cutoff: float, *, limit: int | None, enforce_geo: bool
) -> Plan | None:
    """Score the pending, unscored pool as selection does and pick jobs with
    prerank ``<= cutoff``. ``None`` when the user is absent. Reads only."""
    user_ref = db.collection("users").document(user_id)
    snap = await user_ref.get()
    if not snap.exists:
        return None
    user_doc = snap.to_dict() or {}
    prefs = preferences_from(user_doc)
    residence = residence_country(user_doc)
    applied = await applied_job_ids(user_ref)

    query = user_ref.collection(JOBS).where(
        filter=FieldFilter("user_decision", "==", "pending")
    )
    plan = Plan()
    chosen: list[Candidate] = []
    async for job_snap in query.select(PRUNE_FIELDS).stream():
        doc = job_snap.to_dict() or {}
        if not is_pending_unscored(doc):
            continue
        if job_snap.id in applied:
            plan.applied += 1
            continue
        score = prerank(
            {**doc, "id": job_snap.id}, prefs, residence, enforce_geo=enforce_geo
        ).score
        if score > cutoff:
            plan.kept += 1
            continue
        chosen.append(
            Candidate(
                job_snap.id,
                score,
                str(doc.get("source") or "?"),
                str(doc.get("title") or ""),
                str(doc.get("company") or ""),
            )
        )
    chosen.sort(key=lambda c: (c.score, c.job_id))
    if limit is not None and len(chosen) > limit:
        plan.over_limit = len(chosen) - limit
        plan.kept += plan.over_limit
        chosen = chosen[:limit]
    plan.prune = chosen
    return plan


async def write_prune(
    db, user_id: str, plan: Plan, cutoff: float, *, at: str | None = None
) -> Outcome:
    """Tombstone, then delete, each planned job, one chunk at a time.

    Each chunk is re-read in full and re-checked first; a job that has since
    been scored, decided, applied to, or no longer validates as a ``Job`` is
    skipped. The tombstone batch commits before the delete batch, so a crash
    between them leaves jobs in both collections, never in neither; rerunning
    finishes them.
    """
    at = at or datetime.now(UTC).isoformat()
    user_ref = db.collection("users").document(user_id)
    jobs_ref = user_ref.collection(JOBS)
    tombs_ref = user_ref.collection(TOMBSTONES)
    applied = await applied_job_ids(user_ref)
    out = Outcome()
    for chunk in _chunks(plan.prune):
        scores = {c.job_id: c.score for c in chunk}
        refs = [jobs_ref.document(c.job_id) for c in chunk]
        moves: list[tuple] = []
        async for snap in db.get_all(refs):
            doc = snap.to_dict() if snap.exists else None
            if not doc or not is_pending_unscored(doc) or snap.id in applied:
                out.stale += 1
                continue
            try:
                job = Job.model_validate(doc)
            except Exception:
                out.unrestorable += 1
                continue
            stone = prune_tombstone(
                job, prerank_score=scores[snap.id], cutoff=cutoff, at=at
            )
            out.normalized += stone["restore"] != doc
            moves.append((snap.id, stone))
        if not moves:
            continue
        batch = db.batch()
        for job_id, stone in moves:
            batch.set(tombs_ref.document(job_id), stone)
        await batch.commit()
        out.tombstoned += len(moves)
        batch = db.batch()
        for job_id, _ in moves:
            batch.delete(jobs_ref.document(job_id))
        await batch.commit()
        out.deleted += len(moves)
    return out


# --------------------------------------------------------------------- undo


@dataclass
class UndoOutcome:
    matched: int = 0
    restored: int = 0
    already_live: int = 0
    unrestorable: int = 0


def matches_filter(
    doc: dict,
    *,
    version: int | None = None,
    cutoff: float | None = None,
    since: datetime | None = None,
) -> bool:
    """A pruned tombstone passing ``--version`` / ``--cutoff`` / ``--since``.
    Scored tombstones never match."""
    if not is_pruned(doc):
        return False
    pruned = doc["pruned"]
    if version is not None and pruned.get("version") != version:
        return False
    if cutoff is not None and pruned.get("cutoff") != cutoff:
        return False
    if since is not None:
        at = _instant(pruned.get("at"))
        if at is None or at < since:
            return False
    return True


def _instant(value) -> datetime | None:
    """An ISO-8601 string as a tz-aware instant; naive means UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _since_arg(value: str) -> datetime:
    parsed = _instant(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {value!r}")
    return parsed


async def pruned_ids(db, user_id: str, **filters) -> list[str]:
    """Ids of the user's tombstones :func:`matches_filter` accepts, in stream
    order. Projects ``pruned`` alone, so the heavy ``restore`` is not read."""
    tombs_ref = db.collection("users").document(user_id).collection(TOMBSTONES)
    return [
        snap.id
        async for snap in tombs_ref.select(["pruned"]).stream()
        if matches_filter(snap.to_dict() or {}, **filters)
    ]


async def undo_prune(
    db, user_id: str, ids: list[str], *, write: bool, **filters
) -> UndoOutcome:
    """Restore pruned tombstones to ``jobs``: the job batch commits before the
    tombstone-delete batch, ``geo_resurrect.resurrect_one``'s order. A job doc
    that already exists (a prune interrupted between its batches) is left as
    it is and only the tombstone goes. Each tombstone is re-read and re-checked
    against the filters; dry run when ``write`` is false."""
    user_ref = db.collection("users").document(user_id)
    jobs_ref = user_ref.collection(JOBS)
    tombs_ref = user_ref.collection(TOMBSTONES)
    out = UndoOutcome()
    for chunk in _chunks(ids):
        restores: list[tuple[str, dict | None]] = []
        tomb_snaps = [
            s async for s in db.get_all([tombs_ref.document(i) for i in chunk])
        ]
        live = {
            s.id
            async for s in db.get_all([jobs_ref.document(t.id) for t in tomb_snaps])
            if s.exists
        }
        for snap in tomb_snaps:
            doc = snap.to_dict() if snap.exists else None
            if not doc or not matches_filter(doc, **filters):
                continue
            out.matched += 1
            if snap.id in live:
                out.already_live += 1
                restores.append((snap.id, None))
                continue
            try:
                job = Job.model_validate(doc.get("restore"))
            except Exception:
                out.unrestorable += 1
                print(f"  ! {snap.id}: restore payload invalid — left tombstoned")
                continue
            restores.append((snap.id, job.model_dump(mode="json")))
        out.restored += sum(1 for _, payload in restores if payload is not None)
        if not write or not restores:
            continue
        batch = db.batch()
        writes = 0
        for job_id, payload in restores:
            if payload is not None:
                batch.set(jobs_ref.document(job_id), payload)
                writes += 1
        if writes:
            await batch.commit()
        batch = db.batch()
        for job_id, _ in restores:
            batch.delete(tombs_ref.document(job_id))
        await batch.commit()
    return out


# ------------------------------------------------------------------- report


def report_plan(plan: Plan, cutoff: float) -> None:
    print(f"  prerank v{PRERANK_VERSION}  cutoff {cutoff:+g} (prune prerank <= cutoff)")
    print(f"  prune {len(plan.prune)}  keep {plan.kept}")
    if plan.over_limit:
        print(f"  (--limit held back {plan.over_limit} more at or below the cutoff)")
    if plan.applied:
        print(f"  skipped {plan.applied} pending job(s) with an application")
    if not plan.prune:
        return
    print("  per platform:")
    for source, count in Counter(c.source for c in plan.prune).most_common():
        print(f"    {count:6d}  {source}")
    riskiest = sorted(plan.prune, key=lambda c: (-c.score, c.job_id))[:SAMPLE_SIZE]
    print(f"  the {len(riskiest)} highest-prerank jobs being pruned:")
    for c in riskiest:
        print(f"    {c.score:+6g}  {c.company} - {c.title[:60]}")


async def run_prune(
    db,
    user_id: str,
    cutoff: float,
    *,
    limit: int | None,
    write: bool,
    enforce_geo: bool,
) -> tuple[Plan | None, Outcome | None]:
    plan = await plan_prune(db, user_id, cutoff, limit=limit, enforce_geo=enforce_geo)
    if plan is None:
        print(f"  ! no such user: users/{user_id}")
        return None, None
    report_plan(plan, cutoff)
    if not write:
        print("✓ dry run: nothing written (pass --write to prune)")
        return plan, None
    out = await write_prune(db, user_id, plan, cutoff)
    print(
        f"✓ pruned {out.deleted} job(s) into {TOMBSTONES}; skipped {out.stale} "
        f"stale, {out.unrestorable} unrestorable"
    )
    if out.normalized:
        print(
            f"  {out.normalized} restore payload(s) differ from the stored doc "
            "(fields outside the Job model, or defaults filled in)"
        )
    log.info("prune_backlog.done", cutoff=cutoff, **vars(out))
    return plan, out


async def run_undo(
    db, user_id: str, *, limit: int | None, write: bool, **filters
) -> UndoOutcome:
    ids = await pruned_ids(db, user_id, **filters)
    if limit is not None:
        ids = ids[:limit]
    out = await undo_prune(db, user_id, ids, write=write, **filters)
    verb = "restored" if write else "would restore"
    print(
        f"✓ {verb} {out.restored} of {out.matched} pruned tombstone(s); "
        f"{out.already_live} already in {JOBS}, {out.unrestorable} unrestorable"
    )
    if not write:
        print("  dry run: nothing written (pass --write to restore)")
    log.info("prune_backlog.undo_done", write=write, **vars(out))
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--cutoff",
        type=float,
        default=None,
        help="Prune prerank <= this. Required to prune; with --undo, a filter",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--write", action="store_true", help="Actually write")
    parser.add_argument(
        "--undo", action="store_true", help="Restore pruned tombstones instead"
    )
    parser.add_argument(
        "--version", type=int, default=None, help="--undo: only this prerank version"
    )
    parser.add_argument(
        "--since",
        type=_since_arg,
        default=None,
        help="--undo: only prunes at or after this ISO-8601 UTC instant",
    )
    args = parser.parse_args()
    if not args.undo and args.cutoff is None:
        parser.error("--cutoff is required unless --undo")
    if not args.undo and (args.version is not None or args.since is not None):
        parser.error("--version and --since apply only to --undo")
    bind_run_context("prune_backlog", user_id=args.user_id)
    db = firestore_client()
    if args.undo:
        await run_undo(
            db,
            args.user_id,
            limit=args.limit,
            write=args.write,
            version=args.version,
            cutoff=args.cutoff,
            since=args.since,
        )
    else:
        await run_prune(
            db,
            args.user_id,
            args.cutoff,
            limit=args.limit,
            write=args.write,
            enforce_geo=geo_enforce_enabled(),
        )


if __name__ == "__main__":
    asyncio.run(main())
