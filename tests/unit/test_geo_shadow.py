# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The geo gate wired into scoring — as a tape recorder, and nothing more.

``tools.matching.geo`` is provably safe (0 false positives over 1,127 replayed
scores) and its coverage is provably *unmeasurable* from history: every job it
would have caught was tombstoned out of `jobs` by ``persist_result``, and the
tombstones carry no ``jd_parsed``. So the gate now runs live and writes down
what it would have said, next to what Pro actually said.

Two questions run through everything below.

**Is it really shadow?** The no-op tests are the load-bearing ones here. Score,
recommendation and discard outcome must be byte-identical with the gate wired
in and without it — if any test could tell the difference in *scoring*
behavior, this phase has quietly become a behavior change wearing a cost
optimization's clothes.

**Is it recording everywhere?** Both batch paths get their own test, because
the cheap path is the one that ships silently uninstrumented, and a coverage
number measured over the online scorer alone would be wrong in a way nobody
could see.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tools.matching.batch as batch
import tools.matching.score as score
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile, Residence
from tools.matching import geo

# Written by persist_result on every call and never comparable across two of
# them: one is a clock, the other is the recording under test.
VOLATILE = {"geo_gate", "scored_at", "discarded_at"}


def _profile(country: str = "US") -> MasterProfile:
    return MasterProfile(
        user_id="u1",
        full_name="Test Candidate",
        email="test@example.com",
        location="Somewhere, Elsewhere",
        residence=Residence(country=country),
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
        discovered_at=datetime.now(UTC),
    )
    job.jd_parsed = parsed
    return job


