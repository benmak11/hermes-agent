# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared per-board health: one ``board_health/{platform}:{slug}`` doc per board.

Discovery records what each fresh board fetch answered, so a board that moved
platform or died stops looking like an empty one. The records are shared by
every user and hold no user data: a board answers the same whoever asks, which
is also why ``cli/reset_user.py`` and the account wipe leave them alone.

States:

* ``ok`` / ``failing`` — the last fetch answered, or did not.
* ``moved`` — after 404s on :data:`PROBE_AFTER_DAYS` UTC days, exactly one
  other platform has jobs under the same slug and its company name matches
  the board's recorded name. ``resolves_to`` names it. Discovery fetches it
  instead only under ``BOARD_REROUTE_AUTO``; re-verified every
  :data:`REVERIFY_MOVED`.
* ``quarantined`` — the probe found jobs elsewhere but could not prove it is
  the same company. Still fetched; ``candidates`` are for a human.
* ``dead`` — 404 on :data:`DEAD_AFTER_DAYS` days spanning :data:`DEAD_SPAN`
  with no candidate. Skipped by discovery until ``next_check_at``.

Any fetch that answers ok returns a board to ``ok``. Records are read once per
cycle and written only where a stored field would change, with absolute values
so two users' cycles on the same day are idempotent. Every failure is logged
and swallowed: health tracking must never fail a search.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from obs.logging import get_logger
from tools.ats._http import NOT_FOUND, OK, board_client
from tools.ats.probe import PROBED_PLATFORMS, BoardProbe, probe_board

log = get_logger("tools.ats.board_health")

COLLECTION = "board_health"

#: Board platforms. ``google_jobs`` / ``meta_jobs`` slugs are search queries,
#: not boards, so they get no record.
TRACKED_PLATFORMS = frozenset({"greenhouse", "lever", "ashby"})

STATE_OK = "ok"
STATE_FAILING = "failing"
STATE_QUARANTINED = "quarantined"
STATE_DEAD = "dead"
STATE_MOVED = "moved"

#: Problem states first; the admin table and the CLI report sort by this.
STATE_ORDER = (STATE_FAILING, STATE_QUARANTINED, STATE_DEAD, STATE_MOVED, STATE_OK)

#: 404 days before the slug is probed on the other platforms.
PROBE_AFTER_DAYS = 2
#: 404 days, and the span from the first to the last, before a board is dead.
DEAD_AFTER_DAYS = 3
DEAD_SPAN = timedelta(days=14)
#: How long a dead board is skipped between re-checks.
RECHECK_DEAD = timedelta(days=14)
#: How often a moved board's original location is probed again.
REVERIFY_MOVED = timedelta(days=30)

REROUTE_FLAG = "BOARD_REROUTE_AUTO"

#: Concurrent writes. A first cycle writes every board once; after that a cycle
#: writes only the boards whose state moved.
_WRITE_CONCURRENCY = 20
#: Concurrent probes (each is one or two GETs) through the pooled client.
_PROBE_CONCURRENCY = 5

#: Fields set only in some states; absent from the stored doc when unset.
_OPTIONAL = (
    "first_not_found_day",
    "probed_at",
    "next_check_at",
    "reverify_at",
    "resolves_to",
    "candidates",
)

#: Fields whose change earns a write. ``updated_at`` is not one of them.
_COMPARED = (
    "platform",
    "slug",
    "state",
    "last_outcome",
    "last_status",
    "last_ok_at",
    "failing_since",
    "not_found_days",
    "last_not_found_day",
    *_OPTIONAL,
)

_UNHEALTHY = (STATE_FAILING, STATE_QUARANTINED, STATE_DEAD)


def doc_id(platform: str, slug: str) -> str:
    """The record's id. The slug keeps its case: Lever slugs are case-sensitive."""
    return f"{platform}:{slug}"


def _now() -> datetime:
    return datetime.now(UTC)


def now() -> datetime:
    """The current UTC time, through the one clock tests freeze."""
    return _now().astimezone(UTC)


def reroute_enabled() -> bool:
    """``BOARD_REROUTE_AUTO``, read per call; off unless set to 1/true/on."""
    return os.getenv(REROUTE_FLAG, "").strip().lower() in {"1", "true", "on"}


# ------------------------------------------------------------------ names

_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "gmbh",
        "plc",
        "sa",
        "ag",
        "bv",
        "pty",
        "oy",
        "ab",
    }
)
_APOSTROPHES = re.compile("['\\u2019]")
_PUNCT = re.compile(r"[^\w\s]|_")


