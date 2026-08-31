# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-user scoring budget: the cap that makes a signup's worst case knowable.

Uncapped, one fresh signup's first discovery cycle fetched ~13,000 jobs and
spent $28-32 — automatically, on onboarding completion. The cap is a *pre-run
reservation* of job slots rather than a per-job counter, so these tests split
the same way the code does: the whole rollover/clamping matrix against the
pure ``apply_reservation``/``apply_release``, then the transaction wrapper,
then the three scoring entry points that must all pass through the gate (miss
any one of them and that path bypasses the cap entirely).

Zero LLM calls, zero GCP: the Firestore transaction is faked at the protocol
the real ``@async_transactional`` decorator drives, so the decorator's own
retry-on-Aborted behavior is exercised rather than stubbed out.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted

import tools.matching.batch as batch
import tools.matching.batch_runs as batch_runs
import tools.matching.score as score
from models.job import Job, ParsedJD
from tools.matching import budget
from tools.matching.budget import (
    Limits,
    Reservation,
    apply_release,
    apply_reservation,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
TODAY = "2026-08-23"
LIMITS = Limits(per_cycle=300, per_day=1000)


def _state(**kw) -> dict:
    base = {
        "day": TODAY,
        "jobs_scored_today": 0,
        "cycle_id": "cycle-1",
        "jobs_scored_this_cycle": 0,
        "updated_at": NOW.isoformat(),
    }
    base.update(kw)
    return base


def _reserve(state, wanted, *, cycle_id="cycle-1", now=NOW, limits=LIMITS):
    return apply_reservation(state, wanted, now=now, cycle_id=cycle_id, limits=limits)


# ------------------------------------------------------- apply_reservation()


def test_first_ever_run_grants_the_whole_ask():
    new_state, res = _reserve(None, 300)
    assert res.granted == 300 and res.capped is False
    assert res.remaining_cycle == 0 and res.remaining_day == 700
    assert new_state == {
        "day": TODAY,
        "jobs_scored_today": 300,
        "cycle_id": "cycle-1",
        "jobs_scored_this_cycle": 300,
        "updated_at": NOW.isoformat(),
    }


def test_a_13k_backlog_is_truncated_to_one_cycle():
    """The number this whole phase exists for: the ask is the backlog, the
    grant is one cycle's worth, and the rest waits."""
    _new_state, res = _reserve(None, 13_000)
    assert res.granted == 300 and res.capped is True


def test_second_run_in_the_same_cycle_gets_the_remainder():
    state, first = _reserve(None, 200)
    _state2, second = _reserve(state, 200)
    assert (first.granted, second.granted) == (200, 100)
    assert second.capped is True and second.remaining_cycle == 0


def test_exhausted_cycle_grants_nothing():
    _new_state, res = _reserve(_state(jobs_scored_this_cycle=300), 50)
    assert res.granted == 0 and res.capped is True
    assert res.remaining_cycle == 0


def test_a_new_cycle_rolls_the_cycle_counter_but_not_the_day():
    state = _state(jobs_scored_this_cycle=300, jobs_scored_today=300)
    new_state, res = _reserve(state, 300, cycle_id="cycle-2")
    assert res.granted == 300  # fresh cycle window
    assert new_state["cycle_id"] == "cycle-2"
    assert new_state["jobs_scored_this_cycle"] == 300
    # …and the day counter carried over, which is what stops "new cycle every
    # minute" from being free.
    assert new_state["jobs_scored_today"] == 600
    assert res.remaining_day == 400


def test_the_daily_cap_binds_even_with_a_fresh_cycle():
    state = _state(jobs_scored_today=950, jobs_scored_this_cycle=0)
    _new_state, res = _reserve(state, 300, cycle_id="cycle-9")
    assert res.granted == 50 and res.capped is True
    assert res.remaining_day == 0 and res.remaining_cycle == 250


def test_yesterdays_count_rolls_over_lazily_at_read_time():
    """No cron resets this: a stored day that isn't today just reads as zero."""
    stale = _state(day="2026-08-22", jobs_scored_today=1000, jobs_scored_this_cycle=0)
    new_state, res = _reserve(stale, 300, now=NOW)
    assert res.granted == 300
    assert new_state["day"] == TODAY and new_state["jobs_scored_today"] == 300


def test_rollover_is_by_utc_date_not_elapsed_hours():
    state = _state(jobs_scored_today=1000)
    just_before = datetime(2026, 8, 23, 23, 59, tzinfo=UTC)
    _s1, res = _reserve(state, 10, now=just_before)
    assert res.granted == 0
    _s2, res = _reserve(state, 10, now=just_before + timedelta(minutes=2))
    assert res.granted == 10


def test_none_cycle_id_draws_down_the_open_window():
    """An ad-hoc score task with no run bound must not open a fresh window —
    it spends what the current cycle has left, with the daily cap behind it."""
    state = _state(jobs_scored_this_cycle=280, jobs_scored_today=280)
    new_state, res = _reserve(state, 300, cycle_id=None)
    assert res.granted == 20 and res.capped is True
    assert new_state["cycle_id"] == "cycle-1"  # window unchanged
    assert new_state["jobs_scored_this_cycle"] == 300


def test_none_cycle_id_on_a_user_who_has_never_scored():
    new_state, res = _reserve(None, 10, cycle_id=None)
    assert res.granted == 10 and res.cycle_id is None
    assert new_state["cycle_id"] is None


def test_asking_for_nothing_takes_nothing():
    new_state, res = _reserve(_state(), 0)
    assert res.granted == 0 and res.capped is False
    assert new_state["jobs_scored_this_cycle"] == 0


def test_garbage_counters_never_grant_more_than_the_limit():
    """A hand-edited doc is the realistic source of this; it must fail closed
    on nonsense, not hand out extra budget."""
    for bad in ({"jobs_scored_this_cycle": -500}, {"jobs_scored_this_cycle": "many"}):
        _new_state, res = _reserve(_state(**bad), 300)
        assert res.granted == 300 and res.remaining_cycle == 0


def test_limits_are_honored_from_the_passed_limits_object():
    _new_state, res = _reserve(None, 500, limits=Limits(per_cycle=50, per_day=60))
    assert res.granted == 50 and res.capped is True


# ------------------------------------------------------------ apply_release()


def test_release_gives_back_unused_slots():
    state = _state(jobs_scored_today=300, jobs_scored_this_cycle=300)
    refunded = apply_release(state, 288, now=NOW, cycle_id="cycle-1")
    assert refunded is not None
    assert refunded["jobs_scored_this_cycle"] == 12
    assert refunded["jobs_scored_today"] == 12


def test_release_of_nothing_is_a_noop():
    assert apply_release(_state(), 0, now=NOW, cycle_id="cycle-1") is None
    assert apply_release(_state(), -5, now=NOW, cycle_id="cycle-1") is None


def test_release_will_not_credit_a_different_cycle():
    """The refund is windowed: crediting a cycle the slots weren't taken from
    would hand a later run budget it never earned."""
    state = _state(cycle_id="cycle-2", jobs_scored_this_cycle=40, jobs_scored_today=340)
    refunded = apply_release(state, 288, now=NOW, cycle_id="cycle-1")
    assert refunded is not None
    assert refunded["jobs_scored_this_cycle"] == 40  # untouched
    assert refunded["jobs_scored_today"] == 52  # the day is still the day


def test_release_after_midnight_credits_nothing():
    state = _state(day="2026-08-22", cycle_id="cycle-0", jobs_scored_today=300)
    assert apply_release(state, 300, now=NOW, cycle_id="cycle-1") is None


def test_release_floors_at_zero():
    refunded = apply_release(
        _state(jobs_scored_today=5, jobs_scored_this_cycle=5),
        100,
        now=NOW,
        cycle_id="cycle-1",
    )
    assert refunded == {**_state(), "updated_at": refunded["updated_at"]}


# ----------------------------------------------------------------- summary()


def test_summary_reports_a_full_draw_as_capped():
    """A run that filled every slot it was granted has more backlog waiting,
    even though the reservation itself was granted in full."""
    res = Reservation(300, False, 0, 700, "cycle-1")
    assert budget.summary(res, drawn=300)["budget_capped"] is True
    assert budget.summary(res, drawn=12)["budget_capped"] is False
    assert budget.summary(res)["budget_capped"] is False


def test_summary_reports_what_is_left_after_the_refund_not_before_it():
    """The 1B reporting gap. ``Reservation`` is built at reserve time, with the
    whole grant already debited; the caller's ``finally`` hands back
    ``granted - drawn``. Reporting the reservation's own numbers skips that
    refund, and ``budget_remaining_day`` is what the Profile card prints
    verbatim as "N of today's scoring budget left".

    12 pending jobs against a 200-slot grant on a 1000/day limit: 188 come
    straight back, and the user has 988 left, not 800.
    """
    res = Reservation(
        granted=200,
        capped=False,
        remaining_cycle=0,
        remaining_day=800,
        cycle_id="cycle-1",
    )
    scored_twelve = budget.summary(res, drawn=12)
    assert scored_twelve["budget_remaining_day"] == 988
    assert scored_twelve["budget_remaining_cycle"] == 188
    assert scored_twelve["budget_granted"] == 200  # the grant is still the grant

    # A run that drew everything refunds nothing, and reports the debit in full.
    drew_it_all = budget.summary(res, drawn=200)
    assert drew_it_all["budget_remaining_day"] == 800
    assert drew_it_all["budget_remaining_cycle"] == 0

    # No ``drawn`` at all means "the run has not happened yet" — the reserve-time
    # log line is the caller — so nothing is refunded there either.
    assert budget.summary(res)["budget_remaining_day"] == 800


def test_summary_of_an_unbudgeted_run_is_empty():
    # --ignore-budget reports no budget at all rather than a fabricated zero.
    assert budget.summary(None, drawn=9000) == {}


# --------------------------------------------------------------- Limits.from_env


def test_limits_default_to_the_documented_numbers(monkeypatch):
    monkeypatch.delenv("SCORING_BUDGET_PER_CYCLE", raising=False)
    monkeypatch.delenv("SCORING_BUDGET_PER_DAY", raising=False)
    # Calibrated against the $0.0098/job measured 2026-08-23: 200 slots is
    # ~$1.96, which is what holds a first cycle under the $2 criterion.
    assert Limits.from_env() == Limits(
        per_cycle=budget.DEFAULT_PER_CYCLE, per_day=budget.DEFAULT_PER_DAY
    )
    assert (budget.DEFAULT_PER_CYCLE, budget.DEFAULT_PER_DAY) == (200, 400)


def test_limits_read_the_env(monkeypatch):
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "25")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "40")
    assert Limits.from_env() == Limits(per_cycle=25, per_day=40)


