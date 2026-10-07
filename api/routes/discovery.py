# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Auto-discovery settings + scheduler.

The user sets, from the Profile page, how often the discovery agent finds new
jobs and how often already-discovered postings are re-checked against their ATS
(the liveness sweep). Two triggers drive the loops: opportunistic ticks, where
hot endpoints schedule ``tick_user`` as a background task, and
``POST /internal/cron/tick`` for unattended runs.

A tick leases the slot; the cycle's own success write releases it. The slot is
claimed by ``last_*_at``, written by :func:`run_discovery_cycle` /
:func:`run_sweep_cycle` only once the work has actually happened, so a run that
died is never mistaken for one that succeeded. The lease covers the gap in
between: it stops a second tick dispatching on top of a live run, expires on
its own if the run is killed silently, and is dropped if the run fails loudly
so the next hourly tick retries.

Discovery cycles commit real Gemini spend.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore

from api.deps import (
    SpendConfirm,
    dev_mode,
    firebase_auth,
    spend_402,
    spend_client,
    verify_user,
)
from api.routes.applications import dispatch_tailor
from models.settings import DiscoverySettings
from obs.llm_cost import run_cost_snapshot
from obs.logging import get_logger, log_agent_end, log_agent_start, run_context
from tools import allowlist, queues, spend
from tools.account.delete import is_deleted
from tools.applications import reaper
from tools.ats.sweep import sweep_postings
from tools.discovery import budget as discovery_budget
from tools.discovery.pipeline import persist_new_jobs, run_discovery
from tools.discovery.title_filter import load_job_preferences, prefilter_jobs
from tools.matching import batch_runs
from tools.matching.score import count_unscored, score_pending_jobs
from tools.run_costs import DONE, FAILED, RUNNING, open_run, persist_run_cost

log = get_logger("api.discovery")

router = APIRouter(tags=["discovery"])

# In-process throttle so polling endpoints don't re-read settings on every hit.
# Per-instance on purpose: it is a throttle, not a lock — correctness across
# Cloud Run instances is the slot lease's job (_LEASE_SECONDS below).
_TICK_CHECK_EVERY = timedelta(minutes=5)
_last_tick_check: dict[str, datetime] = {}

# ``write_option`` is a staticmethod factory: it builds a precondition and never
# touches a client or the network, so the compare-and-swaps below need no client
# instance to construct one.
_precondition = firestore.Client.write_option

#: Seconds a slot lease stays valid. The inequality that makes it a lock: a
#: lease must outlive the longest run it guards, so it is derived from Cloud
#: Tasks' ``_DISPATCH_DEADLINE_SECONDS`` (1800) with the grace added on top,
#: never subtracted. A lease shorter than the run is not a weaker lock but no
#: lock at all — it has expired before the run could have finished, so every
#: tick in the meantime reads it as permission (shipped once, as a 1200s lease
#: over 1800s of work).
#:
#: This bounds run duration and nothing else, which is why the lease is
#: re-stamped when the run starts — see :func:`_extend_slot`. Covering an
#: unbounded queue wait too would mean a silently dead run held its slot for
#: that whole span.
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
#: that arrive holding a lease. ``manual`` and ``onboarding`` go straight to
#: :func:`dispatch_cycle` and take no slot, so a failure on one of those must
#: not release a scheduled run's lease out from under it.
SLOT_TRIGGERS = frozenset({_CRON_TRIGGER, _OPPORTUNISTIC_TRIGGER})

_MANUAL_TRIGGER = "manual"
_ONBOARDING_TRIGGER = "onboarding"

#: The triggers whose dispatch charged a run against the weekly allowance, and
#: therefore the only ones a pre-work refusal inside the cycle may refund.
#: Every other trigger never took a run, and crediting one back would hand out
#: an allowance nobody spent — the one direction a cap must not fail in.
#:
#: The charge lives at the three dispatch sites — :func:`tick_user` once its
#: ``_claim_slot`` has won, :func:`run_discovery_now`, and the onboarding
#: kickoff in ``api.routes.profile`` — and not in the worker's
#: ``/tasks/discovery*`` handlers, where a redelivery would charge twice for
#: one user-visible search.
CHARGED_TRIGGERS = SLOT_TRIGGERS | {_MANUAL_TRIGGER, _ONBOARDING_TRIGGER}

