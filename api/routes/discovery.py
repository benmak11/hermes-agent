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

**A tick leases the slot; the cycle's own success write releases it.** The
slot is claimed by ``last_*_at``, and that field is written by
:func:`run_discovery_cycle` / :func:`run_sweep_cycle` when the work has
actually happened. A tick used to write it *before* dispatching, which made a
run that died indistinguishable from one that succeeded — the user then waited
out a full ``discovery_interval_hours`` before anything retried. The lease
covers only the gap in between: it stops a second tick dispatching on top of a
live run, expires on its own if the run is killed silently, and is dropped
immediately if the run fails loudly, so the next hourly tick retries.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore

from api.deps import dev_mode, firebase_auth, verify_user
from api.routes.applications import dispatch_tailor
from models.settings import DiscoverySettings
from obs.llm_cost import run_cost_snapshot
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import allowlist, queues
from tools.account.delete import is_deleted
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
# Deliberately per-instance and not moved to Firestore: it is a throttle, not a
# lock — correctness against multiple Cloud Run instances is the slot lease's
# job (_LEASE_SECONDS below), and the worst a missed throttle costs is one extra
# settings read. Decided and closed in Phase 3; please don't re-litigate.
_TICK_CHECK_EVERY = timedelta(minutes=5)
_last_tick_check: dict[str, datetime] = {}

# ``write_option`` is a *staticmethod* factory: it builds a precondition and
# never touches a client or the network. Bound once, the way
# ``tools.applications.state`` binds it, so the compare-and-swaps below need no
# client instance to construct one.
_precondition = firestore.Client.write_option

#: Seconds a slot lease stays valid, and **the inequality that makes it a
#: lock**: a lease must outlive the longest run it guards.
#:
#: Derived from the dispatch deadline rather than restated, for the reason
#: ``tools.applications.state._LEASE_SECONDS`` spells out. Cloud Tasks abandons
#: a dispatch at ``_DISPATCH_DEADLINE_SECONDS`` (1800), so 1800s is the longest
#: a queued cycle can still be running *as far as the queue is concerned* — and
#: that is the load-bearing anchor here, because it is the one that lives in
#: this repo. hermes-worker's ``timeoutSeconds`` is also 1800, but it is a Cloud
#: Run value set by hand and represented in no terraform CI would check; if it
#: were raised, the queue would abandon and possibly redeliver at 1800s anyway,
#: which is a duplicate-dispatch exposure this module already had and does not
#: add to.
#:
#: A lease *shorter* than the run is not a weaker lock, it is **no lock**: it is
#: guaranteed to have expired before the run could possibly have finished, so
#: every tick in the meantime reads it as permission. This project has already
#: shipped that bug once (a 1200s lease over 1800s of work, PR B). The grace is
#: added on top of the deadline, never subtracted from it.
#:
#: **This bounds run duration and nothing else**, which is why the lease is
#: re-stamped when the run starts — see :func:`_extend_slot`. Sizing it to also
#: cover an unbounded queue wait would mean a silently dead run held its slot
#: for that whole span.
#:
#: **Deliberately not a heartbeat.** Recovery latency here is bounded by the
#: hourly Cloud Scheduler tick, not by this TTL: a lease that lapses at T+1860
#: and one a dead heartbeat frees at T+1830 are collected by the same next tick,
#: so a heartbeat would be observably identical while having to be threaded
#: through the whole body of ``run_discovery_cycle``.
_LEASE_GRACE_SECONDS = 60
_LEASE_SECONDS = queues._DISPATCH_DEADLINE_SECONDS + _LEASE_GRACE_SECONDS

#: Per loop: the ``discovery_state`` field whose timestamp *is* the slot claim,
#: and the field holding the lease that covers the gap before it is written.
_SLOTS: dict[str, tuple[str, str]] = {
    "discovery": ("last_discovery_at", "discovery_lease"),
    "sweep": ("last_sweep_at", "sweep_lease"),
}

_CRON_TRIGGER = "cron"
_OPPORTUNISTIC_TRIGGER = "opportunistic"