def normalize_name(name: str | None) -> str | None:
    """Lowercase, punctuation to spaces, trailing legal suffixes dropped.

    Every other word is kept, so "Acme Labs" and "Acme" stay different.
    ``None`` for a missing or empty name.
    """
    if not name:
        return None
    text = _PUNCT.sub(" ", _APOSTROPHES.sub("", name.casefold()))
    words = text.split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words) or None


def names_match(a: str | None, b: str | None) -> bool:
    """Both names present and equal once normalised."""
    na, nb = normalize_name(a), normalize_name(b)
    return na is not None and na == nb


# ------------------------------------------------------------ transitions


def _finish(record: dict) -> dict:
    """Drop unset optional fields, so a stored doc carries only what applies."""
    return {k: v for k, v in record.items() if k not in _OPTIONAL or v is not None}


def _carry(prev: dict) -> dict:
    return {k: prev.get(k) for k in _COMPARED}


def _ok_record(prev: dict, platform: str, slug: str, status, stamp: str) -> dict:
    return {
        "platform": platform,
        "slug": slug,
        "state": STATE_OK,
        "last_outcome": OK,
        "last_status": status,
        "last_ok_at": prev.get("last_ok_at")
        if prev.get("state") == STATE_OK
        else stamp,
        "failing_since": None,
        "not_found_days": 0,
        "last_not_found_day": None,
    }


def next_record(
    prev: dict | None,
    platform: str,
    slug: str,
    outcome: str,
    status: int | None,
    now: datetime,
) -> dict:
    """The record after one fetch of the board itself, before ``updated_at``.

    Ok from any state is ``ok``. A failure keeps ``quarantined`` and ``dead``
    (a dead board's re-check pushes ``next_check_at`` on) and leaves a
    ``moved`` board's reroute and counts alone. ``last_ok_at`` moves only when
    the board turns ok, so a board that stays ok is not rewritten.
    """
    prev = prev or {}
    state = prev.get("state")
    now = now.astimezone(UTC)
    stamp = now.isoformat()
    if outcome == OK:
        return _ok_record(prev, platform, slug, status, stamp)
    if state == STATE_MOVED:
        return _finish({**_carry(prev), "last_outcome": outcome, "last_status": status})
    unhealthy = state in _UNHEALTHY
    days = (prev.get("not_found_days") or 0) if unhealthy else 0
    last_day = prev.get("last_not_found_day") if unhealthy else None
    # A record from before ``first_not_found_day`` existed starts its span at
    # its latest 404 day: later than the truth, so a board dies late, never early.
    first_day = (prev.get("first_not_found_day") or last_day) if unhealthy else None
    today = now.date().isoformat()
    if outcome == NOT_FOUND and last_day != today:
        days, last_day = days + 1, today
        first_day = first_day or today
    record: dict[str, Any] = {
        "platform": platform,
        "slug": slug,
        "state": state if state in (STATE_QUARANTINED, STATE_DEAD) else STATE_FAILING,
        "last_outcome": outcome,
        "last_status": status,
        "last_ok_at": prev.get("last_ok_at"),
        "failing_since": prev.get("failing_since") if unhealthy else stamp,
        "not_found_days": days,
        "last_not_found_day": last_day,
        "first_not_found_day": first_day,
    }
    if unhealthy:
        record["probed_at"] = prev.get("probed_at")
    if state == STATE_QUARANTINED:
        record["candidates"] = prev.get("candidates")
    if state == STATE_DEAD:
        record["next_check_at"] = (now + RECHECK_DEAD).isoformat()
    return _finish(record)


def next_rerouted(prev: dict, outcome: str, status: int | None, now: datetime) -> dict:
    """A ``moved`` record after its reroute target was fetched in its place.

    An ok target changes nothing. A failing target drops the reroute and the
    board goes back to ``failing``, carrying the target's outcome.
    """
    if outcome == OK:
        return _finish(_carry(prev))
    record = _carry(prev)
    record.update(
        state=STATE_FAILING,
        last_outcome=outcome,
        last_status=status,
        failing_since=prev.get("failing_since") or now.astimezone(UTC).isoformat(),
        resolves_to=None,
        reverify_at=None,
        candidates=None,
        next_check_at=None,
    )
    return _finish(record)


def _due(value: Any, now: datetime) -> bool:
    """An ISO timestamp at or before ``now``; a missing one is due."""
    if not isinstance(value, str):
        return True
    try:
        return datetime.fromisoformat(value) <= now
    except ValueError:
        return True


def _probed_today(record: dict, now: datetime) -> bool:
    probed = record.get("probed_at")
    return isinstance(probed, str) and probed[:10] == now.date().isoformat()


