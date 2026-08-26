# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The geo gate allowed to *act* — and shipped switched off.

Phase 1C wired ``tools.matching.geo`` in as a tape recorder
(``test_geo_shadow.py``). This is the machinery that lets it skip the Pro call
instead, behind ``GEO_GATE_ENFORCE``, which defaults to off. Three things are
being pinned, and they are not equally important:

**1. The merge-safety proof.** With the flag off — the shipped state — nothing
in this phase can produce a skip, and the pre-filter never so much as consults
the gate. If any test here could tell the enforcing code apart from the code
that shipped before it *while the flag is off*, the phase has quietly become a
behavior change.

**2. The sentinel is 0, not 20.** ``score.DISCARD_AT_OR_BELOW`` is 20 and means
one thing everywhere: *Pro* applied Rule 6's geographic cap. A gate-issued 20
would forge Pro decisions that were never made — corrupting
``cli.geo_replay``'s ``GEO_CAP_SCORE``, ``shadow_geo_gate``'s ``pro_capped``,
and every tombstone count derived from either.

**3. The skip is reversible.** ``discarded_jobs`` is discovery's dedupe key, so
a wrong skip suppresses a posting forever rather than losing one job. The
``restore`` payload and ``cli.geo_resurrect`` are what make that recoverable,
and the write ordering inside a resurrection is itself load-bearing.
"""

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import cli.geo_resurrect as geo_resurrect
import tools.matching.batch as batch
import tools.matching.batch_runs as batch_runs
import tools.matching.pipeline as pipeline
import tools.matching.score as score
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile, Residence
from tools.matching import geo

# A JD parse the gate can only call ineligible: a German office, no remote
# scope to widen it, no US-remote invitation. Rule 4, `country_mismatch`.
FOREIGN = {"job_country": "Germany"}


def _profile(country: str | None = "US") -> MasterProfile:
    return MasterProfile(
        user_id="u1",
        full_name="Test Candidate",
        email="test@example.com",
        location="Somewhere, Elsewhere",
        residence=Residence(country=country) if country else None,
        objective_template="{role} at {company}",
        experience=[],
        education=[],
        skills={},
        preferences={
            "target_role_families": ["engineering"],
            "target_titles": ["Staff Software Engineer"],
            "target_seniorities": ["staff"],
        },
    )


def _parsed(**kw) -> ParsedJD:
    kw.setdefault("role_family", "engineering")
    return ParsedJD(seniority="staff", summary="Build.", **kw)


def _job(job_id: str = "j1", *, parsed: ParsedJD | None = None) -> Job:
    job = Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id=job_id,
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=f"Build things {job_id}.",
        discovered_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    job.jd_parsed = parsed if parsed is not None else _parsed(**FOREIGN)
    return job


def _match(value: float = 85) -> JobMatch:
    return JobMatch(
        job_id="j1",
        overall_score=value,
        breakdown=ScoreBreakdown(
            role_fit=value,
            qualifications_match=value,
            seniority_match=value,
            comp_alignment=50,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="A fine match.",
        recommendation="apply",
    )


@pytest.fixture
def no_holdout(monkeypatch):
    """Take the hold-out out of the picture for the enforcement tests.

    The hold-out is deliberately probabilistic-looking (it is not — see
    :func:`pipeline.geo_holdout`), and a test asserting "this job is skipped"
    must not depend on which side of the hash its id happens to fall. The
    hold-out gets its own tests below, where it is the subject.
    """
    monkeypatch.setenv("GEO_GATE_HOLDOUT", "0")


# ----------------------------------------------------- the sentinel is not 20


def test_geo_sentinel_scores_zero():
    assert pipeline.GEO_INELIGIBLE.overall_score == 0
    assert pipeline.GEO_INELIGIBLE.recommendation == "skip"


def test_geo_sentinel_must_not_be_the_pro_geo_cap():
    """**The test that fails if someone "helpfully" changes 0 to 20.**

    20 is how a *Pro-issued* geo cap is identified, everywhere. Three separate
    readers agree on that, and all three are checked here rather than trusted:
    they are in different modules and nothing but this test couples them.
    """
    from cli.geo_replay import GEO_CAP_SCORE

    sentinel = pipeline.GEO_INELIGIBLE.overall_score
    assert sentinel != score.DISCARD_AT_OR_BELOW
    # cli.geo_replay would count an enforced tombstone as a Pro geo cap...
    assert float(sentinel) != GEO_CAP_SCORE
    # ...and shadow_geo_gate would report pro_capped on a call Pro never made.
    assert sentinel != score.DISCARD_AT_OR_BELOW
    # It is still discarded, of course — that is what 0 has always meant.
    assert score.should_discard(pipeline.GEO_INELIGIBLE)


def test_the_two_free_sentinels_are_told_apart_by_the_flag_not_the_score(no_holdout):
    """``OUT_OF_FAMILY`` and ``GEO_INELIGIBLE`` share a score on purpose: both
    mean "never reached Pro". Only ``geo_gate.enforced`` separates them."""
    profile = _profile()
    family_miss, no_decision = pipeline.prefilter(
        _job(parsed=_parsed(role_family="sales")), profile, enforce=True
    )
    geo_skip, decision = pipeline.prefilter(_job(), profile, enforce=True)

    assert family_miss.overall_score == geo_skip.overall_score == 0
    assert no_decision is None and decision is not None
    assert score.enforced_geo_gate(no_decision) is None
    assert score.enforced_geo_gate(decision)["enforced"] is True


# ------------------------------------------------- the flag is off by default


def test_enforcement_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("GEO_GATE_ENFORCE", raising=False)
    assert pipeline.geo_enforce_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", " on "])
def test_enforcement_flag_accepts_the_house_spellings(monkeypatch, value):
    monkeypatch.setenv("GEO_GATE_ENFORCE", value)
    assert pipeline.geo_enforce_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "yes", "no", "enabled"])
def test_enforcement_flag_rejects_everything_else(monkeypatch, value):
    """Anything not on the list is off. A spend-changing switch must never be
    turned on by a typo."""
    monkeypatch.setenv("GEO_GATE_ENFORCE", value)
    assert pipeline.geo_enforce_enabled() is False


# -------------------------------------------------------- the merge-safety proof


@pytest.mark.parametrize(
    "parsed",
    [
        _parsed(**FOREIGN),  # ineligible: country_mismatch
        _parsed(remote_scope="Europe only"),  # ineligible: scope_excludes_country
        _parsed(),  # abstain
        _parsed(us_remote_ok=True),  # eligible
    ],
)
def test_flag_off_never_produces_a_geo_skip(parsed):
    """**The merge-safety proof.** Off is the shipped state, and off has to mean
    the pre-Pro behavior that shipped before this phase: a family test, and
    nothing else. Every verdict the gate can reach is run through it."""
    match, decision = pipeline.prefilter(_job(parsed=parsed), _profile(), enforce=False)
    assert match is None
    assert decision is None


def test_flag_off_does_not_even_consult_the_gate(monkeypatch):
    """Stronger than "no skip": with the flag off there is no new code path at
    all, so not even an exception can come out of one. Checked by making the
    gate explode — if it is reached, this test says so."""

    def explode(parsed, profile):
        raise AssertionError("the gate was consulted with enforcement off")

    monkeypatch.setattr(pipeline.geo, "evaluate", explode)
    assert pipeline.prefilter(_job(), _profile(), enforce=False) == (None, None)


def test_flag_off_still_rejects_out_of_family(monkeypatch):
    """...and the family test it *does* still do is untouched."""
    monkeypatch.delenv("GEO_GATE_ENFORCE", raising=False)
    match, decision = pipeline.prefilter(
        _job(parsed=_parsed(role_family="sales")), _profile(), enforce=False
    )
    assert match.overall_score == 0
    assert match.reasoning == pipeline.OUT_OF_FAMILY.reasoning
    assert decision is None


# ---------------------------------------------------------- enforcing, flag on


def test_ineligible_us_resident_is_skipped(no_holdout):
    match, decision = pipeline.prefilter(_job("j9"), _profile("US"), enforce=True)
    assert match is not None
    assert match.job_id == "j9"
    assert match.overall_score == 0
    assert match.reasoning == pipeline.GEO_INELIGIBLE.reasoning
    assert (decision.verdict, decision.rule) == ("ineligible", "country_mismatch")


def test_ineligible_non_us_resident_is_never_skipped(no_holdout):
    """**Non-negotiable, and it lives here rather than in geo.py.** For a
    non-US resident the rule structure inverts: ``us_remote_ok`` is hard-gated
    on US inside the gate so the safety valve carrying 73.4% of the kept corpus
    never fires, while ``country_mismatch`` fires against nearly every US
    posting. That population is unmeasured *and* structurally different."""
    job = _job(parsed=_parsed(job_country="United States"))
    match, decision = pipeline.prefilter(job, _profile("Canada"), enforce=True)

    # The gate really does say ineligible — this is not a case that fell
    # through some earlier rule.
    assert (decision.verdict, decision.residence_country) == ("ineligible", "CA")
    assert match is None  # ...and we decline to act on it anyway.


@pytest.mark.parametrize(
    "parsed,expected_verdict",
    [
        (_parsed(), "abstain"),
        (_parsed(remote_scope="Somewhere unreadable"), "abstain"),
        (_parsed(us_remote_ok=True), "eligible"),
    ],
)
def test_only_ineligible_is_ever_skipped(parsed, expected_verdict, no_holdout):
    """``abstain`` means "let Pro decide", which is the status quo, and
    ``eligible`` is only ever reached by the US-remote exception and would skip
    nothing. Neither may cost a Pro call."""
    match, decision = pipeline.prefilter(_job(parsed=parsed), _profile(), enforce=True)
    assert match is None
    assert decision.verdict == expected_verdict


def test_out_of_family_wins_over_the_gate(no_holdout):
    """The family test is checked first and short-circuits, so an out-of-family
    job that is *also* geographically ineligible tombstones as out-of-family and
    carries no gate record. Otherwise the enforced corpus would be polluted with
    jobs the gate never actually saved a call on."""
    job = _job(parsed=_parsed(role_family="sales", **FOREIGN))
    match, decision = pipeline.prefilter(job, _profile(), enforce=True)
    assert match.reasoning == pipeline.OUT_OF_FAMILY.reasoning
    assert decision is None


def test_a_broken_profile_abstains_instead_of_skipping(monkeypatch):
    """The gate is pure and has no business raising, but a profile it cannot
    read must cost a skipped *optimization*, never a skipped job."""

    def explode(parsed, profile):
        raise TypeError("no residence on this thing")

    monkeypatch.setattr(pipeline.geo, "evaluate", explode)
    assert pipeline.prefilter(_job(), _profile(), enforce=True) == (None, None)


def test_sentinel_is_a_copy_not_the_singleton(no_holdout):
    first, _ = pipeline.prefilter(_job("a"), _profile(), enforce=True)
    second, _ = pipeline.prefilter(_job("b"), _profile(), enforce=True)
    assert first is not pipeline.GEO_INELIGIBLE
    assert (first.job_id, second.job_id) == ("a", "b")
    assert pipeline.GEO_INELIGIBLE.job_id == ""


# ------------------------------------------------------------------ hold-out


def test_holdout_is_deterministic_across_calls_and_processes():
    """**Load-bearing.** ``batch_runs`` decides at submit and joins the
    responses at ingest, hours later and in a different process. A job that
    flipped in between would corrupt the content join — the ingest would either
    hunt for a Pro response that was never requested, or tombstone a job whose
    paid response is in the output it is holding.

    The literal digests are pinned, not just self-consistency: ``hash(str)`` is
    salted per process, so a self-consistency check inside one interpreter is
    exactly the assertion that would keep passing after someone swapped the
    implementation for the broken thing.
    """
    ids = [f"job-{i}" for i in range(50)]
    first = [pipeline.geo_holdout(i, 0.5) for i in ids]
    second = [pipeline.geo_holdout(i, 0.5) for i in ids]
    assert first == second
    # Pinned values, computed from sha256 — these must survive a restart, a
    # redeploy, and a different machine.
    assert pipeline.geo_holdout("job-0", 0.5) is False
    assert pipeline.geo_holdout("job-1", 0.5) is True
    assert pipeline.geo_holdout("posting-2", 0.10) is False
    assert pipeline.geo_holdout("posting-25", 0.10) is True


def test_holdout_fraction_is_roughly_right_over_many_ids():
    n = 5000
    ids = [f"posting-{i}" for i in range(n)]
    held = sum(pipeline.geo_holdout(i, 0.10) for i in ids)
    # Binomial sd here is ~21, so ±5% absolute is ~24 sd of slack: this is
    # checking the mapping isn't skewed, not chasing a p-value.
    assert 0.05 < held / n < 0.15


@pytest.mark.parametrize(
    "fraction,expected", [(0.0, False), (-1.0, False), (1.0, True)]
)
def test_holdout_endpoints(fraction, expected):
    assert pipeline.geo_holdout("anything", fraction) is expected


def test_holdout_defaults_to_ten_percent(monkeypatch):
    monkeypatch.delenv("GEO_GATE_HOLDOUT", raising=False)
    assert pipeline.geo_holdout_fraction() == pipeline.DEFAULT_GEO_HOLDOUT == 0.10


@pytest.mark.parametrize("value", ["banana", "1.5", "-0.2", "", "  "])
def test_bad_holdout_env_falls_back_rather_than_disabling(monkeypatch, value):
    """Falling back to the default, never to zero: a hold-out of zero is the one
    setting that silently destroys the measurement the gate is justified by."""
    monkeypatch.setenv("GEO_GATE_HOLDOUT", value)
    assert pipeline.geo_holdout_fraction() == pipeline.DEFAULT_GEO_HOLDOUT


def test_held_out_job_reaches_pro_despite_an_ineligible_verdict(monkeypatch):
    monkeypatch.setenv("GEO_GATE_HOLDOUT", "1")  # everything held out
    match, decision = pipeline.prefilter(_job(), _profile(), enforce=True)
    assert match is None  # goes to Pro, and the shadow path records it as usual
    assert decision.verdict == "ineligible"


# --------------------------------------------- the enforced record and restore


def _enforced_gate() -> dict:
    _, decision = pipeline.prefilter(_job(), _profile(), enforce=True)
    return score.enforced_geo_gate(decision)


def test_enforced_record_carries_no_pro_keys(monkeypatch):
    monkeypatch.setenv("GEO_GATE_HOLDOUT", "0")
    gate = _enforced_gate()
    assert gate == {
        "version": geo.GATE_VERSION,
        "verdict": "ineligible",
        "rule": "country_mismatch",
        "residence_country": "US",
        "job_country": "DE",
        "enforced": True,
    }
    # Absent, not null: no Pro call was made, and a null pro_score sitting
    # beside thousands of real ones is an invitation to average it in.
    assert not any(k.startswith("pro_") for k in gate)


def test_enforced_tombstone_carries_restore_and_the_flag(no_holdout):
    ref = _DiscardingRef()
    job = _job()
    match, decision = pipeline.prefilter(job, _profile(), enforce=True)

    outcome = asyncio.run(
        score.persist_result(
            ref, job, match, geo_gate=score.enforced_geo_gate(decision)
        )
    )

    assert outcome == "discarded"
    stone = ref.written
    assert stone["score"] == 0
    assert stone["geo_gate"]["enforced"] is True
    assert not any(k.startswith("pro_") for k in stone["geo_gate"])
    # The restore payload is what makes the suppression reversible.
    assert Job.model_validate(stone["restore"]) == job
    assert stone["restore"]["jd_raw"] == job.jd_raw
    assert stone["restore"]["jd_parsed"]["job_country"] == "Germany"


def test_ordinary_tombstones_carry_no_restore_payload():
    """A Pro-issued discard is a judgement, not a machine-provable claim:
    reversing it would need the call re-run, so the (heavy) payload would be
    pure weight. ``jd_raw`` staying out is the whole point of a tombstone."""
    ref = _DiscardingRef()
    job = _job()
    asyncio.run(score.persist_result(ref, job, _match(20), profile=_profile()))
    assert "restore" not in ref.written
    assert "jd_raw" not in ref.written


def test_purge_discarded_still_gets_a_plain_tombstone():
    """``cli.purge_discarded`` calls ``discard_tombstone`` directly with neither
    a gate nor a restore — that caller is why all three stayed keyword-only."""
    stone = score.discard_tombstone(_job(), _match(0), scored_run_id="older-run")
    assert stone["scored_run_id"] == "older-run"
    assert "geo_gate" not in stone and "restore" not in stone


def test_explicit_geo_gate_beats_the_shadow_path(no_holdout):
    """An enforced record cannot travel through ``shadow_geo_gate``: that
    correctly returns ``None`` at ``overall_score <= 0``, because there is no
    Pro decision to compare against — and an enforced skip is precisely a
    record with no Pro decision."""
    job = _job()
    assert score.shadow_geo_gate(job, pipeline.GEO_INELIGIBLE, _profile()) is None

    ref = _DiscardingRef()
    asyncio.run(
        score.persist_result(
            ref,
            job,
            pipeline.GEO_INELIGIBLE,
            profile=_profile(),  # would otherwise record nothing at all
            geo_gate={"enforced": True, "verdict": "ineligible"},
        )
    )
    assert ref.written["geo_gate"] == {"enforced": True, "verdict": "ineligible"}


# ------------------------------------------------ every scorer counts the skip


class _KeepingRef:
    def __init__(self):
        self.updates: list[dict] = []

    async def update(self, fields: dict) -> None:
        self.updates.append(fields)

    @property
    def written(self) -> dict:
        return self.updates[0]


class _DiscardingRef:
    """A job doc that gets tombstoned: persist_result walks parent.parent."""

    def __init__(self):
        self.stones: list[dict] = []
        self.deleted = False
        outer = self

        class _Doc:
            async def set(self, doc):
                outer.stones.append(doc)

        class _Collection:
            def document(self, job_id):
                return _Doc()

        class _UserRef:
            def collection(self, name):
                assert name == "discarded_jobs"
                return _Collection()

        self.parent = SimpleNamespace(parent=_UserRef())

    async def delete(self) -> None:
        self.deleted = True

    @property
    def written(self) -> dict:
        return self.stones[0]


def test_online_scorer_skips_and_counts(monkeypatch, unlimited_budget, no_holdout):
    """End to end through the online scorer: no Pro call, a tombstone carrying
    the enforced record, and a ``geo_skipped`` tally on the counts contract."""
    monkeypatch.setenv("GEO_GATE_ENFORCE", "1")
    profile = _profile()
    skipped_ref, scored_ref = _DiscardingRef(), _KeepingRef()
    jobs = [
        (skipped_ref, _job("j1")),  # foreign office → skipped
        (scored_ref, _job("j2", parsed=_parsed())),  # abstain → scored
    ]
    pro_calls: list[str] = []

    async def fake_load(db, user_id, limit=None):
        return profile, jobs

    async def fake_match(job, prof, cached_content=None):
        pro_calls.append(job.id)
        return _match(85)

    async def no_cache(prof, *a, **kw):
        return None

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert pro_calls == ["j2"]  # the money not spent
    assert counts["geo_skipped"] == 1
    assert counts["discarded"] == 1 and counts["scored"] == 1
    assert skipped_ref.written["geo_gate"]["enforced"] is True
    assert "restore" in skipped_ref.written
    # The scored job took the ordinary shadow path, untouched.
    assert scored_ref.written["geo_gate"]["pro_capped"] is False


def test_online_scorer_with_the_flag_off_scores_everything(
    monkeypatch, unlimited_budget
):
    """The same run, flag off: both jobs reach Pro and nothing is skipped."""
    monkeypatch.delenv("GEO_GATE_ENFORCE", raising=False)
    profile = _profile()
    jobs = [(_KeepingRef(), _job("j1")), (_KeepingRef(), _job("j2"))]
    pro_calls: list[str] = []

    async def fake_load(db, user_id, limit=None):
        return profile, jobs

    async def fake_match(job, prof, cached_content=None):
        pro_calls.append(job.id)
        return _match(85)

    async def no_cache(prof, *a, **kw):
        return None

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert sorted(pro_calls) == ["j1", "j2"]
    assert counts["geo_skipped"] == 0
    assert counts["scored"] == 2


def test_batch_scorer_skips_and_counts(monkeypatch, unlimited_budget, no_holdout):
    """The half-price path enforces through the same seam. If it did not, the
    cheap path would ship silently unenforced and the measured saving would
    describe only whatever fraction happened to run online."""
    monkeypatch.setenv("GEO_GATE_ENFORCE", "1")
    ref = _DiscardingRef()

    async def fake_load(db, user_id, limit=None):
        return _profile(), [(ref, _job("j1"))]

    async def fake_run_batch(*, model, lines, **kw):
        raise AssertionError("a batch was submitted for a job the gate rejected")

    monkeypatch.setattr(batch, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(batch, "_run_batch", fake_run_batch)
    monkeypatch.setattr(batch, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(batch.batch_score_pending_jobs("u1"))

    assert counts["geo_skipped"] == 1 and counts["discarded"] == 1
    assert ref.written["geo_gate"]["enforced"] is True
    assert "restore" in ref.written


def test_resumable_batch_run_skips_and_records(monkeypatch, no_holdout):
    """The third scorer. ``batch_runs`` is the path a real signup's backlog
    takes (≥50 pending jobs), so an unenforced seam here would mean the gate
    never fires on the workload it was measured against."""
    monkeypatch.setenv("GEO_GATE_ENFORCE", "1")
    persisted: list[tuple] = []

    async def fake_persist_result(ref, job, match, *, profile=None, geo_gate=None):
        persisted.append((job.id, match.overall_score, geo_gate))
        return "discarded"

    async def no_submit(**kw):
        raise AssertionError("a Pro batch was submitted for a gate-rejected job")

    updates: list[dict] = []
    run_ref = SimpleNamespace(id="tag", update=lambda fields: _record(updates, fields))
    monkeypatch.setattr(batch_runs, "persist_result", fake_persist_result)
    monkeypatch.setattr(batch_runs, "submit_batch", no_submit)

    counts = {"scored": 0, "discarded": 0, "failed": 0, "parse_failed": 0}
    stage = asyncio.run(
        batch_runs._submit_score_stage(
            None,
            run_ref,
            "tag",
            "gs://bucket/tag",
            _profile(),
            [(object(), _job("j1"))],
            counts,
        )
    )

    assert stage == "done"  # nothing left needing Pro
    assert counts["geo_skipped"] == 1 and counts["discarded"] == 1
    job_id, overall, gate = persisted[0]
    assert (job_id, overall) == ("j1", 0)
    assert gate["enforced"] is True and gate["rule"] == "country_mismatch"
    assert updates[-1]["state"] == "done"


async def _record(sink: list, fields: dict) -> None:
    sink.append(fields)


def test_every_scorer_reads_one_enforcement_flag():
    """Three scorers, one switch. A path with its own copy would enforce (or
    fail to) independently of the others."""
    assert score.geo_enforce_enabled is pipeline.geo_enforce_enabled
    assert batch.geo_enforce_enabled is pipeline.geo_enforce_enabled
    assert batch_runs.geo_enforce_enabled is pipeline.geo_enforce_enabled


def test_empty_geo_counts_carries_the_new_key():
    """The counts contract must not depend on which branch produced it — one
    KeyError waiting for whoever reads these numbers next."""
    assert score.EMPTY_GEO_COUNTS == {
        "geo_ineligible": 0,
        "geo_abstain": 0,
        "geo_skipped": 0,
    }


# --------------------------------------------------------------- resurrection


class _RecordingUserRef:
    """A user doc that records the *order* of every write it receives."""

    def __init__(self, tombstones: dict[str, dict] | None = None):
        self.ops: list[tuple[str, str]] = []
        self.jobs: dict[str, dict] = {}
        self.tombstones = dict(tombstones or {})
        outer = self

        class _JobDoc:
            def __init__(self, doc_id):
                self.doc_id = doc_id

            async def set(self, doc):
                outer.ops.append(("set_job", self.doc_id))
                outer.jobs[self.doc_id] = doc

        class _TombDoc:
            def __init__(self, doc_id):
                self.doc_id = doc_id

            async def delete(self):
                outer.ops.append(("delete_tombstone", self.doc_id))
                outer.tombstones.pop(self.doc_id, None)

        class _Collection:
            def __init__(self, name):
                self.name = name

            def document(self, doc_id):
                return _JobDoc(doc_id) if self.name == "jobs" else _TombDoc(doc_id)

        self._collection = _Collection

    def collection(self, name):
        return self._collection(name)


def test_resurrection_writes_the_job_before_deleting_the_tombstone():
    """**The ordering is the point.** Discovery checks the tombstone *before*
    the job doc, so job-then-tombstone fails safe (a crash in between leaves the
    posting suppressed-but-present: the scorer picks it up, discovery does not
    duplicate it) while tombstone-then-job opens a window in which a concurrent
    discovery cycle re-persists a stale copy."""
    user_ref = _RecordingUserRef()
    job = _job("j1")

    asyncio.run(geo_resurrect.resurrect_one(user_ref, job))

    assert user_ref.ops == [("set_job", "j1"), ("delete_tombstone", "j1")]
    assert user_ref.jobs["j1"]["jd_raw"] == job.jd_raw


def _tombstone(job: Job, *, version: int = 1) -> dict:
    """An enforced tombstone exactly as ``persist_result`` would have written it."""
    gate = score.enforced_geo_gate(geo.evaluate(job.jd_parsed, _profile()))
    return score.discard_tombstone(
        job,
        pipeline.GEO_INELIGIBLE,
        geo_gate={**gate, "version": version},
        restore=score.restore_payload(job),
    )


def test_classify_resurrects_only_a_changed_verdict(no_holdout):
    """Still ineligible → left alone. Resurrecting it would buy the Pro call the
    gate exists to avoid, and the next enforcing run would tombstone it again."""
    still_bad = _tombstone(_job("j1"))
    assert geo_resurrect.classify(still_bad, _profile(), below_version=None)[0] == (
        "still_ineligible"
    )

    # Same tombstone, but the user has moved to Germany: the office is now
    # local and the verdict flips to abstain.
    outcome, job, decision = geo_resurrect.classify(
        still_bad, _profile("Germany"), below_version=None
    )
    assert outcome == "resurrect"
    assert decision.verdict == "abstain" and job.id == "j1"


def test_classify_reruns_the_current_gate_not_the_stored_verdict(no_holdout):
    """A tombstone records what the *old* gate said. What decides resurrection
    is what the current one says over the stored parse — that is the whole
    point of carrying ``jd_parsed`` rather than a verdict."""
    stone = _tombstone(_job("j1"))
    assert stone["geo_gate"]["verdict"] == "ineligible"
    # A profile whose residence the gate cannot read abstains at rule 1.
    outcome, _, decision = geo_resurrect.classify(
        stone, _profile(None), below_version=None
    )
    assert (outcome, decision.rule) == ("resurrect", "no_residence")


def test_classify_below_version_skips_current_gate_tombstones(no_holdout):
    stone = _tombstone(_job("j1"), version=3)
    assert geo_resurrect.classify(stone, _profile(), below_version=3)[0] == (
        "current_version"
    )
    # Older than the bump: re-evaluated (and here, still ineligible).
    assert geo_resurrect.classify(stone, _profile(), below_version=4)[0] == (
        "still_ineligible"
    )


def test_classify_reports_a_tombstone_it_cannot_restore():
    """Tombstones written before ``restore`` existed stay suppressed, and that
    has to be *reported* rather than skipped quietly."""
    plain = score.discard_tombstone(
        _job("j1"), pipeline.GEO_INELIGIBLE, geo_gate={"enforced": True, "version": 1}
    )
    assert geo_resurrect.classify(plain, _profile(), below_version=None)[0] == (
        "unrestorable"
    )


def test_classify_will_not_re_parse_to_get_a_verdict(no_holdout):
    """A restore payload with no parse cannot be re-evaluated for free, and
    buying a Flash call is exactly what this tool promises not to do."""
    job = _job("j1")
    stone = _tombstone(job)
    stone["restore"] = {**stone["restore"], "jd_parsed": None}
    assert geo_resurrect.classify(stone, _profile(), below_version=None)[0] == (
        "no_parse"
    )


def test_resurrect_cli_writes_only_changed_docs_and_in_the_right_order(
    monkeypatch, no_holdout, capsys
):
    """The tool end to end: two enforced tombstones, one whose verdict changed.

    Runs against a profile that has moved to Germany, so the German posting
    becomes reachable while the Europe-scoped one stays out (a US-scoped role
    is what a German resident cannot hold).
    """
    stays = _job("stays", parsed=_parsed(remote_scope="United States only"))
    comes_back = _job("back")
    user_ref = _RecordingUserRef(
        {"stays": _tombstone(stays), "back": _tombstone(comes_back)}
    )
    profile = _profile("Germany")
    _install_fake_db(monkeypatch, user_ref, profile)
    monkeypatch.setattr(sys, "argv", ["geo_resurrect", "--user-id", "u1", "--execute"])

    asyncio.run(geo_resurrect.main())

    assert user_ref.ops == [("set_job", "back"), ("delete_tombstone", "back")]
    assert set(user_ref.tombstones) == {"stays"}
    assert "resurrect" in capsys.readouterr().out


def test_resurrect_cli_is_dry_run_by_default(monkeypatch, no_holdout, capsys):
    """Without ``--execute`` it reports and writes nothing — the same shape as
    ``cli.reset_user``, and for the same reason."""
    user_ref = _RecordingUserRef({"back": _tombstone(_job("back"))})
    _install_fake_db(monkeypatch, user_ref, _profile("Germany"))
    monkeypatch.setattr(sys, "argv", ["geo_resurrect", "--user-id", "u1"])

    asyncio.run(geo_resurrect.main())

    assert user_ref.ops == []
    assert "would resurrect 1" in capsys.readouterr().out


def test_resurrect_cli_limit_bounds_the_spend_it_reopens(
    monkeypatch, no_holdout, capsys
):
    """Each resurrection re-opens a Pro call competing for the next cycle's
    budget slots, so a thousand-job sweep is a spend decision."""
    stones = {f"j{i}": _tombstone(_job(f"j{i}")) for i in range(5)}
    user_ref = _RecordingUserRef(stones)
    _install_fake_db(monkeypatch, user_ref, _profile("Germany"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["geo_resurrect", "--user-id", "u1", "--limit", "2", "--execute"],
    )

    asyncio.run(geo_resurrect.main())

    assert len([op for op in user_ref.ops if op[0] == "set_job"]) == 2
    assert "over_limit: 3" in capsys.readouterr().out


def _install_fake_db(monkeypatch, user_ref: _RecordingUserRef, profile) -> None:
    """A Firestore stand-in exposing just what ``geo_resurrect.main`` touches."""

    class _Snap:
        def __init__(self, doc_id, doc):
            self.id = doc_id
            self._doc = doc
            self.exists = doc is not None

        def to_dict(self):
            return self._doc

    class _Query:
        def __init__(self, docs):
            self._docs = docs

        async def stream(self):
            for doc_id, doc in list(self._docs.items()):
                yield _Snap(doc_id, doc)

        def __aiter__(self):
            return self.stream()

    class _Collection:
        def __init__(self, name):
            self.name = name

        def where(self, *, filter):
            assert filter.field_path == "geo_gate.enforced"
            return _Query(
                {
                    k: v
                    for k, v in user_ref.tombstones.items()
                    if (v.get("geo_gate") or {}).get("enforced") is True
                }
            )

        def document(self, doc_id):
            # Writes go to the recording ref, so the ordering assertions see
            # both halves of a resurrection.
            return user_ref.collection(self.name).document(doc_id)

    class _UserDoc:
        async def get(self):
            return _Snap("u1", profile.model_dump(mode="json"))

        def collection(self, name):
            return _Collection(name)

    class _DB:
        def collection(self, name):
            assert name == "users"
            return SimpleNamespace(document=lambda uid: _UserDoc())

    monkeypatch.setattr(geo_resurrect.firestore, "AsyncClient", lambda: _DB())