def test_unparseable_limits_fall_back_to_the_defaults(monkeypatch):
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "lots")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "")
    assert Limits.from_env() == Limits(
        per_cycle=budget.DEFAULT_PER_CYCLE, per_day=budget.DEFAULT_PER_DAY
    )


# ------------------------------------------------- reserve() / release() txn


class _FakeSnap:
    def __init__(self, doc):
        self._doc = doc

    def to_dict(self):
        return dict(self._doc)


class _FakeDoc:
    def __init__(self, store):
        self._store = store

    async def get(self, transaction=None):
        return _FakeSnap(self._store)


class _FakeTransaction:
    """Enough of AsyncTransaction for the real decorator to drive it.

    Writes buffer until commit, so a test can make one attempt lose the race
    (``Aborted``) and watch the retry re-read the winner's state — which is
    the whole argument for a reservation over a per-job counter.
    """

    _read_only = False
    _max_attempts = 5

    def __init__(self, store, abort_once=False):
        self._store = store
        self._id = None
        self._buffered = None
        self._abort_once = abort_once
        self.commits = 0

    def _clean_up(self):
        self._buffered = None

    async def _begin(self, retry_id=None):
        self._id = b"txn"

    async def _rollback(self):
        self._buffered = None

    async def _commit(self):
        if self._abort_once:
            self._abort_once = False
            raise Aborted("contended")
        if self._buffered is not None:
            self._store.update(self._buffered)
        self.commits += 1
        return []

    def set(self, reference, document_data, merge=False):
        self._buffered = dict(document_data)