def needs_probe(record: dict, now: datetime, *, observed: bool, active: bool) -> bool:
    """Whether this cycle should run a probe round for the board.

    ``observed``: fetched this cycle. ``active``: composed this cycle. At most
    one round per board per UTC day.
    """
    if _probed_today(record, now):
        return False
    state = record.get("state")
    if state == STATE_MOVED:
        return active and _due(record.get("reverify_at"), now)
    if state == STATE_DEAD:
        return observed
    if state not in (STATE_FAILING, STATE_QUARANTINED) or not active:
        return False
    if (record.get("not_found_days") or 0) < PROBE_AFTER_DAYS:
        return False
    last_day = record.get("last_not_found_day")
    probed = record.get("probed_at")
    return bool(last_day) and (not probed or last_day > probed[:10])


def probe_targets(record: dict) -> list[tuple[str, str]]:
    """What a probe round fetches: a moved board's original location, or the
    same slug on every other probed platform."""
    platform, slug = record["platform"], record["slug"]
    if record.get("state") == STATE_MOVED:
        return [(platform, slug)]
    return [(p, slug) for p in PROBED_PLATFORMS if p != platform]


def _span_days(first: Any, last: Any) -> int:
    try:
        return (date.fromisoformat(last) - date.fromisoformat(first)).days
    except (TypeError, ValueError):
        return 0


def resolve(
    record: dict,
    entry_name: str | None,
    probes: dict[tuple[str, str], BoardProbe],
    now: datetime,
) -> dict:
    """The record after a probe round over :func:`probe_targets`.

    An inconclusive round (any answer other than ok or 404) changes nothing
    but ``probed_at``. ``entry_name`` is the board's name in the company YAML.
    """
    now = now.astimezone(UTC)
    stamp = now.isoformat()
    base = _carry(record)
    if any(p.outcome not in (OK, NOT_FOUND) for p in probes.values()):
        return _finish({**base, "probed_at": stamp})

    platform, slug = record["platform"], record["slug"]
    if record.get("state") == STATE_MOVED:
        original = probes.get((platform, slug))
        if original and original.outcome == OK and (original.job_count or 0) > 0:
            return _ok_record(record, platform, slug, original.status, stamp)
        return _finish(
            {
                **base,
                "probed_at": stamp,
                "reverify_at": (now + REVERIFY_MOVED).isoformat(),
            }
        )

    with_jobs = [
        {
            "platform": p,
            "slug": s,
            "name": probe.name,
            "job_count": probe.job_count,
            "probed_at": stamp,
        }
        for (p, s), probe in probes.items()
        if probe.outcome == OK and (probe.job_count or 0) > 0
    ]
    base.update(
        probed_at=stamp,
        resolves_to=None,
        reverify_at=None,
        candidates=None,
        next_check_at=None,
    )
    if not with_jobs:
        days = base.get("not_found_days") or 0
        span = _span_days(
            base.get("first_not_found_day"), base.get("last_not_found_day")
        )
        if record.get("state") == STATE_DEAD or (
            days >= DEAD_AFTER_DAYS and span >= DEAD_SPAN.days
        ):
            base.update(
                state=STATE_DEAD, next_check_at=(now + RECHECK_DEAD).isoformat()
            )
        else:
            base["state"] = STATE_FAILING
        return _finish(base)
    base["candidates"] = with_jobs
    (only, *rest) = with_jobs
    if not rest and names_match(entry_name, only["name"]):
        base.update(
            state=STATE_MOVED,
            resolves_to={"platform": only["platform"], "slug": only["slug"]},
            reverify_at=(now + REVERIFY_MOVED).isoformat(),
        )
    else:
        base["state"] = STATE_QUARANTINED
    return _finish(base)


def target_of(record: dict | None) -> tuple[str, str] | None:
    """A moved record's ``resolves_to`` as ``(platform, slug)``, if usable."""
    if not record or record.get("state") != STATE_MOVED:
        return None
    to = record.get("resolves_to")
    if not isinstance(to, dict):
        return None
    platform, slug = to.get("platform"), to.get("slug")
    if platform not in TRACKED_PLATFORMS or not isinstance(slug, str):
        return None
    if not slug or "/" in slug:
        return None
    return platform, slug


# ------------------------------------------------------------ compose time


@dataclass
class FetchPlan:
    """What one cycle fetches once health records are applied."""

    boards: list[tuple[Any, str, Any]] = field(default_factory=list)
    #: origin doc id → target doc id, for boards fetched at their target.
    reroutes: dict[str, str] = field(default_factory=dict)
    skipped_dead: list[str] = field(default_factory=list)
    #: ``(origin, target)`` doc ids a shadow cycle would have rerouted.
    would_reroute: list[tuple[str, str]] = field(default_factory=list)