#: The triggers :func:`tick_user` dispatches under, and therefore the only ones
#: that arrive holding a lease. ``manual`` (``POST /settings/discovery/run``)
#: and ``onboarding`` (the kickoff in ``api.routes.profile``) go straight to
#: :func:`dispatch_cycle` and take no slot at all, so a failing one of those
#: must not release a *scheduled* run's lease out from under it.
SLOT_TRIGGERS = frozenset({_CRON_TRIGGER, _OPPORTUNISTIC_TRIGGER})

#: The one way to run the real pipeline from a developer's machine anyway.
#: Deliberately a second, explicit variable rather than "unset AUTH_DEV_MODE":
#: unsetting the bypass also takes away the way you were calling the API, so the
#: cheapest route around the guard would have been to delete it.
LIVE_RUN_OVERRIDE = "ALLOW_LIVE_RUNS"

LIVE_RUN_REFUSED = (
    "refusing to start a live discovery run from a local process: "
    f"AUTH_DEV_MODE is on. Set {LIVE_RUN_OVERRIDE}=1 to override."
)


def live_runs_refused() -> bool:
    """Would starting a real crawl here be a local process driving production?

    **This has happened.** On 2026-08-23 a local harness posted to
    ``POST /settings/discovery/run`` through FastAPI's ``TestClient``, which runs
    ``background_tasks`` *synchronously* — so the call did not schedule a crawl
    for later, it ran one: ~110s against production on ADC credentials, 8,469
    junk jobs written to a real user, ~$0.50-1.00 spent. Phase 1's budget cap
    bounded the damage; nothing prevented the trigger.

    The signal is ``api.deps.dev_mode()`` — ``AUTH_DEV_MODE=1`` — because that is
    what actually tells the two cases apart. There is one GCP project and it is
    production, so "am I pointed at production?" is always yes and cannot
    discriminate; what a deployed revision never has is this variable, which
    Terraform does not set. So: dev bypass on ⇒ local process ⇒ the pipeline it
    would drive is the real one, and it needs to be asked for by name.

    Not applied to the queued path *versus* the in-process one, but to both: an
    enqueue from a laptop with ``QUEUE_MODE=1`` hands the same crawl to the real
    worker, which is the same spend one process further away.
    """
    if os.getenv(LIVE_RUN_OVERRIDE, "").strip().lower() in {"1", "true", "on"}:
        return False
    return dev_mode()


_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db


#: An async client, purely for :func:`_allowlisted` — everything else in this
#: module is sync-plus-``asyncio.to_thread``, but ``tools.allowlist.is_allowed``
#: is async (it is shared with ``api.deps``, which runs on an event loop with
#: no thread hop to spare). Memoising it is safe for the reason ``_client``
#: above and ``api.routes.account``'s async client both are: one uvicorn loop
#: for the life of the process. Built at all only when ``cron_tick`` runs with
#: ``ALLOWLIST_ENFORCED`` on.
_adb: firestore.AsyncClient | None = None


def _async_client() -> firestore.AsyncClient:
    global _adb
    if _adb is None:
        _adb = firestore.AsyncClient()
    return _adb


def _user_ref(user_id: str):
    return _client().collection("users").document(user_id)


def _now() -> datetime:
    return datetime.now(UTC)


async def _allowlisted(user_id: str) -> bool:
    """Is this user's Auth email an active allowlist seat?

    Reached only from :func:`cron_tick`'s fan-out, and only while
    ``tools.allowlist.enforced()`` is on. That endpoint is the one caller that
    reaches a user without ever passing through ``verify_user`` — it streams
    every document in ``users`` — which is exactly why PR C's
    :func:`is_deleted` check lives at this same seam: a de-allowlisted user's
    background loops have to stop *here*, or removing them from the allowlist
    bounds none of their spend.

    Reads the Auth email, never ``users/{uid}.email`` — same reasoning as
    everywhere else this PR touches: the profile field is résumé-extracted and
    is not guaranteed to be the login address, where the Auth email is what
    :func:`tools.allowlist.is_allowed` is keyed on. ``get_user`` is a blocking
    Firebase Admin call, off the event loop via ``asyncio.to_thread``; a
    lookup that fails for any reason (the account is gone, the Admin SDK is
    unreachable) reads as "not allowed" — the same fail-closed bias
    :func:`tools.allowlist.is_allowed` documents for itself.
    """
    try:
        record = await asyncio.to_thread(firebase_auth().get_user, user_id)
    except Exception as e:
        log.info("cron.allowlist_lookup_failed", user_id=user_id, error=str(e))
        return False
    return await allowlist.is_allowed(_async_client(), record.email)


