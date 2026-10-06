# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``GET /activity`` — what is actually happening, from server-side records only.

Every state below is derived from a record — a lease, an open run-ledger doc, a
``batch_runs`` document, an application status — and never from how long a
client has been waiting, because the UI must not claim work is happening when
it isn't. Two rules follow: ``done``/``total`` are both present or both absent
(that pair is the client's only licence to draw a bar, and it is populated only
on the batch score leg), and staleness is derived rather than heartbeated — past
``tools.run_costs.STALE_AFTER`` an open record reads ``stalled``.

**This route can never schedule background work.** ``GET /jobs/pending`` and
``GET /settings/discovery`` both ``add_task(tick_user, …)``, which under
QUEUE_MODE can reach a paid Vertex batch; a status endpoint is polled harder
than either. It takes no ``BackgroundTasks`` parameter, schedules nothing and
writes nothing, and ``tests/unit/test_local_guards.py`` asserts that from the
signature.

The response also carries an ``allowance`` block — the two caps with what is
left of each and two independent reset instants — read off the same
``users/{uid}`` document the route already fetched. See :func:`_allowance`.

Known limits: a discovery or sweep cycle that *failed* is not visible here
(this route queries only open ledger docs, and ``last_*_at`` is written on
success only), so a failed loop reads as idle. And nothing here estimates a
duration — there is no completion distribution to quote from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from api.deps import verify_user
from api.routes.discovery import _lease_at, _lease_held, _next_iso, _parse_ts
from models.settings import DiscoverySettings
from obs.logging import get_logger
from tools.applications import state as app_state
from tools.discovery import budget as discovery_budget
from tools.matching import batch_runs
from tools.matching import budget as matching_budget
from tools.run_costs import COLLECTION as RUNS_COLLECTION
from tools.run_costs import RUNNING as LEDGER_RUNNING
from tools.run_costs import run_is_stalled

router = APIRouter(tags=["activity"])
log = get_logger("api.activity")

_db: firestore.AsyncClient | None = None


def _client() -> firestore.AsyncClient:
    global _db
    if _db is None:
        _db = firestore.AsyncClient()
    return _db


def _now() -> datetime:
    return datetime.now(UTC)


#: The seven legs of the product that can be doing something. Always all seven
#: in the response, in this order: a client that renders a fixed list cannot
#: mistake "this leg was omitted" for "this leg is idle".
KINDS = (
    "discovery",
    "sweep",
    "scoring",
    "batch_scoring",
    "extract",
    "tailoring",
    "submission",
)

# The nine states. Each one is a claim about a record, and the comment is what
# the client is allowed to say on the strength of it.
NEVER_STARTED = "never_started"  # no evidence this has ever run
IDLE_SCHEDULED = "idle_scheduled"  # nothing running, and a real next time exists
IDLE_UNSCHEDULED = "idle_unscheduled"  # nothing running and **nothing will**
QUEUED = "queued"  # handed to a queue; no process has claimed it
RUNNING = "running"  # a live, unexpired claim
WAITING_EXTERNAL = "waiting_external"  # with Google; we are doing nothing
FINISHED = "finished"  # terminal, with counts
FAILED = "failed"  # terminal, with an error the user can act on
STALLED = "stalled"  # a claim older than the ceiling, with no terminal write

#: Which leg a run-ledger ``runner`` belongs to. The ledger is the only place
#: some of these legs leave any in-flight trace at all.
_RUNNER_KINDS = {
    "auto_discovery": "discovery",
    "discovery": "discovery",
    "liveness_sweep": "sweep",
    "score_task": "scoring",
    "score_backlog": "scoring",
    "matching": "scoring",
    "batch_start": "batch_scoring",
    "profile_extract": "extract",
    "tailoring": "tailoring",
    "submission": "submission",
}

#: Application statuses that mean a process is supposed to be working, per leg.
_TAILORING_ACTIVE = ("tailoring",)
_SUBMISSION_ACTIVE = ("submitting",)


