# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Auto-discovery settings + scheduler.

The user regulates, from the Profile page, how often the discovery agent finds
new jobs and how often already-discovered postings are re-checked against
their ATS (the liveness sweep). Two triggers drive the loops:

- **Opportunistic ticks**: hot endpoints schedule ``tick_user`` as a background
  task, so cadences are honored whenever the app is in use — no infra needed.
- **``POST /internal/cron/tick``**: a secret-protected endpoint for Cloud
  Scheduler / a GitHub Actions cron, for truly unattended runs while the
  Cloud Run instance is otherwise scaled to zero.

Ticks claim the slot (write ``last_*_at``) before running, so overlapping
triggers never double-run a loop.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from google.cloud import firestore

from api.deps import verify_user
from api.routes.applications import dispatch_tailor
from models.settings import DiscoverySettings
from obs.llm_cost import run_cost_snapshot
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import queues
from tools.applications import reaper
from tools.ats.sweep import sweep_postings
from tools.discovery.pipeline import persist_new_jobs, run_discovery
from tools.discovery.title_filter import load_job_preferences, prefilter_jobs
from tools.matching import batch_runs
from tools.matching.score import score_pending_jobs
from tools.run_costs import persist_run_cost

log = get_logger("api.discovery")

router = APIRouter(tags=["discovery"])

# In-process throttle so polling endpoints don't re-read settings on every hit.
_TICK_CHECK_EVERY = timedelta(minutes=5)
_last_tick_check: dict[str, datetime] = {}

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