#: The one way to run the real pipeline from a developer's machine anyway. A
#: separate variable rather than "unset AUTH_DEV_MODE", because unsetting the
#: bypass also takes away the way you were calling the API.
LIVE_RUN_OVERRIDE = "ALLOW_LIVE_RUNS"

LIVE_RUN_REFUSED = (
    "refusing to start a live discovery run from a local process: "
    f"AUTH_DEV_MODE is on. Set {LIVE_RUN_OVERRIDE}=1 to override."
)


def refuse_live_runs() -> None:
    """Dependency form of :func:`live_runs_refused`, for paid routes.

    Exists for ordering: a route carrying the consent seam as a dependency has
    its single-use token consumed while FastAPI is still solving parameters, so
    a check in the handler body would 403 after spending the confirmation.
    Route-level ``dependencies=[...]`` run ahead of the signature's own, so this
    refuses with the token intact. Pinned in ``test_jobs_score_route.py``.
    """
    if live_runs_refused():
        raise HTTPException(status_code=403, detail=LIVE_RUN_REFUSED)


def live_runs_refused() -> bool:
    """Would starting a real crawl here be a local process driving production?

    The guard exists because it happened: a local harness once ran a real
    production crawl, writing thousands of junk jobs and spending money.

    The signal is ``api.deps.dev_mode()`` (``AUTH_DEV_MODE=1``), because there
    is one GCP project and it is production — "am I pointed at production?"
    cannot discriminate, but a deployed revision never has this variable.
    Applies to the queued path as well as the in-process one: an enqueue from a
    laptop hands the same spend to the real worker.
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


#: An async client, for the two natively async things here (:func:`_allowlisted`
#: and :func:`_backlog`); everything else is sync plus ``asyncio.to_thread``.
#: Memoising is safe because there is one uvicorn loop for the process's life.
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


def _state_paths(*fields: str) -> list[str]:
    """``merge=`` field paths for a ``discovery_state`` success write.

    A path list rather than ``merge=True``: Firestore merges nested maps leaf
    by leaf, so a run's metrics map would keep every key the previous run
    wrote and this one did not. Listed paths are replaced whole; every other
    ``discovery_state`` field is left alone.
    """
    return [f"discovery_state.{field}" for field in fields]


async def _allowlisted(user_id: str) -> bool:
    """Is this user's Auth email an active allowlist seat?

    Reached only from :func:`cron_tick`'s fan-out, which is the one caller that
    reaches a user without passing through ``verify_user`` — a de-allowlisted
    user's background loops have to stop here or removing their seat bounds
    none of their spend.

    Reads the Auth email, never ``users/{uid}.email``: the profile field is
    résumé-extracted and may not be the login address, which is what
    :func:`tools.allowlist.is_allowed` is keyed on. Any failed lookup reads as
    "not allowed" — fail closed.
    """
    try:
        record = await asyncio.to_thread(firebase_auth().get_user, user_id)
    except Exception as e:
        log.info("cron.allowlist_lookup_failed", user_id=user_id, error=str(e))
        return False
    return await allowlist.is_allowed(_async_client(), record.email)


async def _account_deleted(user_id: str) -> bool:
    """Has this user deleted their account? One read, on the cycle's own thread.

    The cycles reach the worker's ``/tasks/*`` handlers with nothing but a user
    id, so a deletion that lands while a task sits in the queue is only visible
    here.

    Not exception-handled, unlike :func:`_extend_slot` and :func:`_release_slot`:
    this is a precondition before a run, so a raise costs a retry of work that
    never happened, where swallowing would read an unreadable document as "not
    deleted" and turn the guard off exactly when Firestore is unhappy.
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

    An unreadable lease reads as *free* here — the opposite bias to
    ``tools.applications.state.lease_is_held``. Nothing reaps a slot, so a lease
    read as held is never dispatched against and this user's loops would stop
    permanently; reading a corrupt lease as free costs at most one extra cycle
    and overwrites the corrupt value on the way past.
    """
    expiry = _lease_at(lease, "expires_at")
    return expiry is not None and expiry > now


def _due(
    last_iso: str | None, interval_hours: int, now: datetime, *, lease=None
) -> bool:
    """Is this loop's interval up *and* its slot free?

    A live lease is not-due however old ``last_iso`` is: a cycle is in flight
    that has not yet written its ``last_*_at``. Advisory at the call sites —
    the authoritative check runs inside :func:`_claim_slot`, against the
    snapshot the claim is conditioned on.
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

    A held lease has to be folded in or the card reads as broken: ``last_*_at``
    moves only on success, so while a run is in flight the last success is more
    than an interval old and the naive answer is a "next run" in the past.
    Counting from the moment the slot was claimed degrades honestly at both
    ends — a failed run drops its lease and the card goes back to "due now".
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

    Synchronous, reached through ``asyncio.to_thread``: read a snapshot, decide
    against *that* snapshot, then write with ``last_update_time=`` so the write
    fails if anything touched the document in between.

    Both preconditions — interval and lease — are re-checked here against this
    read. The screen in :func:`tick_user` runs on a document that may be seconds
    old, and filtering outside the swap is not a compare-and-swap. Re-checking
    the interval matters as much as the lease: a tick holding a stale document
    would otherwise dispatch a second cycle in the window after the first
    succeeded, wrote a fresh ``last_*_at`` and released.

    ``dispatch_cycle``'s hour-granular task ids do not make this redundant: they
    dedupe one ``(trigger, kind, user, hour)``, so a cron tick and an
    opportunistic tick both get through, as do two ticks straddling an hour
    boundary, and with ``QUEUE_MODE`` off there is no name to dedupe on at all.
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
            # One retry: ``tools.matching.budget`` reserves out of this very
            # document in a transaction, so a scoring run in flight bumps its
            # update_time without going anywhere near the slot.
            snap = ref.get()
    return False  # pragma: no cover - the loop always returns