def _next_hour(now: datetime) -> str:
    """The top of the next hour — the Cloud Scheduler tick's real next fire.

    ``hermes-discovery-tick`` is ``0 * * * *`` and enabled (verified live). It
    is the only schedule in the system, so it is the only honest ``next_at``
    for anything the tick drives.
    """
    return (
        now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    ).isoformat()


def _item(
    kind: str,
    state: str,
    *,
    since: str | None = None,
    next_at: str | None = None,
    ref: dict | None = None,
    done: int | None = None,
    total: int | None = None,
    detail: dict | None = None,
) -> dict:
    """One leg's entry, with the both-or-neither rule enforced here.

    ``done``/``total`` is the client's only licence to draw a progress bar, so
    a half-populated pair is dropped rather than passed on — a known numerator
    over an unknown denominator is a fabricated percentage.
    """
    if done is None or total is None:
        done = total = None
    return {
        "kind": kind,
        "state": state,
        "since": since,
        "next_at": next_at,
        "ref": ref,
        "done": done,
        "total": total,
        "detail": detail or {},
    }


def _allowance(user_doc: dict, now: datetime) -> dict:
    """What is left of the two caps, and when each one rolls. Read off the
    ``users/{uid}`` document the route already fetched, so no extra reads, and
    nothing is written.

    ``limit``/``remaining`` are ``None``, never ``0``, when a cap is off:
    "unlimited" is not a quantity and a ``0`` renders as "no searches left".
    The scoring cap has no kill switch (``SCORING_BUDGET_PER_DAY=0`` means *no
    ratings*, not *no cap*), so ``ratings.limit`` is a number today, typed
    ``| None`` so a client cannot depend on that.

    The two ``resets_at`` are independent — searches roll next Monday 00:00
    UTC, ratings next UTC midnight — and coincide only on a Sunday.

    ``ratings.remaining`` is what the next reservation would actually grant,
    ``min(remaining_day, remaining_cycle)``: the per-cycle counter has no time
    rollover, only a new ``cycle_id`` clears it, so a user who rated out this
    search's window and then crossed midnight has a full daily allowance and a
    grant of zero. ``remaining_cycle`` is carried alongside so a surface can
    say which window binds — when it is the cycle, waiting for ``resets_at``
    will not help, only a new search will.

    Both figures come from ``apply_reservation(..., wanted=0)`` on a discarded
    copy of the state, rather than a second implementation of its rollover
    rules, so a display can never promise what the next reservation would
    refuse. ``used`` is the stored counter, never ``limit - remaining``, which
    clamps at the cap and would read "3 of 3" over 46 rated jobs.
    """
    search_limits = discovery_budget.Limits.from_env()
    search_state = user_doc.get(discovery_budget.FIELD)
    tz = discovery_budget.UTC_TZ

    rating_limits = matching_budget.Limits.from_env()
    rating_state = user_doc.get(matching_budget.FIELD)
    _discarded, rated = matching_budget.apply_reservation(
        rating_state,
        0,
        now=now,
        cycle_id=None,
        limits=rating_limits,
    )
    return {
        "searches": {
            "used": discovery_budget.used(search_state, now=now, tz=tz),
            "limit": search_limits.per_week if search_limits.enforced else None,
            "remaining": discovery_budget.remaining(
                search_state, now=now, tz=tz, limits=search_limits
            ),
            "resets_at": discovery_budget.resets_at(now, tz),
        },
        "ratings": {
            "used": matching_budget.used(rating_state, now=now),
            "limit": rating_limits.per_day,
            "remaining": min(rated.remaining_day, rated.remaining_cycle),
            "remaining_cycle": rated.remaining_cycle,
            "resets_at": matching_budget.resets_at(now),
        },
    }


def _open_state(doc: dict, now: datetime) -> str:
    """``running`` or ``stalled`` for an open ledger doc — never anything else."""
    return STALLED if run_is_stalled(doc, now=now) else RUNNING