async def _account_deleted(user_id: str) -> bool:
    """Has this user deleted their account? One read, on the cycle's own thread.

    ``tick_user`` and ``cron_tick`` already hold the user document and check
    :func:`is_deleted` directly; the cycles do not, because they are reached
    from the worker's ``/tasks/*`` handlers with nothing but a user id — so a
    deletion that lands while a task sits in the queue is only visible here.
    One extra ``get`` per cycle, against a crawl that costs a hundred HTTP
    fetches and a Gemini call per job.

    Deliberately not exception-handled, unlike :func:`_extend_slot` and
    :func:`_release_slot`. Those are bookkeeping *after* a run has been
    committed to, and a cycle must not die because its bookkeeping did; this is
    a precondition *before* one, like :func:`_claim_slot`, which also lets a
    failed read propagate. A raise here costs a retry of a run that has not
    happened and spent nothing. Swallowing it would instead mean treating an
    unreadable document as "not deleted" — the guard turning itself off exactly
    when Firestore is unhappy.
    """
    snap = await asyncio.to_thread(_user_ref(user_id).get)
    return is_deleted(snap.to_dict())


def _parse_ts(value) -> datetime | None:
    """A stored timestamp as an aware datetime, or ``None`` if unusable."""
    if isinstance(value, datetime):  # Firestore hands timestamps back as these
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _lease(now: datetime) -> dict:
    """A fresh slot lease for a run starting at ``now``."""
    return {
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=_LEASE_SECONDS)).isoformat(),
    }


def _lease_at(lease, key: str) -> datetime | None:
    """One of a lease's timestamps, or ``None`` if there is no usable one."""
    return _parse_ts(lease.get(key)) if isinstance(lease, dict) else None


def _lease_held(lease, now: datetime) -> bool:
    """Is a run holding this loop's slot right now? Pure.

    **The bias is the opposite of ``tools.applications.state.lease_is_held``'s,
    deliberately.** There an unreadable lease counts as *held*, because refusing
    to claim only wedges one document while claiming anyway risks a duplicate
    real job application. Here nothing reaps a slot: a lease read as held is
    never dispatched against, so nothing ever runs to clear it, and this user's
    loops stop **permanently** — which is precisely the "discovery never runs"
    failure this module exists to prevent. Reading a corrupt lease as free costs
    at most one extra cycle and overwrites the corrupt value on the way past.
    """
    expiry = _lease_at(lease, "expires_at")
    return expiry is not None and expiry > now


def _due(
    last_iso: str | None, interval_hours: int, now: datetime, *, lease=None
) -> bool:
    """Is this loop's interval up *and* its slot free?

    A live lease is not-due however old ``last_iso`` is: it means a cycle is in
    flight that has not yet written its ``last_*_at``. Both halves are only
    advisory at the call sites — the authoritative copy of this check runs
    inside :func:`_claim_slot`, against the snapshot the claim is conditioned
    on.
    """
    if _lease_held(lease, now):
        return False
    last = _parse_ts(last_iso)
    if last is None:
        return True
    return now - last >= timedelta(hours=interval_hours)


def _next_iso(
    last_iso: str | None,
    interval_hours: int,
    *,
    lease=None,
    now: datetime | None = None,
) -> str | None:
    """The Profile card's "next run at", counted from the last *successful* run.

    A held lease has to be folded in or the card reads as broken. ``last_*_at``
    is now written only when a cycle succeeds, so while a run is in flight the
    last success is by definition more than an interval old and the naive answer
    is a "next run" in the past. Counting from the moment the slot was claimed
    restores exactly what the old pre-claim displayed, and degrades honestly at
    both ends: a run that succeeds moves the answer by its own duration, and a
    run that fails drops its lease and the card goes back to saying the loop is
    due now — which it is.
    """
    now = now or _now()
    since = _parse_ts(last_iso)
    if _lease_held(lease, now):
        acquired = _lease_at(lease, "acquired_at") or now
        if since is None or acquired > since:
            since = acquired
    if since is None:
        return None
    return (since + timedelta(hours=interval_hours)).isoformat()