class _FakeCollection:
    def __init__(self, doc):
        self._doc = doc

    def document(self, doc_id):
        return self._doc


class _FakeRunRef:
    """Stands in for a batch_runs doc — batch_runs.start writes one."""

    def __init__(self, store, update_fails=False):
        self.id = "r1"
        self._store = store
        self._update_fails = update_fails

    async def set(self, doc):
        self._store.update(doc)

    async def update(self, fields, option=None):
        if self._update_fails:
            raise RuntimeError("firestore died recording the job name")
        self._store.update(fields)


class _FakeDB:
    """Just enough Firestore for users/{uid} under a transaction."""

    def __init__(self, state=None, abort_once=False, run_update_fails=False):
        self.store: dict = {"scoring_budget": state} if state else {}
        self.run_doc: dict = {}
        self._abort_once = abort_once
        self._run_update_fails = run_update_fails
        self.transactions: list[_FakeTransaction] = []

    def collection(self, name):
        if name == "users":
            return _FakeCollection(_FakeDoc(self.store))
        return _FakeCollection(_FakeRunRef(self.run_doc, self._run_update_fails))

    def transaction(self):
        txn = _FakeTransaction(self.store, abort_once=self._abort_once)
        self._abort_once = False
        self.transactions.append(txn)
        return txn

    @property
    def budget_state(self) -> dict:
        return self.store["scoring_budget"]