def _loop_item(
    kind: str,
    *,
    enabled: bool,
    last_at: str | None,
    lease: Any,
    interval_hours: int,
    last_metrics: dict | None,
    open_doc: dict | None,
    now: datetime,
) -> dict:
    """``discovery`` / ``sweep``: a slot lease and an interval, per user."""
    if open_doc is not None:
        return _item(
            kind,
            _open_state(open_doc, now),
            since=open_doc.get("started_at"),
            ref={"run_id": open_doc.get("run_id")},
            detail={"trigger": open_doc.get("trigger")},
        )
    if _lease_held(lease, now):
        # A cycle has claimed the slot but has not written its ledger doc yet
        # (or the write failed). The lease is the claim; it is evidence enough.
        acquired = _lease_at(lease, "acquired_at")
        return _item(kind, RUNNING, since=acquired.isoformat() if acquired else None)

    detail = dict(last_metrics or {})
    if not enabled:
        # The toggle is off, so nothing is running and nothing will — there is
        # no next time to show.
        return _item(kind, IDLE_UNSCHEDULED, since=last_at, detail=detail)
    if last_at is None and not last_metrics:
        # Scheduled but never run: the interval has nothing to count from, so
        # the only real next time is the tick itself.
        return _item(kind, NEVER_STARTED, next_at=_next_hour(now))
    return _item(
        kind,
        IDLE_SCHEDULED,
        since=last_at,
        next_at=_next_iso(last_at, interval_hours, lease=lease, now=now)
        or _next_hour(now),
        detail=detail,
    )


def _scoring_item(
    *,
    auto_discovery: bool,
    last_discovery: dict | None,
    open_doc: dict | None,
    now: datetime,
) -> dict:
    """Online scoring: the backlog, and whether anything is coming for it."""
    if open_doc is not None:
        return _item(
            "scoring",
            _open_state(open_doc, now),
            since=open_doc.get("started_at"),
            ref={"run_id": open_doc.get("run_id")},
            detail={"trigger": open_doc.get("trigger")},
        )

    metrics = last_discovery or {}
    # ``tools.matching.score.count_unscored``'s figure, as the last cycle
    # recorded it — the one definition of "waiting to be scored" in this
    # codebase. ``None`` when it could not be counted, which stays ``None``:
    # a fabricated zero is the claim "nothing is waiting".
    backlog = metrics.get("unscored_backlog")
    detail = {
        "unscored_backlog": backlog,
        "scored": metrics.get("scored"),
        "discarded": metrics.get("discarded"),
        "failed": metrics.get("failed"),
    }
    if not metrics and backlog is None:
        return _item("scoring", NEVER_STARTED)
    if backlog == 0:
        # Only a measured zero licenses "up to date". No ``since``: the last
        # cycle's timestamp belongs to the discovery leg.
        return _item("scoring", FINISHED, detail=detail)
    # Either jobs are waiting, or the count failed. ``None`` is treated as
    # "there may be work" rather than as zero — seen live, where a failed count
    # sat over 9,219 unscored jobs and an earlier version answered ``finished``.
    if auto_discovery:
        # The hourly tick's discovery cycle scores what it finds, so a backlog
        # under auto-discovery really does have something coming.
        return _item("scoring", IDLE_SCHEDULED, next_at=_next_hour(now), detail=detail)
    # Auto-discovery off: the cron will not fire and the only other way in is a
    # priced click nobody has made, so jobs are waiting and nothing will score
    # them.
    return _item("scoring", IDLE_UNSCHEDULED, detail=detail)