def _claim_slot(user_id: str, kind: str, interval_hours: int, now: datetime) -> bool:
    """Compare-and-swap this loop's lease. ``True`` iff this caller took it.

    Synchronous, reached through ``asyncio.to_thread`` — the hop ``tick_user``
    already makes for its Firestore writes — and modelled on
    ``tools.applications.state.try_claim_lease``: read a snapshot, decide
    against *that* snapshot, then write with ``last_update_time=`` so the write
    fails if anything touched the document in between.

    **Both preconditions are re-checked here, against this read.** The screen in
    :func:`tick_user` runs on a document a caller may have handed over seconds
    ago, and reads a lease another tick may have taken since — filtering outside
    the swap is not a compare-and-swap, which is the bug every PR of this phase
    has contained. Re-checking the *interval* matters as much as re-checking the
    lease: a tick holding a stale document would otherwise dispatch a second
    cycle in the window after the first one succeeded, wrote a fresh
    ``last_*_at``, and released.

    ``dispatch_cycle``'s hour-granular task ids do not make this redundant. They
    dedupe one ``(trigger, kind, user, hour)`` — so two cron ticks in the same
    hour collapse — but a cron tick and the opportunistic tick from
    ``jobs.list_pending_jobs`` carry *different* triggers and both get through,
    as do two ticks straddling an hour boundary; and with ``QUEUE_MODE`` off
    there is no queue and therefore no name to dedupe on at all.
    """
    field, lease_field = _SLOTS[kind]
    ref = _user_ref(user_id)
    snap = ref.get()
    for attempt in (0, 1):
        if not snap.exists:
            return False
        state = (snap.to_dict() or {}).get("discovery_state") or {}
        if not _due(
            state.get(field), interval_hours, now, lease=state.get(lease_field)
        ):
            log.info("tick.slot_taken", user_id=user_id, kind=kind, attempt=attempt)
            return False
        try:
            # A dotted path, not a nested map: ``update`` replaces a map value
            # wholesale, and ``discovery_state`` also holds the last run's
            # metrics and the *other* loop's slot.
            ref.update(
                {f"discovery_state.{lease_field}": _lease(now)},
                option=_precondition(last_update_time=snap.update_time),
            )
            return True
        except NotFound:
            return False
        except FailedPrecondition:
            if attempt:
                log.warning("tick.slot_contended", user_id=user_id, kind=kind)
                return False
            # One retry, for the reason ``state.try_claim_lease`` takes one:
            # ``tools.matching.budget`` reserves out of this very document in a
            # transaction, so a scoring run in flight bumps its update_time
            # without going anywhere near the slot.
            snap = ref.get()
    return False  # pragma: no cover - the loop always returns