def _extend_slot(user_id: str, kind: str, trigger: str, began: datetime) -> bool:
    """Re-stamp this loop's lease against the moment the run actually started.

    The claim and the run do not start at the same time: ``tick_user`` takes the
    lease before ``enqueue_cycle``, but :data:`_LEASE_SECONDS` bounds run
    duration, not queue wait. Queue wait can be tens of minutes, so a lease
    stamped at dispatch time can lapse mid-run and nothing would dedupe the
    second dispatch — a duplicate paid cycle. Re-stamping here starts the TTL
    when the work does, extending the tick-side claim rather than replacing it.

    Unconditional, not a compare-and-swap: a re-stamp that lost a race would
    leave the lease too short, which is the bug being fixed, and a dotted-path
    write touches this slot and nothing else. Gated on :data:`SLOT_TRIGGERS` —
    a manual or onboarding run holds no slot, and stamping one would lock
    scheduled ticks out of a cadence it never joined.

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

    The loud-failure counterpart to :func:`_claim_slot`. A cycle that raised
    wrote no ``last_*_at``, so its lease is the only thing keeping the next tick
    off the slot, and leaving it there costs a lease TTL of silence.

    Conditional, where the success path is not. A successful cycle clears its
    lease in the same unconditional ``set`` that writes ``last_*_at``, and the
    fresh timestamp holds the slot shut for a whole interval anyway. A failure
    writes no timestamp, so the lease is all there is and freeing one that isn't
    ours would put two cycles on one user. Two things say whose it is:
    ``trigger`` (see :data:`SLOT_TRIGGERS`) says a tick dispatched this run at
    all, and ``acquired_at <= began`` says the lease on the document is still
    the one that dispatch came from — a successor's can only be later.

    Never raises: called from an ``except`` block, it must not replace the
    failure the cycle already logged or skip the cost flush behind it.
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


def _allowance_left(user_id: str, doc: dict) -> bool:
    """Has this user a search left this week? A screen, on a document the caller
    already holds — no read, no write, no counter moved.

    Ordered ahead of :func:`_claim_slot` so a capped user costs nothing: a lease
    taken and handed back is two writes plus a window in which a second tick
    sees the slot as busy. The reservation that binds is taken after the claim.
    """
    left = discovery_budget.remaining(doc.get(discovery_budget.FIELD))
    # ``None`` is the kill switch, not "zero left": with the cap off every
    # tick passes the screen. Checked explicitly because ``None > 0`` is a
    # TypeError and ``not None`` would read as capped — the wrong direction.
    if left is None or left > 0:
        return True
    log.info("tick.discovery_capped", user_id=user_id)
    return False


def _cap_429(runs_this_week: int, limits: discovery_budget.Limits) -> HTTPException:
    """The weekly-cap refusal.

    429, never 402: 402 means "confirm the spend" to this client and hands back
    a token that makes the action go through. A cap is not a price — there is
    no token, and only time changes the answer.
    """
    return HTTPException(
        status_code=429,
        detail={
            "reason": "discovery_cap",
            "runs_this_week": runs_this_week,
            "per_week": limits.per_week,
            # An ISO *instant*. The client renders it; a server that formatted
            # a local time would have to know the viewer's timezone.
            "resets_at": discovery_budget.resets_at(_now(), discovery_budget.UTC_TZ),
        },
    )


async def _refund_run(user_id: str, trigger: str, *, week: str | None = None) -> None:
    """Hand back a charged run that never became work. Never raises.

    Only for pre-work outcomes — a queue dedupe, a refused live run, a deleted
    account. A crawl that ran and then failed keeps its charge; the next
    scheduled tick is the retry.

    ``week`` defaults to the current key rather than the reservation's, because
    the cycle-side callers run on the worker with nothing but a user id;
    ``apply_release`` credits a counter only while it still describes the
    window the run was taken from.
    """
    if trigger not in CHARGED_TRIGGERS:
        return
    await discovery_budget.release(
        _async_client(),
        user_id,
        1,
        week=week or discovery_budget.week_key(_now(), discovery_budget.UTC_TZ),
    )


async def _backlog(user_id: str) -> int | None:
    """The unscored backlog, or ``None`` when it could not be counted.

    Never raises: it runs inside the cycle's ``try``, where an exception would
    mark a run that already did its work — and may already have paid for
    scoring — as failed. ``None`` rather than 0 because a fabricated zero
    claims "nothing is waiting".
    """
    try:
        return await count_unscored(_async_client(), user_id)
    except Exception:
        log.exception("discovery.backlog_count_failed", user_id=user_id)
        return None


async def run_discovery_cycle(
    user_id: str, *, trigger: str = "scheduled", score: bool = True
) -> None:
    """Background: discover new jobs, and (unless ``score`` is off) score them.

    Scoring spends real money on Gemini; finding jobs is free. ``score``
    separates the two, and only the manual path uses it: a "run now" without
    scoring consent stops at the persist and the card offers a second, priced
    click. Unattended triggers (``cron``, ``opportunistic``, ``onboarding``)
    keep ``score=True`` — nobody is there to make the second click, and the
    auto-discovery toggle is the consent.

    Runs under a ``run_id`` log context, so every line the cycle emits can be
    pulled up with ``jsonPayload.run_id="..."`` in Cloud Logging.

    The chokepoint for :func:`live_runs_refused` and for a deleted account:
    every other way into a live crawl lands here holding nothing but a user id,
    and under ``TestClient`` a background task runs immediately rather than
    later. Both refusals are ahead of every write this function makes, which
    matters because the success write is a merging ``set`` that would
    recreate a deleted user document and ``persist_new_jobs`` would refill the
    subcollection under it. The guard stops a cycle that has not started, not
    one already past this line.
    """
    if live_runs_refused():
        log.warning("discovery.cycle_refused", user_id=user_id, trigger=trigger)
        await _refund_run(user_id, trigger)
        return
    if await _account_deleted(user_id):
        log.warning("discovery.cycle_account_deleted", user_id=user_id, trigger=trigger)
        # Safe against the resurrection hazard: the refund is a
        # read-modify-write that declines when there is no counter to credit,
        # and a deleted account's document is gone with it.
        await _refund_run(user_id, trigger)
        return
    with run_context(
        "auto_discovery", user_id=user_id, trigger=trigger, scored=score
    ) as run_id:
        started = time.monotonic()
        began = _now()
        started_at = began.isoformat()
        agent_started = log_agent_start(
            log, "discovery", trigger=trigger, user_id=user_id
        )
        counts: dict = {}
        # ``running`` until some leg below decides otherwise. A cycle killed by
        # CancelledError or SIGKILL reaches neither branch, so it banks
        # ``running`` and the activity contract derives ``stalled`` from its age.
        ledger_state = RUNNING
        # Before any work: the tick's claim has been paying for queue wait, and
        # from here the TTL has to cover the run.
        await asyncio.to_thread(_extend_slot, user_id, "discovery", trigger, began)
        # The liveness record, before the first fetch, so "is discovery
        # running?" is answerable from the server.
        await open_run(
            _client,
            user_id,
            run_id,
            runner="auto_discovery",
            trigger=trigger,
            started_at=started_at,
        )
        try:
            summary = await run_discovery(user_id)
            # Free title pre-filter: confidently out-of-family jobs never get
            # persisted, so they never cost a Flash parse downstream.
            preferences = await load_job_preferences(user_id)
            jobs, title_dropped = prefilter_jobs(summary["jobs"], preferences)
            new = await persist_new_jobs(jobs)
            if not score:
                # The find-only leg. Nothing below this line may reach an LLM;
                # the counts are zeros-because-nothing-ran, not zeros-so-far.
                counts = {"scored": 0, "discarded": 0, "failed": 0}
                log.info("discovery.found_only", user_id=user_id, new_jobs=new)
            # Big backlogs go to a resumable half-price batch run instead of
            # online scoring — but only where the worker's resume ticks exist
            # to ingest it (QUEUE_MODE); in-process mode stays fully online.
            elif queues.enabled():
                counts = await batch_runs.score_or_start_run(user_id)
            else:
                counts = await score_pending_jobs(user_id)
            metrics = {
                "run_id": run_id,
                "trigger": trigger,
                # Whether this cycle was allowed to spend at all, so that "0
                # scored" does not read as a failure rather than as a run that
                # was never asked to score.
                "scored_leg": score,
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
                # The backlog and nothing else: jobs the user has not decided on
                # that nothing has scored, counted the same way by every branch
                # from the one definition in
                # ``tools.matching.score.count_unscored``. Not grant-bounded —
                # what a priced click covers is ``min(this, the grant)``, and
                # the grant is the estimate's job.
                "unscored_backlog": await _backlog(user_id),
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
                        # This write is the slot claim, and it lands only now
                        # that the work is done, so a run that died never looks
                        # like one that succeeded. The lease goes in the same
                        # write: the timestamp beside it holds the slot for a
                        # whole interval, so nothing is left to protect.
                        "discovery_lease": firestore.DELETE_FIELD,
                    }
                },
                merge=_state_paths(
                    "last_discovery_at", "last_discovery", "discovery_lease"
                ),
            )
            # A cycle that handed its scoring to a Vertex batch is not over: the
            # worker's resume ticks ingest the results under this same run_id
            # and that ingest closes the doc.
            ledger_state = RUNNING if counts.get("batch_run") else DONE
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
            ledger_state = FAILED
            log.exception("auto_discovery.failed")
            log_agent_end(log, "discovery", agent_started, outcome="failed")
            # A run that fails loudly is over and wrote no ``last_discovery_at``,
            # so holding its lease buys nothing but silence. In the ``except``
            # and not the ``finally``, deliberately: a worker killed mid-cycle
            # raises ``CancelledError``, which never reaches here, so a run that
            # dies silently — and may still be running — leaves its lease to
            # expire on the clock instead.
            #
            # No backoff, decided rather than overlooked. Under QUEUE_MODE the
            # hour-granular task names cap retries at two dispatches an hour;
            # with QUEUE_MODE off (local and dev only) spend is bounded by
            # ``tools.matching.budget``'s daily cap.
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
                # ``started_at`` is not re-sent: ``open_run`` wrote it, and the
                # close must not move it.
                state=ledger_state,
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

    Refuses from a local process (see :func:`live_runs_refused`) — not for
    spend, since the sweep buys no LLM calls, but because it writes
    ``user_decision: dismissed`` onto real jobs and moves real applications to
    ``posting_removed``. Refuses on a deleted account too: the success write is
    a recreating merging ``set``. Both refusals precede
    ``_extend_slot``, so a refused sweep leaves no lease behind either.
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
        ledger_state = RUNNING
        await asyncio.to_thread(_extend_slot, user_id, "sweep", trigger, began)
        await open_run(
            _client,
            user_id,
            run_id,
            runner="liveness_sweep",
            trigger=trigger,
            started_at=started_at,
        )
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
                merge=_state_paths("last_sweep_at", "last_sweep", "sweep_lease"),
            )
            ledger_state = DONE
            log_agent_end(log, "sweep", started, outcome="completed", **counts)
        except Exception:
            ledger_state = FAILED
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
                state=ledger_state,
            )


#: The find-only discovery task's own worker route, deliberately a separate
#: path rather than a field on the existing task: during a rollout Cloud Tasks
#: still delivers to the old revision, which would drop an unknown
#: ``{"score": false}`` and score the backlog anyway — a guard failing open on
#: every deploy. An old worker 404s this path instead and the retry lands once
#: the new revision is serving, so nothing is spent in the window.
SCAN_TASK_PATH = "/tasks/discovery/scan"


def enqueue_cycle(kind: str, user_id: str, *, trigger: str, score: bool = True) -> bool:
    """Push one cycle onto the discovery queue. Returns False when deduped.

    The queue half of :func:`dispatch_cycle`, split out because it is one RPC
    and needs no event loop: a synchronous caller (the onboarding kickoff in
    ``api.routes.profile``) can enqueue inside its request rather than defer to
    a background task on an instance that may be frozen by then.

    Hour-granular ids for scheduled work and minute-granular ids for manual
    runs, so a double-click dedupes but a deliberate re-run a minute later
    does not. ``score=False`` routes to :data:`SCAN_TASK_PATH` and takes its
    own slice of the id namespace — a find-only run and a scored run in the
    same minute are different asks.
    """
    grain = "%Y%m%d%H%M" if trigger == "manual" else "%Y%m%d%H"
    scan = kind == "discovery" and not score
    path = SCAN_TASK_PATH if scan else f"/tasks/{kind}"
    verb = f"{kind}-scan" if scan else kind
    return queues.enqueue(
        "discovery",
        path,
        {"user_id": user_id, "trigger": trigger},
        task_id=f"{trigger}-{verb}-{user_id}-{_now().strftime(grain)}",
    )


async def dispatch_cycle(
    kind: str, user_id: str, *, trigger: str, score: bool = True
) -> bool:
    """Run a discovery/sweep cycle — on the worker via queue when enabled.

    With QUEUE_MODE on, the cycle becomes a named Cloud Tasks task pushed to
    the worker. Without it the cycle runs in-process, so callers that must not
    block for a whole cycle branch on ``queues.enabled()`` themselves rather
    than treating this as the cheap call.
    """
    if queues.enabled():
        # Off the event loop: the enqueue is a blocking gRPC call, and this
        # coroutine runs on a worker serving other tasks concurrently.
        return await asyncio.to_thread(
            enqueue_cycle, kind, user_id, trigger=trigger, score=score
        )
    if kind == "discovery":
        await run_discovery_cycle(user_id, trigger=trigger, score=score)
    else:
        # The sweep buys no LLM calls, so it has no scoring leg to separate.
        await run_sweep_cycle(user_id, trigger=trigger)
    return True


async def tick_user(
    user_id: str, *, force_check: bool = False, doc: dict | None = None
) -> None:
    """Run whichever opted-in loops are due for this user. A due discovery loop
    dispatches a cycle that spends real money on Gemini.

    Leases each slot rather than claiming it: the slot itself is claimed by
    ``last_*_at``, which the cycle writes once the work has actually happened,
    and the lease covers only the gap in between.

    ``doc`` lets a caller that already read this user's document hand it over
    instead of paying for a second read (the cron fan-out streams the whole
    ``users`` collection). A moments-old read is safe because this is only a
    screen — nothing is dispatched off it, and a loop it says is due goes on to
    :func:`_claim_slot`, which re-reads inside a compare-and-swap.
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
        and _allowance_left(user_id, doc)
        and await asyncio.to_thread(
            _claim_slot, user_id, "discovery", settings.discovery_interval_hours, now
        )
    ):
        # Charged here, once the claim has won: the screen above runs on a
        # document that may be seconds old, and this is on the dispatch side so
        # a redelivery of the queued task cannot charge a second time.
        reservation = await discovery_budget.reserve(_async_client(), user_id)
        if reservation.granted <= 0:
            # Lost the race between the screen and the reservation. Give the
            # lease straight back — holding it would keep the next tick off a
            # slot this one is not going to use.
            await asyncio.to_thread(_release_slot, user_id, "discovery", trigger, now)
        else:
            # Logged after the claim, not before it: this line means a cycle was
            # dispatched, and a tick that loses the swap dispatches nothing.
            log.info("tick.discovery_due", user_id=user_id, trigger=trigger)
            try:
                dispatched = await dispatch_cycle("discovery", user_id, trigger=trigger)
            except Exception:
                # A charge whose dispatch never happened: ``queues.enqueue``
                # raises on a Cloud Tasks 503 or a config problem, and left
                # unrefunded the hourly ticks would spend the whole weekly
                # allowance on zero searches.
                #
                # Only where the dispatch really is just an enqueue. With no
                # queue ``dispatch_cycle`` *is* the cycle, so a raise there is a
                # crawl that ran and failed, which keeps its charge and
                # releases its own lease in ``run_discovery_cycle``.
                if queues.enabled():
                    await _refund_run(user_id, trigger, week=reservation.week_key)
                    await asyncio.to_thread(
                        _release_slot, user_id, "discovery", trigger, now
                    )
                raise
            if not dispatched:
                # Deduped by the queue: no crawl will happen under this charge.
                await _refund_run(user_id, trigger, week=reservation.week_key)

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
    background_tasks: BackgroundTasks,
    body: SpendConfirm | None = None,
    user_id: str = Depends(verify_user),
) -> dict:
    """Explicit user action: find new jobs now — and score them only if asked.

    Scoring spends real money, so the default verb is the free one: without
    ``confirm`` the cycle runs with ``score=False`` and the response says
    ``scored: false``, and the card then quotes the scoring step for a second,
    deliberate click. A ``confirm`` that is present but invalid answers 402
    rather than quietly downgrading to find-only — the user asked for the paid
    thing. (This route once meant "find and score" unconditionally and could
    submit a Vertex batch with no estimate and no confirmation.)

    ``mode`` reports where the work went: "queued" (Cloud Tasks → hermes-worker)
    or "in_process" (a background task on this instance, which scale-down can
    kill).

    Refuses from a local process (see :func:`live_runs_refused`) as the very
    first thing, ahead of the consent seam and the QUEUE_MODE branch, because
    every arm spends the same money. Answers 429, not 402, once the week's
    searches are gone — see :func:`_cap_429`.
    """
    if live_runs_refused():
        log.warning("discovery.run_now_refused", user_id=user_id)
        raise HTTPException(status_code=403, detail=LIVE_RUN_REFUSED)

    # Screened before the consent seam, and it gates the free verb too: finding
    # jobs costs no money but does cost a search, and screening first means a
    # 429 never burns a confirmation the user would have to mint again.
    adb = _async_client()
    limits = discovery_budget.Limits.from_env()
    snap = await adb.collection("users").document(user_id).get()
    state = (snap.to_dict() or {}).get(discovery_budget.FIELD) or {}
    left = discovery_budget.remaining(state, limits=limits)
    # ``None`` means the cap is off — never 429 in that case.
    if left is not None and left <= 0:
        log.info("discovery.run_now_capped", user_id=user_id)
        raise _cap_429(discovery_budget.used(state), limits)

    score = False
    if body is not None and body.confirm:
        db = spend_client()
        try:
            await spend.consume(db, user_id, spend.DISCOVERY_SCAN, body.confirm)
        except spend.ConsentRequired:
            raise await spend_402(db, user_id, spend.DISCOVERY_SCAN) from None
        score = True

    # The reservation that binds, taken after consent and before anything is
    # dispatched. It can still come back empty — two clicks can race for the
    # last run of the week — and that is the same 429.
    reservation = await discovery_budget.reserve(adb, user_id, limits=limits)
    if reservation.granted <= 0:
        log.info("discovery.run_now_capped", user_id=user_id)
        raise _cap_429(limits.per_week, limits)

    log.info("discovery.run_now", user_id=user_id, scored=score)
    if queues.enabled():
        try:
            queued = await dispatch_cycle(
                "discovery", user_id, trigger="manual", score=score
            )
        except Exception:
            # The enqueue failed — Cloud Tasks 503, a missing IAM binding, an
            # unset WORKER_URL/TASKS_SA_EMAIL. The user gets a 500 and will
            # click again, so the search they did not get must not be charged.
            # (Only the queue branch: the in-process branch below defers the
            # cycle to a background task and cannot fail here.)
            await _refund_run(user_id, "manual", week=reservation.week_key)
            raise
        if not queued:
            # A double-click the queue collapsed into the first task. No second
            # crawl will run, so no second run is owed.
            await _refund_run(user_id, "manual", week=reservation.week_key)
        return {"ok": True, "mode": "queued", "deduped": not queued, "scored": score}
    # No queue infra: run in-process, after the response goes out.
    background_tasks.add_task(
        run_discovery_cycle, user_id, trigger="manual", score=score
    )
    return {"ok": True, "mode": "in_process", "scored": score}