def _batch_item(
    runs: list[dict],
    open_doc: dict | None,
    last_run: dict | None,
    now: datetime,
) -> dict:
    """The resumable Vertex batch legs.

    The only place ``done``/``total`` is ever populated, and only for the score
    leg while a resume pass is actually ingesting it.
    """
    for run in runs:
        job_name = run.get("job_name")
        claimed = _parse_ts(run.get("claimed_at"))
        stage = run.get("stage")
        ref = {"batch_run": run.get("id")}
        detail = {"stage": stage, **_committed_of(run)}
        if job_name and claimed is None:
            # With Google: the batch was submitted and nothing of ours touches
            # it until a resume tick polls. No percent and no duration — there
            # is no observed ingest to quote from.
            return _item(
                "batch_scoring",
                WAITING_EXTERNAL,
                since=run.get("updated_at") or run.get("created_at"),
                next_at=_next_hour(now),
                ref=ref,
                detail=detail,
            )
        live = (
            claimed is not None
            and (now - claimed).total_seconds() < batch_runs._CLAIM_TTL_SECONDS
        )
        if not live:
            # A claim older than the ingest TTL, or a run submitted without a
            # job name: something died mid-flight. It must not read as running.
            return _item(
                "batch_scoring",
                STALLED,
                since=run.get("claimed_at") or run.get("updated_at"),
                ref=ref,
                detail=detail,
            )
        done = total = None
        if stage == "score":
            counts = run.get("counts") or {}
            job_ids = run.get("job_ids") or []
            if counts and job_ids:
                done, total = sum(counts.values()), len(job_ids)
        return _item(
            "batch_scoring",
            RUNNING,
            since=run.get("claimed_at"),
            ref=ref,
            done=done,
            total=total,
            detail=detail,
        )

    if open_doc is not None:
        return _item(
            "batch_scoring",
            _open_state(open_doc, now),
            since=open_doc.get("started_at"),
            ref={"run_id": open_doc.get("run_id")},
        )
    if last_run:
        # The last cycle started a batch and it is not in the running set, so
        # it is terminal — not the same as never having run, which is what this
        # answered live over a real ``batch_run`` tag from the day before.
        state = last_run.get("state")
        return _item(
            "batch_scoring",
            FAILED if state == "failed" else FINISHED,
            since=last_run.get("updated_at"),
            ref={"batch_run": last_run.get("id")},
            detail={
                "stage": last_run.get("stage"),
                "error": str(last_run.get("error"))[:300]
                if last_run.get("error")
                else None,
                **_committed_of(last_run),
            },
        )
    return _item("batch_scoring", NEVER_STARTED)


def _committed_of(run: dict) -> dict:
    """This run's un-ingested committed range — the money already owed Google.

    Same per-leg rule as ``tools.matching.batch_runs.outstanding_committed``: a
    leg with a ``cost_banked_at`` marker is already priced on the ledger, so
    counting its estimate too would double the same money.
    """
    committed = run.get("committed") or {}
    banked = run.get("cost_banked_at") or {}
    low = sum(
        float(e.get("usd_low") or 0.0)
        for leg, e in committed.items()
        if not banked.get(leg)
    )
    high = sum(
        float(e.get("usd_high") or 0.0)
        for leg, e in committed.items()
        if not banked.get(leg)
    )
    return {"committed_usd_low": round(low, 4), "committed_usd_high": round(high, 4)}


def _app_leg_item(
    kind: str,
    *,
    by_status: dict[str, list[dict]],
    active: tuple[str, ...],
    queued: tuple[str, ...],
    done_statuses: tuple[str, ...],
    open_doc: dict | None,
    now: datetime,
) -> dict:
    """``tailoring`` / ``submission``: the application state machine's own record.

    An in-progress status plus its lease is the claim; the status alone is not,
    because a worker killed mid-run leaves the status behind.
    """
    for status in active:
        for app in by_status.get(status, []):
            held = app_state.lease_is_held(app, now=now)
            return _item(
                kind,
                RUNNING if held else STALLED,
                since=_claim_since(app, status),
                ref={"application_id": app.get("id")},
                detail={"status": status},
            )
    for status in queued:
        apps = by_status.get(status) or []
        if apps:
            return _item(
                kind,
                QUEUED,
                since=_claim_since(apps[0], status),
                ref={"application_id": apps[0].get("id")},
                detail={"status": status, "count": len(apps)},
            )
    if open_doc is not None:
        return _item(
            kind,
            _open_state(open_doc, now),
            since=open_doc.get("started_at"),
            ref={"run_id": open_doc.get("run_id")},
        )
    failed = by_status.get("failed") or []
    finished = [a for s in done_statuses for a in by_status.get(s, [])]
    if finished:
        return _item(
            kind,
            FINISHED,
            since=_claim_since(finished[-1], done_statuses[0]),
            detail={"count": len(finished), "failed": len(failed)},
        )
    if failed:
        return _item(
            kind,
            FAILED,
            since=_claim_since(failed[-1], "failed"),
            ref={"application_id": failed[-1].get("id")},
            detail={"count": len(failed), "error": _last_note(failed[-1])},
        )
    return _item(kind, NEVER_STARTED)