def _match(score_value: float, *, red_flags=(), reasoning="A fine match.") -> JobMatch:
    return JobMatch(
        job_id="j1",
        overall_score=score_value,
        breakdown=ScoreBreakdown(
            role_fit=score_value,
            qualifications_match=score_value,
            seniority_match=score_value,
            comp_alignment=50,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=list(red_flags),
        reasoning=reasoning,
        recommendation="apply",
    )


class _KeepingRef:
    """A job doc that survives scoring: persist_result updates it in place."""

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


def _persist(ref, job, match, profile=None) -> str:
    return asyncio.run(score.persist_result(ref, job, match, profile=profile))


@pytest.fixture
def gate_calls(monkeypatch):
    """Every ``geo.evaluate`` the code under test actually makes.

    The "record nothing" cases below have to be checked against this and not
    only against the absence of a ``geo_gate`` field: :func:`shadow_geo_gate`
    swallows its own exceptions, so deleting a guard outright *also* produces a
    document with no ``geo_gate`` on it. The distinction that matters is
    whether the gate was consulted at all.
    """
    calls = []
    real = geo.evaluate

    def recording(parsed, profile):
        calls.append((parsed, profile))
        return real(parsed, profile)

    monkeypatch.setattr(score.geo, "evaluate", recording)
    return calls


# ------------------------------------------------------ the record itself


def test_scored_job_doc_carries_the_gate_verdict():
    ref = _KeepingRef()
    job = _job(parsed=_parsed(job_country="Germany"))

    assert _persist(ref, job, _match(85), _profile()) == "scored"

    gate = ref.written["geo_gate"]
    assert gate == {
        "version": geo.GATE_VERSION,
        "verdict": "ineligible",
        "rule": "country_mismatch",
        "residence_country": "US",
        "job_country": "DE",
        "pro_score": 85.0,
        "pro_capped": False,
        "pro_geo_flag": False,
    }


def test_tombstone_carries_the_gate_verdict():
    """The discard path is where the gate's whole upside lives — 69.4% of Pro
    calls come back capped at exactly 20 and land here — so a tombstone that
    doesn't carry the verdict makes the measurement impossible."""
    ref = _DiscardingRef()
    job = _job(parsed=_parsed(job_country="Germany"))

    assert _persist(ref, job, _match(20), _profile()) == "discarded"

    gate = ref.written["geo_gate"]
    assert gate["version"] == geo.GATE_VERSION
    assert (gate["verdict"], gate["rule"]) == ("ineligible", "country_mismatch")
    assert gate["pro_score"] == 20.0 and gate["pro_capped"] is True
    # Still a minimal record otherwise.
    assert "jd_raw" not in ref.written and "red_flags_hit" not in ref.written


def test_the_gate_can_disagree_with_pro_and_it_is_still_just_recorded():
    """The whole point of storing raw fields: a verdict of ``abstain`` against
    a Pro cap at 20 is coverage left on the table, and it has to be countable
    later without anyone having decided today what "agree" means."""
    ref = _DiscardingRef()
    job = _job(parsed=_parsed())  # nothing to go on — abstains

    _persist(ref, job, _match(20), _profile())

    gate = ref.written["geo_gate"]
    assert (gate["verdict"], gate["rule"]) == ("abstain", "country_unknown")
    assert gate["pro_capped"] is True
    # Raw inputs, not a conclusion — 1D redefines the metric, not the schema.
    assert "agree" not in gate


def test_pro_capped_means_exactly_twenty():
    # A weighted score that merely lands under the discard threshold is a bad
    # match, not a geo rejection, and conflating them would invent true
    # positives out of the ambiguous band.
    ref = _DiscardingRef()
    _persist(ref, _job(parsed=_parsed()), _match(12), _profile())
    assert ref.written["geo_gate"]["pro_capped"] is False


@pytest.mark.parametrize(
    "red_flags,reasoning,expected",
    [
        ((), "Strong overlap on distributed systems.", False),
        ((), "Role is onsite in Berlin; candidate is in Austin.", True),
        (("Requires relocation to Munich",), "Otherwise a good fit.", True),
        ((), "Geographically ineligible for this posting.", True),
        (("No visa sponsorship",), "Good fit.", True),
    ],
)
def test_pro_geo_flag_reads_both_red_flags_and_reasoning(
    red_flags, reasoning, expected
):
    """The disambiguator for the ambiguous band. It has to read *both* fields
    because ``discard_tombstone`` keeps only ``reasoning``, so if this were
    computed any later than persist_result the red flags would already be gone.
    """
    match = _match(12, red_flags=red_flags, reasoning=reasoning)
    assert score.pro_geo_flag(match) is expected


# --------------------------------------------- when nothing may be recorded


def test_no_record_without_a_profile(gate_calls):
    # cli/purge_discarded and any future backfill reach persist_result with no
    # profile to hand; they must keep working, and must not invent verdicts.
    ref = _KeepingRef()
    _persist(ref, _job(parsed=_parsed(job_country="Germany")), _match(85))
    assert "geo_gate" not in ref.written
    assert gate_calls == []


def test_no_record_without_a_parse(gate_calls):
    ref = _KeepingRef()
    _persist(ref, _job(), _match(85), _profile())
    assert "geo_gate" not in ref.written
    assert gate_calls == []


def test_no_record_for_the_out_of_family_sentinel(gate_calls):
    """A zero never reached Pro (``pipeline.OUT_OF_FAMILY``), so there is no
    decision to compare the gate against. This also drops a genuine Pro zero,
    which under-counts true positives and can never manufacture a false
    positive — the safe direction."""
    ref = _DiscardingRef()
    _persist(ref, _job(parsed=_parsed(job_country="Germany")), _match(0), _profile())
    assert "geo_gate" not in ref.written
    assert gate_calls == []


def test_absent_rather_than_null_so_the_analysis_can_count_documents():
    ref = _KeepingRef()
    _persist(ref, _job(), _match(85), _profile())
    # "we didn't look" has to stay distinguishable from "we looked, found
    # nothing" — a null would collapse the two.
    assert "geo_gate" not in ref.written.keys()


def test_a_broken_gate_never_costs_a_paid_score():
    """persist_result runs after the Pro call is already billed. A profile the
    gate can't read (an old doc, a stub) must cost the recording, never the
    write."""
    ref = _KeepingRef()
    stub = SimpleNamespace()  # no .residence at all — geo.evaluate explodes

    assert _persist(ref, _job(parsed=_parsed()), _match(85), stub) == "scored"
    assert ref.written["match"]["overall_score"] == 85.0
    assert "geo_gate" not in ref.written


# ------------------------------------------------------------ the no-op proof


@pytest.mark.parametrize("score_value", [0, 12, 20, 85])
def test_recording_changes_nothing_about_the_outcome(score_value):
    """**The test this phase lives or dies by.** Same job, same match, once
    with the gate wired in and once without: identical outcome, identical
    document, down to the score and the recommendation. The only difference
    permitted is the recording itself."""
    job = _job(parsed=_parsed(job_country="Germany"))
    match = _match(score_value)
    discarding = score_value <= score.DISCARD_AT_OR_BELOW

    def run(profile):
        ref = _DiscardingRef() if discarding else _KeepingRef()
        return asyncio.run(score.persist_result(ref, job, match, profile=profile)), ref

    shadow_outcome, shadow_ref = run(_profile())
    plain_outcome, plain_ref = run(None)

    assert shadow_outcome == plain_outcome
    assert shadow_outcome == ("discarded" if discarding else "scored")
    strip = lambda d: {k: v for k, v in d.items() if k not in VOLATILE}  # noqa: E731
    assert strip(shadow_ref.written) == strip(plain_ref.written)
    if discarding:
        assert shadow_ref.deleted is plain_ref.deleted is True


def test_purge_discarded_still_gets_a_tombstone_without_a_verdict():
    """cli/purge_discarded calls discard_tombstone directly, passing its own
    scored_run_id and no gate — that caller is why both are explicit keywords
    rather than read from ambient state."""
    stone = score.discard_tombstone(_job(), _match(0), scored_run_id="older-run")
    assert stone["scored_run_id"] == "older-run"
    assert "geo_gate" not in stone


# ------------------------------------------------------- both scorers record


def test_online_scorer_records_and_tallies(monkeypatch, unlimited_budget):
    profile = _profile()
    refs = {
        "j1": _KeepingRef(),  # foreign office, no scope → ineligible
        "j2": _KeepingRef(),  # nothing to go on          → abstain
        "j3": _KeepingRef(),  # explicit US remote        → eligible, untallied
    }
    jobs = [
        (refs["j1"], _job("j1", parsed=_parsed(job_country="Germany"))),
        (refs["j2"], _job("j2", parsed=_parsed())),
        (refs["j3"], _job("j3", parsed=_parsed(us_remote_ok=True))),
    ]

    async def fake_load(db, user_id, limit=None):
        return profile, jobs

    async def fake_match(job, prof, cached_content=None):
        return _match(85)

    async def no_cache(prof, *a, **kw):
        return None

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert counts["scored"] == 3 and counts["failed"] == 0
    assert counts["geo_ineligible"] == 1
    assert counts["geo_abstain"] == 1
    # "eligible" is only ever reached by the us_remote_ok exception and would
    # skip no Pro call, so it gets no counter.
    assert "geo_eligible" not in counts
    assert refs["j1"].written["geo_gate"]["verdict"] == "ineligible"
    assert refs["j3"].written["geo_gate"]["rule"] == "us_remote_ok"


def test_a_run_that_scores_nothing_still_reports_the_geo_keys(
    monkeypatch, unlimited_budget
):
    """The counts contract must not depend on which branch produced it."""

    async def fake_load(db, user_id, limit=None):
        return _profile(), []

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert counts["geo_ineligible"] == 0 and counts["geo_abstain"] == 0


def test_batch_scorer_records_and_tallies(monkeypatch, unlimited_budget):
    """The half-price path persists through the same seam, so it has to carry
    the same recording — otherwise the measured coverage silently describes
    only whatever fraction of scoring happened to run online."""
    profile = _profile()
    ref = _KeepingRef()
    job = _job("j1", parsed=_parsed(job_country="Germany"))

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_run_batch(*, model, lines, **kw):
        assert model == batch.BATCH_PRO_MODEL  # already parsed: no Flash leg
        return [
            {
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": "CTX\n\nBLOCK"}]}]
                },
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"text": _match(85).model_dump_json()}],
                            }
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 5,
                        "totalTokenCount": 15,
                    },
                },
            }
        ]

    monkeypatch.setattr(batch, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(batch, "_run_batch", fake_run_batch)
    monkeypatch.setattr(batch, "build_match_context", lambda p: "CTX")
    monkeypatch.setattr(batch, "build_match_job_block", lambda j: "BLOCK")
    monkeypatch.setattr(batch, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(batch.batch_score_pending_jobs("u1"))

    assert counts["scored"] == 1
    assert counts["geo_ineligible"] == 1 and counts["geo_abstain"] == 0
    assert ref.written["geo_gate"]["rule"] == "country_mismatch"