@router.post("/settings/discovery/sweep")
async def run_sweep_now(
    background_tasks: BackgroundTasks, user_id: str = Depends(verify_user)
) -> dict:
    """Explicit user action: run the liveness sweep immediately.

    Refused from a local process before the ``queues.enabled()`` branch:
    enqueueing from a laptop hands the same production writes to the real
    worker one process further away.
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

    Unlike discovery and the sweep this takes no slot claim and is not on a
    per-user cadence: it costs one indexless Firestore query and no LLM call,
    and a document it does move gets a lease that keeps the next tick off it,
    so "every hour, for everyone" cannot become a retry storm.

    The dispatcher is bound to *this* request's ``background_tasks`` so the
    in-process path still works with ``QUEUE_MODE`` off; under QUEUE_MODE
    ``dispatch_tailor`` enqueues and the object is never touched.
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

    Ticks every user, so this fans out into cycles that spend real money on
    Gemini. Per-user settings decide whether anything actually runs, and a
    tombstoned account (``deleted_at``) is skipped whole. On the private worker
    service Cloud Run has already verified the scheduler's OIDC token; on the
    public hermes-api the ``CRON_SECRET`` header guards it, and leaving it
    unset disables the endpoint.

    Under QUEUE_MODE the fan-out is in-request, because hermes-worker does not
    run with ``cpu-throttling: false`` and anything deferred past the response
    would run on an instance that may already be frozen. Without a queue it
    stays a background task: a due tick there runs the whole
    discovery-and-scoring cycle, which cannot happen inside an HTTP request.

    One user's failure never costs the rest theirs; the loop logs and carries
    on. But a fan-out where *nothing* got through answers 5xx rather than 200,
    so an environmental failure (an unset ``TASKS_SA_EMAIL``, say) is visible
    to the scheduler's retry and alerting instead of looking like success.
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
            # This fan-out streams every document in ``users``, so a tombstoned
            # account whose wipe is still in flight is picked up here and
            # nowhere else. Skipped whole — including the reaper, which would
            # otherwise dispatch tailoring for a user who is leaving.
            deleted += 1
            continue
        if enforce_allowlist and not await _allowlisted(uid):
            # Same seam, same reason: a de-allowlisted user's background loops
            # must stop here, or removing their seat doesn't bound their spend.
            # Off entirely while ALLOWLIST_ENFORCED is unset.
            not_allowlisted += 1
            continue
        if not inline:
            background_tasks.add_task(tick_user, uid, force_check=True, doc=doc)
            # Appends to the collection already being iterated, which is how
            # dispatch_tailor's in-process fallback reaches the loop at all; a
            # task added mid-iteration is still picked up.
            background_tasks.add_task(reap_user, uid, background_tasks=background_tasks)
            continue
        try:
            await tick_user(uid, force_check=True, doc=doc)
        except Exception:
            failed += 1
            log.exception("cron.tick_failed", user_id=uid)
        # Its own try/except, inside the per-user one: a reaper that throws for
        # every user must not turn a fan-out whose discovery ticks all worked
        # into the 5xx below. It gets its own counter instead.
        try:
            tally = await reap_user(uid, background_tasks=background_tasks)
            reaped += tally["recovered"]
            # A pass that ran out of its per-tick budget looks exactly like a
            # pass with nothing to do, so it is carried up: it signals a backlog
            # draining slower than it accumulates.
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
    # Against the users this tick actually tried, not the collection size:
    # counting a skipped (deleted or de-allowlisted) account as a success would
    # mask a fan-out where every real user's tick failed.
    attempted = len(users) - deleted - not_allowlisted
    if attempted and failed == attempted:
        # Nothing ticked. Almost always environmental (credentials, queue
        # config), so let the scheduler retry and let it be visible as a
        # failing job rather than an hourly 200 that does nothing.
        raise HTTPException(
            status_code=500, detail=f"every tick failed ({failed} users)"
        )
    return {
        "ok": True,
        "users": len(users),
        "failed": failed,
        # Tombstoned accounts, skipped whole. Its own counter so a fan-out that
        # suddenly ticks nobody reads as "everyone deleted themselves" rather
        # than "the loop is broken".
        "deleted": deleted,
        # Seat-revoked accounts, skipped whole, same reasoning. Always 0 while
        # ALLOWLIST_ENFORCED is off.
        "not_allowlisted": not_allowlisted,
        # The reaper is the one loop here whose inaction is invisible — a stuck
        # application looks like an idle one — so the count comes back rather
        # than living only in logs.
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