def _extend_slot(user_id: str, kind: str, trigger: str, began: datetime) -> bool:
    """Re-stamp this loop's lease against the moment the run actually started.

    **The claim and the run do not start at the same time.** ``tick_user`` takes
    the lease *before* ``enqueue_cycle``, but :data:`_LEASE_SECONDS` is derived
    from the dispatch deadline, which bounds how long a run may take — not how
    long it may sit in a queue first. So a claim stamped at dispatch time is
    spending its TTL on queue wait, and the shortfall is real rather than
    theoretical: the ``discovery`` queue allows 3 concurrent dispatches and the
    worker runs at ``containerConcurrency = 1`` across five queues, so a
    fan-out where several users come due at one tick can leave the last of them
    waiting tens of minutes. Its lease would then lapse *mid-run*, and because
    the next hourly cron is a different task name nothing would dedupe the
    second dispatch — a duplicate paid cycle, and a regression against the old
    pre-claim, which held for a full interval however long the queue was.

    Re-stamping here is what ``tools.applications.state.try_claim_lease`` gets
    for free by being taken on the worker: the TTL starts when the work does.
    The tick-side claim is still needed and still does its own job — it is what
    stops two ticks *dispatching* — so this extends that claim rather than
    replacing it.

    Unconditional, and deliberately not a compare-and-swap. A re-stamp that lost
    a race would leave the lease too short, which is the bug being fixed; and
    there is nothing to protect, because a dotted-path write touches this slot
    and nothing else. Gated on :data:`SLOT_TRIGGERS` for the same reason
    :func:`_release_slot` is: a manual or onboarding run holds no slot, and
    stamping one would lock scheduled ticks out of a cadence it never joined.

    Never raises — a cycle must not die because its bookkeeping did.
    """
    if trigger not in SLOT_TRIGGERS:
        return False
    _, lease_field = _SLOTS[kind]
    try:
        _user_ref(user_id).update({f"discovery_state.{lease_field}": _lease(began)})
        return True
    except Exception:
        # Including NotFound: no document means no slot to hold, and the cycle
        # is about to discover that for itself.
        log.exception("tick.lease_extend_failed", user_id=user_id, kind=kind)
        return False


def _release_slot(user_id: str, kind: str, trigger: str, began: datetime) -> bool:
    """Hand back a lease **this** run holds, leaving the schedule alone.

    The loud-failure counterpart to :func:`_claim_slot`. A cycle that raised has
    written no ``last_*_at``, so its lease is the only thing keeping the next
    tick off the slot — and the run is over. Leaving it there makes a failure
    cost a lease TTL of silence on top of the failure itself.

    **Conditional, where the success path is not, and that asymmetry is the
    point.** A successful cycle clears its lease inside the same unconditional
    ``set`` that writes ``last_*_at``: even if that clear frees a *successor's*
    lease, the fresh timestamp landing beside it holds the slot shut for a whole
    interval (the shortest offered is 6h, against a 31-minute lease), so nothing
    is actually unlocked and the write must never be allowed to lose. A failure
    writes no timestamp, so the lease is all there is, and freeing one that
    isn't ours would put two cycles on one user.

    Two things say whose it is, without a token to pass across the queue
    boundary. ``trigger`` says a tick dispatched this run at all — see
    :data:`SLOT_TRIGGERS`. ``acquired_at <= began`` says the lease on the
    document is still the one that dispatch came from: this run's own claim was
    taken before it started, and a successor's can only have been taken after.

    Never raises: it is called from an ``except`` block, and must not replace
    the failure the cycle already logged or skip the cost flush behind it.
    """
    if trigger not in SLOT_TRIGGERS:
        return False
    _, lease_field = _SLOTS[kind]
    try:
        ref = _user_ref(user_id)
        snap = ref.get()
        for attempt in (0, 1):
            if not snap.exists:
                return False
            state = (snap.to_dict() or {}).get("discovery_state") or {}
            acquired = _lease_at(state.get(lease_field), "acquired_at")
            if acquired is None or acquired > began:
                # Gone already, unreadable, or a successor's. Letting it expire
                # costs one hourly tick; stealing it costs a duplicate cycle.
                log.info("tick.lease_not_ours", user_id=user_id, kind=kind)
                return False
            try:
                ref.update(
                    {f"discovery_state.{lease_field}": firestore.DELETE_FIELD},
                    option=_precondition(last_update_time=snap.update_time),
                )
                log.info("tick.lease_released", user_id=user_id, kind=kind)
                return True
            except NotFound:
                return False
            except FailedPrecondition:
                if attempt:
                    log.warning(
                        "tick.lease_release_contended", user_id=user_id, kind=kind
                    )
                    return False
                snap = ref.get()
    except Exception:
        log.exception("tick.lease_release_failed", user_id=user_id, kind=kind)
    return False