def _user_ref(user_id: str):
    return _client().collection("users").document(user_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _due(last_iso: str | None, interval_hours: int, now: datetime) -> bool:
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    return now - last >= timedelta(hours=interval_hours)


def _next_iso(last_iso: str | None, interval_hours: int) -> str | None:
    if not last_iso:
        return None
    try:
        return (
            datetime.fromisoformat(last_iso) + timedelta(hours=interval_hours)
        ).isoformat()
    except ValueError:
        return None


async def run_discovery_cycle(user_id: str, *, trigger: str = "scheduled") -> None:
    """Background: discover new jobs, then score them so they reach the queue.

    Runs under a ``run_id`` log context, so every line the cycle emits — the
    discovery fetches, the per-job ``matching.scored`` events, the summary —
    can be pulled up with ``jsonPayload.run_id="..."`` in Cloud Logging.
    """
    with run_context("auto_discovery", user_id=user_id, trigger=trigger) as run_id:
        started = time.monotonic()
        started_at = _now().isoformat()
        agent_started = log_agent_start(
            log, "discovery", trigger=trigger, user_id=user_id
        )
        counts: dict = {}
        try:
            summary = await run_discovery(user_id)
            # Free title pre-filter: confidently out-of-family jobs never get
            # persisted, so they never cost a Flash parse downstream.
            preferences = await load_job_preferences(user_id)
            jobs, title_dropped = prefilter_jobs(summary["jobs"], preferences)
            new = await persist_new_jobs(jobs)
            # Big backlogs go to a resumable half-price batch run instead of
            # online scoring — but only where the worker's resume ticks exist
            # to ingest it (QUEUE_MODE); in-process mode stays fully online.
            if queues.enabled():
                counts = await batch_runs.score_or_start_run(user_id)
            else:
                counts = await score_pending_jobs(user_id)
            metrics = {
                "run_id": run_id,
                "trigger": trigger,
                "jobs_fetched": len(summary["jobs"]),
                "title_filtered": sum(title_dropped.values()),
                "jobs_by_platform": summary["jobs_by_platform"],
                "boards_failed": len(summary["failures"]),
                "empty_boards": len(summary["empty_boards"]),
                "new_jobs": new,
                "scored": counts["scored"],
                "discarded": counts["discarded"],
                "failed": counts["failed"],
                # What this cycle spent on LLM calls so far. Zero on a batch
                # run: those tokens aren't priced until the worker ingests
                # them, and land on the run ledger doc then.
                "cost_usd": run_cost_snapshot(run_id)["cost_usd"],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            if "batch_run" in counts:
                # Scoring went async: the zeros above are "so far", and this
                # tag finds the run in batch_runs / its logs.
                metrics["batch_run"] = counts["batch_run"]
            # What the scoring budget granted this cycle and what is left —
            # the Profile card reads these off discovery_state. Copied only
            # when present: a run with ignore_budget reports no budget at all
            # rather than a fabricated zero.
            metrics.update({k: v for k, v in counts.items() if k.startswith("budget_")})
            await asyncio.to_thread(
                _user_ref(user_id).set,
                {
                    "discovery_state": {
                        "last_discovery_at": _now().isoformat(),
                        "last_discovery": metrics,
                    }
                },
                merge=True,
            )
            # The one line to watch per auto search: how the run performed.
            log.info("auto_discovery.metrics", **metrics)
            log_agent_end(
                log,
                "discovery",
                agent_started,
                outcome="completed",
                new_jobs=new,
                scored=counts["scored"],
                batch_run=counts.get("batch_run"),
            )
        except Exception:
            log.exception("auto_discovery.failed")
            log_agent_end(log, "discovery", agent_started, outcome="failed")
        finally:
            # In the finally, not the happy path: a cycle that died after
            # scoring still spent the money, and that is exactly the run whose
            # cost someone will come looking for.
            await persist_run_cost(
                _client,
                user_id,
                run_id,
                runner="auto_discovery",
                trigger=trigger,
                started_at=started_at,
                batch_run=counts.get("batch_run"),
                jobs={
                    "pending": counts.get("pending", 0),
                    "scored": counts.get("scored", 0),
                    "discarded": counts.get("discarded", 0),
                    "failed": counts.get("failed", 0),
                },
            )


async def run_sweep_cycle(user_id: str, *, trigger: str = "scheduled") -> None:
    """Background: re-check served postings; dismiss ones the ATS took down."""
    with run_context("liveness_sweep", user_id=user_id, trigger=trigger) as run_id:
        started_at = _now().isoformat()
        started = log_agent_start(log, "sweep", trigger=trigger, user_id=user_id)
        try:
            counts = await sweep_postings(user_id)
            await asyncio.to_thread(
                _user_ref(user_id).set,
                {
                    "discovery_state": {
                        "last_sweep_at": _now().isoformat(),
                        "last_sweep": {**counts, "run_id": run_id, "trigger": trigger},
                    }
                },
                merge=True,
            )
            log_agent_end(log, "sweep", started, outcome="completed", **counts)
        except Exception:
            log.exception("sweep.failed")
            log_agent_end(log, "sweep", started, outcome="failed")
        finally:
            # The sweep is HTTP-only today, so this normally banks a $0 run —
            # which is itself the answer to "did the sweep cost anything?".
            await persist_run_cost(
                _client,
                user_id,
                run_id,
                runner="liveness_sweep",
                trigger=trigger,
                started_at=started_at,
            )


def enqueue_cycle(kind: str, user_id: str, *, trigger: str) -> bool:
    """Push one cycle onto the discovery queue. Returns False when deduped.

    The queue half of :func:`dispatch_cycle`, split out because it is the half
    that is one RPC and needs no event loop: a synchronous caller (the
    onboarding kickoff in ``api.routes.profile``) can enqueue *inside* its
    request instead of deferring the RPC to a background task on an instance
    that may be frozen by then.

    Hour-granular ids for scheduled work — one per user per hour no matter how
    many triggers race — and minute-granular ids for manual runs, so a
    double-click dedupes but a deliberate re-run a minute later doesn't.
    """
    grain = "%Y%m%d%H%M" if trigger == "manual" else "%Y%m%d%H"
    return queues.enqueue(
        "discovery",
        f"/tasks/{kind}",
        {"user_id": user_id, "trigger": trigger},
        task_id=f"{trigger}-{kind}-{user_id}-{_now().strftime(grain)}",
    )


async def dispatch_cycle(kind: str, user_id: str, *, trigger: str) -> bool:
    """Run a discovery/sweep cycle — on the worker via queue when enabled.

    With QUEUE_MODE on, the cycle becomes a named Cloud Tasks task pushed to
    the worker service. Without QUEUE_MODE the cycle runs in-process, exactly
    as before — which is why callers that must not block for the length of a
    whole cycle branch on ``queues.enabled()`` themselves rather than treating
    this as "the cheap one".
    """
    if queues.enabled():
        # Off the event loop: the enqueue is a blocking gRPC call, and this
        # coroutine runs on a worker serving other tasks concurrently.
        return await asyncio.to_thread(enqueue_cycle, kind, user_id, trigger=trigger)
    cycle = run_discovery_cycle if kind == "discovery" else run_sweep_cycle
    await cycle(user_id, trigger=trigger)
    return True


async def tick_user(
    user_id: str, *, force_check: bool = False, doc: dict | None = None
) -> None:
    """Run whichever opted-in loops are due for this user.

    Claims each slot (``last_*_at`` = now) before running so a concurrent tick
    from another trigger sees it as not-due. A failed run therefore waits out
    a full interval instead of retrying hot.

    ``doc`` lets a caller that has already read this user's document hand it
    over instead of paying for a second read — the cron fan-out streams the
    whole ``users`` collection and would otherwise re-fetch every document it
    just had. Safe to pass a moments-old read: nothing here is a compare-and-
    swap, and the dispatch it leads to is deduped by a name the queue owns.
    """
    now = _now()
    last_check = _last_tick_check.get(user_id)
    if not force_check and last_check and now - last_check < _TICK_CHECK_EVERY:
        return
    _last_tick_check[user_id] = now

    if doc is None:
        doc = (await asyncio.to_thread(_user_ref(user_id).get)).to_dict() or {}
    settings = DiscoverySettings.model_validate(doc.get("discovery_settings") or {})
    state = doc.get("discovery_state") or {}

    trigger = "cron" if force_check else "opportunistic"

    if settings.auto_discovery and _due(
        state.get("last_discovery_at"), settings.discovery_interval_hours, now
    ):
        log.info("tick.discovery_due", user_id=user_id, trigger=trigger)
        await asyncio.to_thread(
            _user_ref(user_id).set,
            {"discovery_state": {"last_discovery_at": now.isoformat()}},
            merge=True,
        )
        await dispatch_cycle("discovery", user_id, trigger=trigger)

    if settings.liveness_sweep and _due(
        state.get("last_sweep_at"), settings.sweep_interval_hours, now
    ):
        log.info("tick.sweep_due", user_id=user_id, trigger=trigger)
        await asyncio.to_thread(
            _user_ref(user_id).set,
            {"discovery_state": {"last_sweep_at": now.isoformat()}},
            merge=True,
        )
        await dispatch_cycle("sweep", user_id, trigger=trigger)


@router.get("/settings/discovery")
def get_discovery_settings(
    background_tasks: BackgroundTasks, user_id: str = Depends(verify_user)
) -> dict:
    """Current auto-discovery settings + run state (drives the Profile card)."""
    doc = _user_ref(user_id).get().to_dict() or {}
    settings = DiscoverySettings.model_validate(doc.get("discovery_settings") or {})
    state = doc.get("discovery_state") or {}
    # Opportunistic tick: opening the Profile page keeps the loops honest.
    background_tasks.add_task(tick_user, user_id)
    return {
        "settings": settings.model_dump(),
        "state": state,
        "next_discovery_at": (
            _next_iso(state.get("last_discovery_at"), settings.discovery_interval_hours)
            if settings.auto_discovery
            else None
        ),
        "next_sweep_at": (
            _next_iso(state.get("last_sweep_at"), settings.sweep_interval_hours)
            if settings.liveness_sweep
            else None
        ),
    }


@router.put("/settings/discovery")
def save_discovery_settings(
    body: DiscoverySettings, user_id: str = Depends(verify_user)
) -> dict:
    _user_ref(user_id).set({"discovery_settings": body.model_dump()}, merge=True)
    log.info("discovery_settings.saved", **body.model_dump())
    return {"ok": True}


@router.post("/settings/discovery/run")
async def run_discovery_now(
    background_tasks: BackgroundTasks, user_id: str = Depends(verify_user)
) -> dict:
    """Explicit user action: run discovery + scoring immediately.

    ``mode`` reports where the work actually went — "queued" (Cloud Tasks →
    hermes-worker) or "in_process" (a background task on this instance, which
    scale-down can kill). It is the cheapest way to confirm from outside that a
    deployment's QUEUE_MODE is what you think it is.
    """
    log.info("discovery.run_now", user_id=user_id)
    if queues.enabled():
        queued = await dispatch_cycle("discovery", user_id, trigger="manual")
        return {"ok": True, "mode": "queued", "deduped": not queued}
    # No queue infra: run in-process, after the response goes out.
    background_tasks.add_task(run_discovery_cycle, user_id, trigger="manual")
    return {"ok": True, "mode": "in_process"}


@router.post("/settings/discovery/sweep")
async def run_sweep_now(
    background_tasks: BackgroundTasks, user_id: str = Depends(verify_user)
) -> dict:
    """Explicit user action: run the liveness sweep immediately."""
    log.info("sweep.run_now", user_id=user_id)
    if queues.enabled():
        queued = await dispatch_cycle("sweep", user_id, trigger="manual")
        return {"ok": True, "mode": "queued", "deduped": not queued}
    background_tasks.add_task(run_sweep_cycle, user_id, trigger="manual")
    return {"ok": True, "mode": "in_process"}


async def reap_user(user_id: str, *, background_tasks: BackgroundTasks) -> dict:
    """One reaper pass for this user: collect applications whose worker died.

    Unlike discovery and the sweep this is not on a per-user cadence and takes
    no slot claim. It costs one indexless Firestore query and no LLM call, and
    the thing it recovers from — an instance killed mid-run — is not something a
    user opts into. A document it does move gets a lease that keeps the next
    tick off it, so "every hour, for everyone" cannot become a retry storm.

    ``asyncio.to_thread`` because ``tools.applications.state`` is synchronous by
    design; the same hop ``tick_user`` makes for its own Firestore writes.

    The dispatcher is bound to *this* request's ``background_tasks`` so the
    in-process path still works with ``QUEUE_MODE`` off. Under QUEUE_MODE —
    every deployment that has a worker — ``dispatch_tailor`` enqueues and the
    object is never touched.
    """
    return await asyncio.to_thread(
        reaper.reap_applications,
        user_id,
        dispatch=lambda uid, job_id: dispatch_tailor(
            uid, job_id, background_tasks=background_tasks
        ),
    )


@router.post("/internal/cron/tick")
async def cron_tick(
    background_tasks: BackgroundTasks,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """External scheduler entry point (Cloud Scheduler / GH Actions cron).

    Ticks every user; per-user settings decide whether anything actually runs.
    On the worker service no app-level guard is needed: the service is
    private, so Cloud Run has already verified the scheduler's OIDC token.
    Elsewhere (public hermes-api) the ``CRON_SECRET`` header guards it —
    unset disables the endpoint.

    **Under QUEUE_MODE the fan-out is in-request.** This endpoint's real home is
    hermes-worker, which does *not* run with ``cpu-throttling: false``, so
    anything deferred past the response runs on an instance that may already be
    frozen — the hourly tick would then fire for nobody, which is the same shape
    of bug as "discovery never runs". With a queue a tick is a settings read
    plus an enqueue, so it costs the request nothing to do it properly.

    Without a queue it stays a background task: there, a due tick runs the whole
    discovery-and-scoring cycle, which is minutes of work per user and cannot
    happen inside an HTTP request.

    One user's failure never costs the rest theirs — the loop logs and carries
    on rather than abandoning the fan-out mid-way, which is what an exception
    escaping a background task did before. But **swallowing every failure and
    answering 200 would be worse than the bug above**: with, say,
    ``TASKS_SA_EMAIL`` unset, every tick raises, the scheduler sees success, and
    the hourly loops are dead with nothing to alert on. The count comes back in
    the response, and a fan-out where *nothing* got through answers 5xx so the
    scheduler's own retry and alerting are worth something.
    """
    if not queues.worker_mode():
        secret = os.getenv("CRON_SECRET")
        if not secret:
            raise HTTPException(status_code=503, detail="cron not configured")
        if x_cron_secret != secret:
            raise HTTPException(status_code=403, detail="forbidden")
    # The documents, not just the ids: tick_user needs each user's settings and
    # this stream has already paid for them.
    users = await asyncio.to_thread(
        lambda: [
            (snap.id, snap.to_dict() or {})
            for snap in _client().collection("users").stream()
        ]
    )
    inline = queues.enabled()
    failed = 0
    reaped = 0
    reap_failed = 0
    reap_truncated = 0
    for uid, doc in users:
        if not inline:
            background_tasks.add_task(tick_user, uid, force_check=True, doc=doc)
            # Appending to the collection that is already being iterated when
            # this runs, which is how dispatch_tailor's in-process fallback
            # reaches the loop at all. A background task added mid-iteration is
            # still picked up.
            background_tasks.add_task(reap_user, uid, background_tasks=background_tasks)
            continue
        try:
            await tick_user(uid, force_check=True, doc=doc)
        except Exception:
            failed += 1
            log.exception("cron.tick_failed", user_id=uid)
        # Its own try/except, *inside* the per-user one: a reaper that throws
        # for every user must not turn a fan-out whose discovery ticks all
        # worked into the 5xx below. It is reported on its own counter instead,
        # which is the thing to alert on.
        try:
            tally = await reap_user(uid, background_tasks=background_tasks)
            reaped += tally["recovered"]
            # A pass that ran out of its per-tick budget looks exactly like a
            # pass with nothing to do. Carried up so it can be alerted on: it is
            # the signal that a backlog is draining slower than it accumulates.
            reap_truncated += tally.get("truncated", 0)
        except Exception:
            reap_failed += 1
            log.exception("cron.reap_failed", user_id=uid)
    await asyncio.to_thread(maybe_enqueue_batch_resume)
    log.info(
        "cron.tick",
        users=len(users),
        failed=failed,
        reaped=reaped,
        reap_failed=reap_failed,
        reap_truncated=reap_truncated,
        inline=inline,
    )
    if users and failed == len(users):
        # Nothing ticked. Almost always environmental (credentials, queue
        # config), so let the scheduler retry it and let it be visible as a
        # failing job rather than an hourly 200 that does nothing.
        raise HTTPException(
            status_code=500, detail=f"every tick failed ({failed} users)"
        )
    return {
        "ok": True,
        "users": len(users),
        "failed": failed,
        # Additive to the contract PR C established. The reaper is the one loop
        # here whose *inaction* is invisible — a stuck application looks like an
        # idle one — so the count comes back rather than living only in logs.
        "reaped": reaped,
        "reap_failed": reap_failed,
        "reap_truncated": reap_truncated,
    }


def maybe_enqueue_batch_resume() -> bool:
    """Queue one batch-runs resume pass if any run is in flight.

    A queue task (not a background task here) so the ingest work gets its own
    request lifetime and Cloud Tasks retries; the hour-granular name dedupes
    it against scheduler retries. Never lets a failure here break the tick.
    """
    if not (queues.worker_mode() and queues.enabled()):
        return False
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        running = (
            _client()
            .collection("batch_runs")
            .where(filter=FieldFilter("state", "==", "running"))
            .limit(1)
            .get()
        )
        if not running:
            return False
        return queues.enqueue(
            "score",
            "/tasks/batch/resume",
            {},
            task_id=f"batch-resume-{_now().strftime('%Y%m%d%H')}",
        )
    except Exception:
        log.exception("cron.batch_resume_enqueue_failed")
        return False
