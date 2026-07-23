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

import os
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from google.cloud import firestore

from api.deps import verify_user
from models.settings import DiscoverySettings
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import queues
from tools.ats.sweep import sweep_postings
from tools.discovery.pipeline import persist_new_jobs, run_discovery
from tools.discovery.title_filter import load_job_preferences, prefilter_jobs
from tools.matching import batch_runs
from tools.matching.score import score_pending_jobs

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
        agent_started = log_agent_start(
            log, "discovery", trigger=trigger, user_id=user_id
        )
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
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            if "batch_run" in counts:
                # Scoring went async: the zeros above are "so far", and this
                # tag finds the run in batch_runs / its logs.
                metrics["batch_run"] = counts["batch_run"]
            _user_ref(user_id).set(
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


async def run_sweep_cycle(user_id: str, *, trigger: str = "scheduled") -> None:
    """Background: re-check served postings; dismiss ones the ATS took down."""
    with run_context("liveness_sweep", user_id=user_id, trigger=trigger) as run_id:
        started = log_agent_start(log, "sweep", trigger=trigger, user_id=user_id)
        try:
            counts = await sweep_postings(user_id)
            _user_ref(user_id).set(
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


async def dispatch_cycle(kind: str, user_id: str, *, trigger: str) -> bool:
    """Run a discovery/sweep cycle — on the worker via queue when enabled.

    With QUEUE_MODE on, the cycle becomes a named Cloud Tasks task pushed to
    the worker service: hour-granular ids for scheduled work (one per user
    per hour no matter how many triggers race) and minute-granular ids for
    manual runs (double-click dedupe). Returns False when the queue deduped.
    Without QUEUE_MODE the cycle runs in-process, exactly as before.
    """
    if queues.enabled():
        now = _now()
        grain = "%Y%m%d%H%M" if trigger == "manual" else "%Y%m%d%H"
        return queues.enqueue(
            "discovery",
            f"/tasks/{kind}",
            {"user_id": user_id, "trigger": trigger},
            task_id=f"{trigger}-{kind}-{user_id}-{now.strftime(grain)}",
        )
    cycle = run_discovery_cycle if kind == "discovery" else run_sweep_cycle
    await cycle(user_id, trigger=trigger)
    return True


async def tick_user(user_id: str, *, force_check: bool = False) -> None:
    """Run whichever opted-in loops are due for this user.

    Claims each slot (``last_*_at`` = now) before running so a concurrent tick
    from another trigger sees it as not-due. A failed run therefore waits out
    a full interval instead of retrying hot.
    """
    now = _now()
    last_check = _last_tick_check.get(user_id)
    if not force_check and last_check and now - last_check < _TICK_CHECK_EVERY:
        return
    _last_tick_check[user_id] = now

    doc = _user_ref(user_id).get().to_dict() or {}
    settings = DiscoverySettings.model_validate(doc.get("discovery_settings") or {})
    state = doc.get("discovery_state") or {}

    trigger = "cron" if force_check else "opportunistic"

    if settings.auto_discovery and _due(
        state.get("last_discovery_at"), settings.discovery_interval_hours, now
    ):
        log.info("tick.discovery_due", user_id=user_id, trigger=trigger)
        _user_ref(user_id).set(
            {"discovery_state": {"last_discovery_at": now.isoformat()}}, merge=True
        )
        await dispatch_cycle("discovery", user_id, trigger=trigger)

    if settings.liveness_sweep and _due(
        state.get("last_sweep_at"), settings.sweep_interval_hours, now
    ):
        log.info("tick.sweep_due", user_id=user_id, trigger=trigger)
        _user_ref(user_id).set(
            {"discovery_state": {"last_sweep_at": now.isoformat()}}, merge=True
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
    """Explicit user action: run discovery + scoring immediately."""
    log.info("discovery.run_now", user_id=user_id)
    if queues.enabled():
        queued = await dispatch_cycle("discovery", user_id, trigger="manual")
        return {"ok": True, "deduped": not queued}
    # No queue infra: run in-process, after the response goes out.
    background_tasks.add_task(run_discovery_cycle, user_id, trigger="manual")
    return {"ok": True}


@router.post("/settings/discovery/sweep")
async def run_sweep_now(
    background_tasks: BackgroundTasks, user_id: str = Depends(verify_user)
) -> dict:
    """Explicit user action: run the liveness sweep immediately."""
    log.info("sweep.run_now", user_id=user_id)
    if queues.enabled():
        queued = await dispatch_cycle("sweep", user_id, trigger="manual")
        return {"ok": True, "deduped": not queued}
    background_tasks.add_task(run_sweep_cycle, user_id, trigger="manual")
    return {"ok": True}


@router.post("/internal/cron/tick")
def cron_tick(
    background_tasks: BackgroundTasks,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """External scheduler entry point (Cloud Scheduler / GH Actions cron).

    Ticks every user; per-user settings decide whether anything actually runs.
    On the worker service no app-level guard is needed: the service is
    private, so Cloud Run has already verified the scheduler's OIDC token.
    Elsewhere (public hermes-api) the ``CRON_SECRET`` header guards it —
    unset disables the endpoint.
    """
    if not queues.worker_mode():
        secret = os.getenv("CRON_SECRET")
        if not secret:
            raise HTTPException(status_code=503, detail="cron not configured")
        if x_cron_secret != secret:
            raise HTTPException(status_code=403, detail="forbidden")
    users = [snap.id for snap in _client().collection("users").stream()]
    for uid in users:
        background_tasks.add_task(tick_user, uid, force_check=True)
    maybe_enqueue_batch_resume()
    log.info("cron.tick", users=len(users))
    return {"ok": True, "users": len(users)}


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