async def run_discovery_cycle(user_id: str, *, trigger: str = "scheduled") -> None:
    """Background: discover new jobs, then score them so they reach the queue.

    Runs under a ``run_id`` log context, so every line the cycle emits — the
    discovery fetches, the per-job ``matching.scored`` events, the summary —
    can be pulled up with ``jsonPayload.run_id="..."`` in Cloud Logging.

    **The chokepoint for :func:`live_runs_refused`.** The manual route refuses
    early so the caller gets a 403 instead of silence, but every other way into
    a live crawl lands here — the opportunistic tick that ``GET
    /settings/discovery`` schedules, ``cron_tick``'s fan-out, the onboarding
    kickoff, the worker's ``/tasks/*`` handlers — and under ``TestClient`` a
    background task is not "later", it is now. Refusing before the first
    ``_extend_slot`` write means a refused run touches no Firestore either.

    **And the chokepoint for a deleted account**, for the same reason: a task
    already on the queue when the user deleted themselves arrives here holding
    nothing but a user id. The refusal is ahead of every write this function
    makes, which matters more here than anywhere else — the success write below
    is a ``set(..., merge=True)`` that would **recreate** the deleted user
    document, and ``persist_new_jobs`` would refill the subcollection under it.
    That is also this guard's honest limit: it stops a cycle that has not
    started, not one already past this line. See
    :func:`tools.account.delete.delete_account`.
    """
    if live_runs_refused():
        log.warning("discovery.cycle_refused", user_id=user_id, trigger=trigger)
        return
    if await _account_deleted(user_id):
        log.warning("discovery.cycle_account_deleted", user_id=user_id, trigger=trigger)
        return
    with run_context("auto_discovery", user_id=user_id, trigger=trigger) as run_id:
        started = time.monotonic()
        began = _now()
        started_at = began.isoformat()
        agent_started = log_agent_start(
            log, "discovery", trigger=trigger, user_id=user_id
        )
        counts: dict = {}
        # Before any work: the tick's claim has been paying for queue wait, and
        # from here the TTL has to cover the run.
        await asyncio.to_thread(_extend_slot, user_id, "discovery", trigger, began)
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
                # What the shared board cache absorbed. Both are 0 while
                # BOARD_CACHE_TTL_SECONDS is unset; once ops flips it, this is
                # where the effect becomes visible per cycle.
                "boards_cached": summary["boards_cached"],
                "boards_fetched": summary["boards_fetched"],
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
                        # **This write is the slot claim**, and it lands only
                        # now that the work is done — a tick used to write the
                        # timestamp before dispatching, which made a run that
                        # died look exactly like one that succeeded. The lease
                        # is released in the same write because the timestamp
                        # beside it holds the slot for a whole interval, so
                        # there is nothing left for the lease to protect.
                        "discovery_lease": firestore.DELETE_FIELD,
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
            # A run that fails *loudly* is over, and it wrote no
            # ``last_discovery_at`` — so its lease is all that stands between
            # this user and the next tick, and holding it buys nothing but
            # silence. In the ``except`` and **not** the ``finally``, which is
            # the whole distinction: a worker killed mid-cycle raises
            # ``CancelledError``, which is not an ``Exception`` and never
            # reaches here, so a run that dies *silently* — and may still be
            # running — leaves its lease to expire on the clock instead.
            #
            # **No backoff, decided rather than overlooked.** Waiting out a full
            # interval after a failure was an accidental circuit breaker in the
            # old pre-claim, and dropping it does raise the re-run rate of a
            # deterministically failing cycle. Under QUEUE_MODE — every
            # deployment that has a worker — the hour-granular task names cap
            # that at two dispatches an hour, which is the retry this PR is
            # for. The 5-minute storm is reachable only with QUEUE_MODE off,
            # where the cycle runs in-process: local and dev. What actually
            # bounds the spend either way is ``tools.matching.budget``'s daily
            # cap, which is the guard built for that job. A backoff lease would
            # also have to be told apart from a run lease in ``_next_iso``, or
            # the Profile card would advertise a next run a day out when it is
            # minutes away — a lease taxonomy for a dev-only exposure.
            await asyncio.to_thread(_release_slot, user_id, "discovery", trigger, began)
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
    """Background: re-check served postings; dismiss ones the ATS took down.

    Refuses from a local process for the same reason discovery does — see
    :func:`live_runs_refused`. The sweep buys no LLM calls, so this is not
    about spend: it writes ``user_decision: dismissed`` onto real jobs and
    moves real applications to ``posting_removed``, and a laptop pointed at
    production should not be able to retire a user's queue by accident.
    Refusing before the ``_extend_slot`` write means a refused sweep also
    leaves no lease behind.

    Refuses on a deleted account too — see :func:`run_discovery_cycle`. The
    sweep's success write is the same recreating ``set(..., merge=True)``, so a
    swept deleted account would leave a ``users/{uid}`` document behind holding
    nothing but discovery state.
    """
    if live_runs_refused():
        log.warning("sweep.cycle_refused", user_id=user_id, trigger=trigger)
        return
    if await _account_deleted(user_id):
        log.warning("sweep.cycle_account_deleted", user_id=user_id, trigger=trigger)
        return
    with run_context("liveness_sweep", user_id=user_id, trigger=trigger) as run_id:
        began = _now()
        started_at = began.isoformat()
        started = log_agent_start(log, "sweep", trigger=trigger, user_id=user_id)
        await asyncio.to_thread(_extend_slot, user_id, "sweep", trigger, began)
        try:
            counts = await sweep_postings(user_id)
            await asyncio.to_thread(
                _user_ref(user_id).set,
                {
                    "discovery_state": {
                        "last_sweep_at": _now().isoformat(),
                        "last_sweep": {**counts, "run_id": run_id, "trigger": trigger},
                        # The slot claim, written now that the sweep has run —
                        # see run_discovery_cycle for why the lease goes with it.
                        "sweep_lease": firestore.DELETE_FIELD,
                    }
                },
                merge=True,
            )
            log_agent_end(log, "sweep", started, outcome="completed", **counts)
        except Exception:
            log.exception("sweep.failed")
            log_agent_end(log, "sweep", started, outcome="failed")
            await asyncio.to_thread(_release_slot, user_id, "sweep", trigger, began)
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

    **Leases each slot rather than claiming it.** The slot itself is claimed by
    ``last_*_at``, which the cycle writes when the work has actually happened;
    this used to be written here, before dispatching, so a run that died was
    indistinguishable from one that succeeded and the user waited out a full
    interval before anything retried. The lease covers only the gap in between.

    ``doc`` lets a caller that has already read this user's document hand it
    over instead of paying for a second read — the cron fan-out streams the
    whole ``users`` collection and would otherwise re-fetch every document it
    just had. It stays safe to pass a moments-old read, but for a different
    reason than before: it is now only a **screen**. Nothing is dispatched off
    it; a loop it says is due goes on to :func:`_claim_slot`, which re-reads and
    re-checks both the interval and the lease inside a compare-and-swap. So the
    hand-over still saves the read on the overwhelmingly common not-due path,
    and the rare due path pays one read to get a precondition worth having.
    """
    now = _now()
    last_check = _last_tick_check.get(user_id)
    if not force_check and last_check and now - last_check < _TICK_CHECK_EVERY:
        return
    _last_tick_check[user_id] = now

    if doc is None:
        doc = (await asyncio.to_thread(_user_ref(user_id).get)).to_dict() or {}
    if is_deleted(doc):
        # Nothing here is free: a claimed slot is a write, and a dispatched
        # cycle is a crawl. Checked on the document the caller already has.
        log.info("tick.account_deleted", user_id=user_id)
        return
    settings = DiscoverySettings.model_validate(doc.get("discovery_settings") or {})
    state = doc.get("discovery_state") or {}

    trigger = _CRON_TRIGGER if force_check else _OPPORTUNISTIC_TRIGGER

    if (
        settings.auto_discovery
        and _due(
            state.get("last_discovery_at"),
            settings.discovery_interval_hours,
            now,
            lease=state.get("discovery_lease"),
        )
        and await asyncio.to_thread(
            _claim_slot, user_id, "discovery", settings.discovery_interval_hours, now
        )
    ):
        # Logged after the claim, not before it: this line means a cycle was
        # dispatched, and a tick that loses the swap dispatches nothing.
        log.info("tick.discovery_due", user_id=user_id, trigger=trigger)
        await dispatch_cycle("discovery", user_id, trigger=trigger)

    if (
        settings.liveness_sweep
        and _due(
            state.get("last_sweep_at"),
            settings.sweep_interval_hours,
            now,
            lease=state.get("sweep_lease"),
        )
        and await asyncio.to_thread(
            _claim_slot, user_id, "sweep", settings.sweep_interval_hours, now
        )
    ):
        log.info("tick.sweep_due", user_id=user_id, trigger=trigger)
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
        # The lease goes in: ``last_*_at`` now moves only on success, so a run
        # in flight would otherwise leave the card advertising a next run in
        # the past.
        "next_discovery_at": (
            _next_iso(
                state.get("last_discovery_at"),
                settings.discovery_interval_hours,
                lease=state.get("discovery_lease"),
            )
            if settings.auto_discovery
            else None
        ),
        "next_sweep_at": (
            _next_iso(
                state.get("last_sweep_at"),
                settings.sweep_interval_hours,
                lease=state.get("sweep_lease"),
            )
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

    Refuses outright from a local process — see :func:`live_runs_refused`. The
    check is the *first* thing here, ahead of the QUEUE_MODE branch, because
    both arms of it spend the same money: one on this instance, one on the
    worker.
    """
    if live_runs_refused():
        log.warning("discovery.run_now_refused", user_id=user_id)
        raise HTTPException(status_code=403, detail=LIVE_RUN_REFUSED)
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
    """Explicit user action: run the liveness sweep immediately.

    Refused from a local process, and — as on the discovery route — refused
    *before* the ``queues.enabled()`` branch: enqueueing from a laptop hands
    the same production writes to the real worker one process further away.
    """
    if live_runs_refused():
        log.warning("sweep.run_now_refused", user_id=user_id)
        raise HTTPException(status_code=403, detail=LIVE_RUN_REFUSED)
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

    Ticks every user; per-user settings decide whether anything actually runs,
    and a tombstoned account (``deleted_at``) is skipped whole. On the worker
    service no app-level guard is needed: the service is
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
    deleted = 0
    not_allowlisted = 0
    enforce_allowlist = allowlist.enforced()
    for uid, doc in users:
        if is_deleted(doc):
            # This fan-out is the one caller that reaches a user without ever
            # passing ``verify_user``: it streams *every* document in ``users``,
            # so a tombstoned account with a wipe still in flight (or one whose
            # wipe failed partway) is picked up here and nowhere else. Skipped
            # whole — not just the tick, but the reaper too, which would
            # otherwise dispatch tailoring for a user who is leaving.
            deleted += 1
            continue
        if enforce_allowlist and not await _allowlisted(uid):
            # Same seam, same reason, same shape as the ``is_deleted`` skip
            # above: a de-allowlisted user's background loops must stop here,
            # or removing them from the allowlist doesn't bound their spend.
            # Off entirely while ALLOWLIST_ENFORCED is unset — this whole
            # branch never runs on the shipped state of this PR.
            not_allowlisted += 1
            continue
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
        deleted=deleted,
        not_allowlisted=not_allowlisted,
        reaped=reaped,
        reap_failed=reap_failed,
        reap_truncated=reap_truncated,
        inline=inline,
    )
    # Against the users this tick actually *tried*, not the collection size: a
    # tombstoned account is skipped by design, and counting it as a success
    # would mask a fan-out where every real user's tick failed. A
    # de-allowlisted user is skipped the same way and for the same reason —
    # always 0 while ALLOWLIST_ENFORCED is off.
    attempted = len(users) - deleted - not_allowlisted
    if attempted and failed == attempted:
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
        # Tombstoned accounts, skipped whole. Additive, and worth its own
        # counter: a fan-out that suddenly ticks nobody should be readable as
        # "everyone deleted themselves" rather than "the loop is broken".
        "deleted": deleted,
        # Seat-revoked accounts, skipped whole — see ``deleted`` above for the
        # identical reasoning. Always 0 while ALLOWLIST_ENFORCED is off.
        "not_allowlisted": not_allowlisted,
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