def test_reserve_persists_the_new_counters(monkeypatch):
    db = _FakeDB()
    res = asyncio.run(budget.reserve(db, "u1", 10, cycle_id="cycle-1", limits=LIMITS))
    assert res.granted == 10
    assert db.budget_state["jobs_scored_this_cycle"] == 10
    assert db.budget_state["cycle_id"] == "cycle-1"


def test_reserve_defaults_to_a_full_cycles_worth(monkeypatch):
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "7")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "1000")
    db = _FakeDB()
    res = asyncio.run(budget.reserve(db, "u1", cycle_id="c"))
    assert res.granted == 7  # "however much this cycle is allowed"


def test_concurrent_runs_get_disjoint_slices():
    """Two instances scoring the same user: transactions serialize, so the
    second sees the first's debit and takes what is left — never the same
    slots twice."""
    db = _FakeDB()
    limits = Limits(per_cycle=300, per_day=1000)
    first = asyncio.run(budget.reserve(db, "u1", 200, cycle_id="c", limits=limits))
    second = asyncio.run(budget.reserve(db, "u1", 200, cycle_id="c", limits=limits))
    assert (first.granted, second.granted) == (200, 100)
    assert db.budget_state["jobs_scored_this_cycle"] == 300


def test_a_contended_reserve_retries_without_double_debiting():
    db = _FakeDB(abort_once=True)
    res = asyncio.run(budget.reserve(db, "u1", 50, cycle_id="c", limits=LIMITS))
    assert res.granted == 50
    assert db.budget_state["jobs_scored_this_cycle"] == 50  # not 100