def _claim_since(app: dict, status: str) -> str | None:
    """When this application entered ``status``, from its own timeline."""
    for entry in reversed(app.get(app_state.TIMELINE_FIELD) or []):
        if isinstance(entry, dict) and entry.get("status") == status:
            return entry.get("at")
    return None


def _last_note(app: dict) -> str | None:
    for entry in reversed(app.get(app_state.TIMELINE_FIELD) or []):
        if isinstance(entry, dict) and entry.get("note"):
            return str(entry["note"])[:300]
    return None


async def _open_runs(db, user_id: str) -> dict[str, dict]:
    """The user's open ledger docs, newest per leg.

    One equality filter on a subcollection — no composite index. An open doc is
    the only in-flight trace some legs leave.
    """
    newest: dict[str, dict] = {}
    query = (
        db.collection("users")
        .document(user_id)
        .collection(RUNS_COLLECTION)
        .where(filter=FieldFilter("state", "==", LEDGER_RUNNING))
    )
    async for snap in query.stream():
        doc = snap.to_dict() or {}
        doc.setdefault("run_id", snap.id)
        kind = _RUNNER_KINDS.get(doc.get("runner") or "")
        if kind is None:
            continue
        current = newest.get(kind)
        if current is None or _after(doc.get("started_at"), current.get("started_at")):
            newest[kind] = doc
    return newest


def _after(a: Any, b: Any) -> bool:
    left, right = _parse_ts(a), _parse_ts(b)
    if left is None:
        return False
    return right is None or left > right


async def _running_batches(db, user_id: str) -> list[dict]:
    """This user's in-flight batch runs, newest first.

    ``state == "running"`` only: a ``failed`` run is money owed, not liveness,
    and it surfaces under ``committed`` instead. Two equality filters on
    ``batch_runs``; verified live to plan without a composite index.
    """
    runs = []
    query = (
        db.collection(batch_runs.COLLECTION)
        .where(filter=FieldFilter("user_id", "==", user_id))
        .where(filter=FieldFilter("state", "==", "running"))
    )
    async for snap in query.stream():
        doc = snap.to_dict() or {}
        doc["id"] = snap.id
        runs.append(doc)
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return runs


async def _last_batch_run(db, tag: str | None) -> dict | None:
    """One ``batch_runs`` document by id — the tag the last cycle recorded.

    A single ``get``, and only when there is a tag, so the common case costs
    nothing. It is what lets this endpoint tell ``finished`` from ``failed``
    from ``never_started`` for a batch that is no longer running.
    """
    if not tag:
        return None
    snap = await db.collection(batch_runs.COLLECTION).document(tag).get()
    doc = snap.to_dict() or {}
    if not doc:
        return None
    doc.setdefault("id", tag)
    return doc


async def _applications(db, user_id: str) -> dict[str, list[dict]]:
    """Every application, bucketed by status, oldest first within a bucket.

    Unfiltered on purpose: without the terminal statuses there is no way to
    tell ``never_started`` from ``finished``.
    """
    by_status: dict[str, list[dict]] = {}
    query = db.collection("users").document(user_id).collection("applications")
    async for snap in query.stream():
        doc = snap.to_dict() or {}
        doc.setdefault("id", snap.id)
        by_status.setdefault(doc.get(app_state.STATUS_FIELD) or "unknown", []).append(
            doc
        )
    return by_status