def plan_fetches(
    companies: list[tuple[Any, str, Any]],
    records: dict[str, dict],
    now: datetime,
    *,
    reroute: bool,
    skip: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset(),
) -> FetchPlan:
    """Drop dead boards not yet due a re-check and, with ``reroute``, swap each
    moved board for its target.

    A target already composed (or already a substitute) is not added twice.
    A target in ``skip`` — ``(platform, slug.casefold())``, the blocklist and
    the user's exclusions — is never fetched; the original is, as in shadow.
    """
    plan = FetchPlan()
    composed = {(p, s.casefold()) for p, s, _ in companies}
    added: set[tuple[str, str]] = set()
    for platform, slug, source in companies:
        key = doc_id(platform, slug)
        record = records.get(key) if platform in TRACKED_PLATFORMS else None
        if record and record.get("state") == STATE_DEAD:
            if not _due(record.get("next_check_at"), now):
                plan.skipped_dead.append(key)
                continue
        target = target_of(record)
        if target is None:
            plan.boards.append((platform, slug, source))
            continue
        tp, ts = target
        if not reroute or (tp, ts.casefold()) in skip:
            plan.would_reroute.append((key, doc_id(tp, ts)))
            plan.boards.append((platform, slug, source))
            continue
        plan.reroutes[key] = doc_id(tp, ts)
        cf = (tp, ts.casefold())
        if cf in composed or cf in added:
            continue
        added.add(cf)
        plan.boards.append((tp, ts, source))
    return plan


# ----------------------------------------------------------------- storage


def _changed(prev: dict | None, record: dict) -> bool:
    if prev is None:
        return True
    return any(prev.get(k) != record.get(k) for k in _COMPARED)


async def _read_all(db) -> dict[str, dict]:
    return {
        snap.id: snap.to_dict() or {}
        async for snap in db.collection(COLLECTION).stream()
    }


async def load_records(db) -> dict[str, dict] | None:
    """Every record, keyed by doc id; ``None`` (logged) if the read fails."""
    try:
        return await _read_all(db)
    except Exception as e:
        log.warning("board_health.read_failed", error=f"{type(e).__name__}: {e}")
        return None


def _log_change(prev: dict | None, record: dict, **fields: Any) -> bool:
    """Log a first sight (info) or a state change (warning). True on a change."""
    if prev is None:
        log.info("board_health.first_seen", state=record["state"], **fields)
        return False
    if prev.get("state") == record["state"]:
        return False
    extra: dict[str, Any] = {}
    if record.get("resolves_to"):
        extra["resolves_to"] = record["resolves_to"]
    if record["state"] == STATE_QUARANTINED:
        extra["candidates"] = [c.get("name") for c in record.get("candidates") or []]
    log.warning(
        "board_health.changed",
        from_state=prev.get("state"),
        to_state=record["state"],
        not_found_days=record["not_found_days"],
        **fields,
        **extra,
    )
    return True


async def _write(
    db, pending: list[tuple[str, dict]]
) -> list[tuple[str, BaseException]]:
    sem = asyncio.Semaphore(_WRITE_CONCURRENCY)

    async def one(key: str, record: dict) -> None:
        async with sem:
            await db.collection(COLLECTION).document(key).set(record)

    results = await asyncio.gather(
        *(one(k, r) for k, r in pending), return_exceptions=True
    )
    failed = [
        (k, r)
        for (k, _), r in zip(pending, results, strict=True)
        if isinstance(r, BaseException)
    ]
    if failed:
        key, err = failed[0]
        log.warning(
            "board_health.write_failed",
            failed=len(failed),
            first=key,
            error=f"{type(err).__name__}: {err}",
        )
    return failed