def test_release_refunds_through_the_transaction():
    db = _FakeDB(
        state={
            "day": TODAY,
            "jobs_scored_today": 300,
            "cycle_id": "c",
            "jobs_scored_this_cycle": 300,
        }
    )
    asyncio.run(budget.release(db, "u1", 288, cycle_id="c"))
    assert db.budget_state["jobs_scored_this_cycle"] == 12


def test_release_never_raises():
    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    asyncio.run(budget.release(_BrokenDB(), "u1", 10, cycle_id="c"))


def test_release_of_zero_never_touches_firestore():
    class _ExplodingDB:
        def collection(self, name):
            raise AssertionError("wrote for a zero refund")

    asyncio.run(budget.release(_ExplodingDB(), "u1", 0, cycle_id="c"))


def test_reserve_failures_propagate():
    """Fail closed: a run that cannot reserve cannot read its jobs either, and
    guessing in the other direction is what costs money."""

    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    with pytest.raises(RuntimeError):
        asyncio.run(budget.reserve(_BrokenDB(), "u1", 10))


# ------------------------------------------------------- the three gate sites


PROFILE = SimpleNamespace(
    preferences=SimpleNamespace(target_role_families=["engineering"])
)


def _job(job_id: str, *, parsed: bool = True) -> Job:
    return Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id=job_id,
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=f"Build things {job_id}.",
        discovered_at=datetime.now(UTC),
        jd_parsed=ParsedJD(
            role_family="engineering", seniority="staff", summary="Build."
        )
        if parsed
        else None,
    )


@pytest.fixture
def budgeted(monkeypatch):
    """A real budget against a fake Firestore, shared by every gate below."""
    monkeypatch.setenv("SCORING_BUDGET_PER_CYCLE", "5")
    monkeypatch.setenv("SCORING_BUDGET_PER_DAY", "1000")
    db = _FakeDB()
    for module in (score, batch, batch_runs):
        monkeypatch.setattr(module.firestore, "AsyncClient", lambda: db, raising=False)
    return db


def _patch_pending(monkeypatch, module, pending):
    """Record the limit the gate hands down, and honor it like Firestore would."""
    loaded: list = []

    async def fake_load(db, user_id, limit=None):
        loaded.append(limit)
        return PROFILE, pending[:limit] if limit else pending

    monkeypatch.setattr(module, "load_profile_and_pending", fake_load)
    return loaded


def test_online_scorer_scores_only_what_it_was_granted(budgeted, monkeypatch):
    pending = [(SimpleNamespace(), _job(f"j{i}")) for i in range(20)]
    loaded = _patch_pending(monkeypatch, score, pending)
    scored = []

    async def fake_match(job, profile, cached_content=None):
        scored.append(job.id)
        raise RuntimeError("no LLM in unit tests")

    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score, "create_match_cache", _never_called_cache)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    # The grant became the query's limit — the other 15 jobs were never even
    # loaded, let alone scored.
    assert loaded == [5]
    assert len(scored) == 5
    assert counts["failed"] == 5
    assert counts["budget_granted"] == 5 and counts["budget_capped"] is True
    assert counts["budget_remaining_cycle"] == 0
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 5


async def _never_called_cache(profile, *a, **kw):
    return None


def test_online_scorer_refunds_the_slots_it_did_not_use(budgeted, monkeypatch):
    pending = [(SimpleNamespace(), _job("j1"))]
    _patch_pending(monkeypatch, score, pending)

    async def fake_match(job, profile, cached_content=None):
        raise RuntimeError("no LLM in unit tests")

    monkeypatch.setattr(score, "match_job", fake_match)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert counts["pending"] == 1
    # Reserved 5, drew on 1 — the other 4 go back, so free rejections don't
    # burn a budget they never spent.
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 1
    assert budgeted.budget_state["jobs_scored_today"] == 1