@router.get("/activity")
async def get_activity(user_id: str = Depends(verify_user)) -> dict:
    """What is happening for this user, right now, from records only.

    Takes no ``BackgroundTasks`` parameter, ever — see the module docstring and
    ``test_the_activity_route_can_never_schedule_background_work``.
    """
    db = _client()
    now = _now()

    user_doc = (await db.collection("users").document(user_id).get()).to_dict() or {}
    settings = DiscoverySettings.model_validate(
        user_doc.get("discovery_settings") or {}
    )
    dstate = user_doc.get("discovery_state") or {}
    open_runs = await _open_runs(db, user_id)
    batches = await _running_batches(db, user_id)
    by_status = await _applications(db, user_id)
    last_batch = await _last_batch_run(
        db, (dstate.get("last_discovery") or {}).get("batch_run")
    )
    committed = await batch_runs.outstanding_committed(db, user_id=user_id)

    items = [
        _loop_item(
            "discovery",
            enabled=settings.auto_discovery,
            last_at=dstate.get("last_discovery_at"),
            lease=dstate.get("discovery_lease"),
            interval_hours=settings.discovery_interval_hours,
            last_metrics=dstate.get("last_discovery"),
            open_doc=open_runs.get("discovery"),
            now=now,
        ),
        _loop_item(
            "sweep",
            enabled=settings.liveness_sweep,
            last_at=dstate.get("last_sweep_at"),
            lease=dstate.get("sweep_lease"),
            interval_hours=settings.sweep_interval_hours,
            last_metrics=dstate.get("last_sweep"),
            open_doc=open_runs.get("sweep"),
            now=now,
        ),
        _scoring_item(
            auto_discovery=settings.auto_discovery,
            last_discovery=dstate.get("last_discovery"),
            open_doc=open_runs.get("scoring"),
            now=now,
        ),
        _batch_item(
            batches,
            open_runs.get("batch_scoring"),
            last_batch,
            now,
        ),
        _extract_item(user_doc, open_runs.get("extract"), now),
        _app_leg_item(
            "tailoring",
            by_status=by_status,
            active=_TAILORING_ACTIVE,
            queued=("queued",),
            done_statuses=("ready_for_review", "submitted", "responded"),
            open_doc=open_runs.get("tailoring"),
            now=now,
        ),
        _app_leg_item(
            "submission",
            by_status=by_status,
            active=_SUBMISSION_ACTIVE,
            queued=(),
            done_statuses=("submitted", "responded"),
            open_doc=open_runs.get("submission"),
            now=now,
        ),
    ]

    return {
        "polled_at": now.isoformat(),
        "next_tick_at": _next_hour(now),
        "items": items,
        "allowance": _allowance(user_doc, now),
        # Money already owed Google that the ledger has not priced yet. Never
        # summed with actual spend: the two mean different things, and a UI
        # that wants both shows two lines.
        "committed": {
            "usd_low": committed["usd_low"],
            "usd_high": committed["usd_high"],
            "runs": committed["runs"],
        },
    }


def _extract_item(user_doc: dict, open_doc: dict | None, now: datetime) -> dict:
    """Résumé extraction: one shot per user, and the profile is its own record."""
    if open_doc is not None:
        return _item(
            "extract",
            _open_state(open_doc, now),
            since=open_doc.get("started_at"),
            ref={"run_id": open_doc.get("run_id")},
            detail={"trigger": open_doc.get("trigger")},
        )
    if user_doc.get("experience") or user_doc.get("full_name"):
        return _item(
            "extract",
            FINISHED,
            detail={"roles": len(user_doc.get("experience") or [])},
        )
    return _item("extract", NEVER_STARTED)