async def record_outcomes(
    db,
    observations: list[tuple[str, str, str, int | None]],
    now: datetime | None = None,
    *,
    existing: dict[str, dict] | None = None,
    reroutes: dict[str, str] | None = None,
) -> dict[str, int]:
    """Fold this cycle's ``(platform, slug, outcome, status)`` into the records.

    Untracked platforms are ignored; a board observed twice keeps the last
    observation. ``reroutes`` (origin → target doc id) applies each target's
    outcome to its moved origin as well. ``existing`` is this cycle's read of
    the collection, reused rather than read again, and is updated in place so
    a later probe round sees the new records. Returns
    ``{"read", "written", "changed", "errors"}`` and never raises.
    """
    counts = {"read": 0, "written": 0, "changed": 0, "errors": 0}
    latest: dict[str, tuple[str, str, str, int | None]] = {}
    for platform, slug, outcome, status in observations:
        if platform in TRACKED_PLATFORMS and slug and "/" not in slug:
            latest[doc_id(platform, slug)] = (platform, slug, outcome, status)
    if not latest:
        return counts
    now = (now or _now()).astimezone(UTC)
    if existing is None:
        existing = await load_records(db)
        if existing is None:
            # Writing without the previous state would reset the 404 day count.
            counts["errors"] += 1
            return counts
    counts["read"] = len(existing)

    updates: list[tuple[str, dict | None, dict, dict]] = []
    for key, (platform, slug, outcome, status) in latest.items():
        prev = existing.get(key)
        record = next_record(prev, platform, slug, outcome, status, now)
        fields = {
            "platform": platform,
            "slug": slug,
            "outcome": outcome,
            "status": status,
        }
        updates.append((key, prev, record, fields))
    for origin, target in (reroutes or {}).items():
        prev = existing.get(origin)
        seen = latest.get(target)
        if seen is None or target_of(prev) is None:
            continue
        assert prev is not None
        _, _, outcome, status = seen
        record = next_rerouted(prev, outcome, status, now)
        fields = {
            "platform": prev["platform"],
            "slug": prev["slug"],
            "outcome": outcome,
            "status": status,
            "via": target,
        }
        updates.append((origin, prev, record, fields))

    pending: list[tuple[str, dict]] = []
    for key, prev, record, fields in updates:
        if not _changed(prev, record):
            continue
        record["updated_at"] = now.isoformat()
        pending.append((key, record))
        existing[key] = record
        if _log_change(prev, record, **fields):
            counts["changed"] += 1

    failed = await _write(db, pending)
    counts["written"] = len(pending) - len(failed)
    counts["errors"] += len(failed)
    log.info("board_health.recorded", boards=len(latest), **counts)
    return counts


async def probe_due(
    db,
    records: dict[str, dict],
    *,
    observed: set[str],
    active: set[str],
    names: Callable[[], dict[tuple[str, str], str]],
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one probe round for each board :func:`needs_probe` picks, and store
    what :func:`resolve` makes of it.

    ``names()`` maps ``(platform, slug.casefold())`` to the YAML name; it is
    called only when something is due. A round that raises is logged and its
    record left as it was. Returns ``{"probed", "probe_failed", "changed",
    "written", "errors"}`` and never raises.
    """
    counts = {"probed": 0, "probe_failed": 0, "changed": 0, "written": 0, "errors": 0}
    now = (now or _now()).astimezone(UTC)
    due = [
        key
        for key, record in records.items()
        if record.get("platform") in TRACKED_PLATFORMS
        and needs_probe(record, now, observed=key in observed, active=key in active)
    ]
    if not due:
        return counts
    try:
        entry_names = names()
    except Exception as e:
        log.warning("board_health.names_failed", error=f"{type(e).__name__}: {e}")
        entry_names = {}

    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(platform: str, slug: str) -> BoardProbe:
        async with sem:
            return await probe_board(platform, slug)

    async def round_for(key: str) -> dict[tuple[str, str], BoardProbe]:
        targets = probe_targets(records[key])
        answers = await asyncio.gather(*(one(p, s) for p, s in targets))
        return dict(zip(targets, answers, strict=True))

    async with board_client():
        rounds = await asyncio.gather(
            *(round_for(k) for k in due), return_exceptions=True
        )

    pending: list[tuple[str, dict]] = []
    for key, result in zip(due, rounds, strict=True):
        prev = records[key]
        fields = {"platform": prev["platform"], "slug": prev["slug"]}
        if isinstance(result, BaseException):
            counts["probe_failed"] += 1
            log.warning(
                "board_health.probe_failed",
                error=f"{type(result).__name__}: {result}",
                **fields,
            )
            continue
        counts["probed"] += 1
        name = entry_names.get((prev["platform"], prev["slug"].casefold()))
        record = resolve(prev, name, result, now)
        if not _changed(prev, record):
            continue
        record["updated_at"] = now.isoformat()
        pending.append((key, record))
        records[key] = record
        if _log_change(
            prev,
            record,
            outcome=record.get("last_outcome"),
            status=record.get("last_status"),
            probed=sorted(f"{p}/{s}:{b.outcome}" for (p, s), b in result.items()),
            **fields,
        ):
            counts["changed"] += 1

    failed = await _write(db, pending)
    counts["written"] = len(pending) - len(failed)
    counts["errors"] = len(failed)
    log.info("board_health.probed", boards=len(due), **counts)
    return counts