def test_a_cancelled_run_keeps_only_the_slots_it_was_spending(budgeted, monkeypatch):
    """Worker shutdown / the 1800s Cloud Run timeout cancels the gather. The
    counts dict never comes back, so slots have to be tracked as jobs run —
    refunding a run that already paid for hundreds of Pro calls is the one
    direction that costs money."""
    pending = [(SimpleNamespace(), _job(f"j{i}")) for i in range(5)]
    _patch_pending(monkeypatch, score, pending)
    monkeypatch.setattr(score, "create_match_cache", _never_called_cache)
    in_flight: list[str] = []

    async def hang(job, profile, cached_content=None):
        in_flight.append(job.id)
        await asyncio.sleep(3600)  # a Pro call that never comes back

    monkeypatch.setattr(score, "match_job", hang)

    async def scenario():
        task = asyncio.create_task(score.score_pending_jobs("u1", concurrency=2))
        for _ in range(100):  # let the two semaphore slots fill
            if len(in_flight) >= 2:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # The two calls in flight are billed and keep their slots; the three still
    # queued behind the semaphore never ran, so those go back.
    assert in_flight == ["j0", "j1"]
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 2


def test_a_run_that_dies_still_gives_its_slots_back(budgeted, monkeypatch):
    async def explode(db, user_id, limit=None):
        raise ValueError("No profile at users/u1.")

    monkeypatch.setattr(score, "load_profile_and_pending", explode)

    with pytest.raises(ValueError):
        asyncio.run(score.score_pending_jobs("u1"))

    assert budgeted.budget_state["jobs_scored_this_cycle"] == 0


def test_online_scorer_stops_when_the_budget_is_spent(budgeted, monkeypatch):
    async def explode(db, user_id, limit=None):
        raise AssertionError("loaded jobs with no budget left")

    monkeypatch.setattr(score, "load_profile_and_pending", explode)
    budgeted.store["scoring_budget"] = {
        "day": datetime.now(UTC).date().isoformat(),
        "jobs_scored_today": 5,
        "cycle_id": None,
        "jobs_scored_this_cycle": 5,
    }

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert counts == {
        "scored": 0,
        "discarded": 0,
        "failed": 0,
        "pending": 0,
        # Zeroed rather than omitted: every branch that can return counts
        # returns the same key set, or reading them means guessing which one
        # ran (tools/matching/score.py: EMPTY_GEO_COUNTS).
        "geo_ineligible": 0,
        "geo_abstain": 0,
        "geo_skipped": 0,
        "budget_granted": 0,
        "budget_remaining_cycle": 0,
        "budget_remaining_day": 995,
        "budget_capped": True,
    }


def test_ignore_budget_skips_the_gate_but_keeps_a_ceiling(budgeted, monkeypatch):
    loaded = _patch_pending(monkeypatch, score, [])
    monkeypatch.setattr(score, "create_match_cache", _never_called_cache)

    asyncio.run(score.score_pending_jobs("u1", ignore_budget=True))

    # No reservation was taken, so nothing was debited — but the operator
    # still doesn't get to stream an unbounded backlog into memory.
    assert loaded == [score.SCORE_LIMIT_CEILING]
    assert budgeted.store == {}


def test_ignore_budget_honors_an_explicit_limit(budgeted, monkeypatch):
    loaded = _patch_pending(monkeypatch, score, [])

    asyncio.run(score.score_pending_jobs("u1", limit=5000, ignore_budget=True))

    assert loaded == [5000]  # the documented 12-13K backlog workflow
    assert budgeted.store == {}


def test_batch_scorer_reserves_too(budgeted, monkeypatch):
    """The third gate. cli/run_matching --batch reaches this function through
    neither of the other two, so without a reservation here the CLI bypasses
    the cap entirely."""
    pending = [(SimpleNamespace(), _job(f"j{i}")) for i in range(20)]
    loaded = _patch_pending(monkeypatch, batch, pending)

    async def fake_run_batch(**kw):
        raise RuntimeError("pro batch exploded")

    monkeypatch.setattr(batch, "_run_batch", fake_run_batch)
    monkeypatch.setattr(batch, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch, "build_match_context", lambda p: "CTX")
    monkeypatch.setattr(batch, "build_match_job_block", lambda j: f"BLOCK-{j.id}")

    with pytest.raises(RuntimeError, match="pro batch exploded"):
        asyncio.run(batch.batch_score_pending_jobs("u1"))

    assert loaded == [5]
    # A submitted batch's spend is committed, so its slots are not refunded
    # even though the run then died.
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 5


def test_batch_scorer_honors_ignore_budget(budgeted, monkeypatch):
    loaded = _patch_pending(monkeypatch, batch, [])
    monkeypatch.setattr(batch, "batch_bucket_name", lambda: "test-bucket")

    asyncio.run(batch.batch_score_pending_jobs("u1", limit=9000, ignore_budget=True))

    assert loaded == [9000]
    assert budgeted.store == {}


def test_batch_run_start_reserves(budgeted, monkeypatch):
    pending = [(SimpleNamespace(), _job(f"j{i}")) for i in range(20)]
    loaded = _patch_pending(monkeypatch, batch_runs, pending)
    submitted = []

    async def fake_submit(*, model, lines, gcs_dir, display_name):
        submitted.append(len(lines))
        return "batch/1"

    async def fake_upload(path, text, **kw):
        pass

    async def fake_lookup_many(db, texts):
        return {}

    monkeypatch.setattr(batch_runs, "submit_batch", fake_submit)
    monkeypatch.setattr(batch_runs, "upload_text", fake_upload)
    monkeypatch.setattr(batch_runs.jd_cache, "lookup_many", fake_lookup_many)
    monkeypatch.setattr(batch_runs, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch_runs, "build_match_context", lambda p: "CTX")
    monkeypatch.setattr(batch_runs, "build_match_job_block", lambda j: f"BLOCK-{j.id}")

    result = asyncio.run(batch_runs.start("u1"))

    assert loaded == [5]
    assert result["pending"] == 5 and submitted == [5]
    assert result["budget_granted"] == 5 and result["budget_capped"] is True
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 5


def test_batch_run_start_keeps_its_slots_when_the_submit_is_paid_for(
    budgeted, monkeypatch
):
    """start() pays for the Flash batch and *then* records the job name. If
    that write fails, the batch is still running on Vertex — resume() will
    orphan the run — so the slots must stay spent. Refunding them here is how
    a user gets billed twice for the same grant."""
    # Unparsed, so the run's paid leg is the Flash parse batch — the submit
    # that happens before the job name is recorded.
    pending = [(SimpleNamespace(), _job(f"j{i}", parsed=False)) for i in range(20)]
    _patch_pending(monkeypatch, batch_runs, pending)
    budgeted._run_update_fails = True
    submitted = []

    async def fake_submit(*, model, lines, gcs_dir, display_name):
        submitted.append(len(lines))
        return "batch/1"

    async def fake_lookup_many(db, texts):
        return {}

    monkeypatch.setattr(batch_runs, "submit_batch", fake_submit)
    monkeypatch.setattr(batch_runs.jd_cache, "lookup_many", fake_lookup_many)
    monkeypatch.setattr(batch_runs, "batch_bucket_name", lambda: "test-bucket")

    with pytest.raises(RuntimeError, match="recording the job name"):
        asyncio.run(batch_runs.start("u1"))

    assert submitted == [5]  # real money already spent
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 5


def test_batch_run_start_refunds_when_it_does_not_start(budgeted, monkeypatch):
    """min_pending said the backlog was too small: nothing was submitted, so
    nothing is owed."""
    _patch_pending(monkeypatch, batch_runs, [(SimpleNamespace(), _job("j1"))])

    result = asyncio.run(batch_runs.start("u1", min_pending=50))

    assert result["started"] is False
    assert budgeted.budget_state["jobs_scored_this_cycle"] == 0
