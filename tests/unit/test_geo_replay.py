# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.geo_replay``'s two reports: the tombstone census and the
``GEO_GATE_ENFORCE`` readiness counts.

The replay itself streams Firestore and is exercised against the real thing.
What is unit-testable is the printing, which runs at the very end of a long
read — so an arithmetic error there costs the whole run. The readiness counts
are additionally unit-testable end to end, since they are arithmetic over
stored maps rather than over a re-run rule.
"""

from __future__ import annotations

import asyncio

from cli.geo_replay import (
    SHADOW_SOURCES,
    Replay,
    ShadowTally,
    readiness_user,
    replay_user,
    report,
    report_readiness,
)
from tools.matching import rates


def test_a_history_of_only_free_rejects_does_not_divide_by_zero(capsys):
    """``pro_calls`` is the denominator of the capped-at-20 share, and it is
    zero for an account whose tombstones all scored 0 (out-of-family, never a
    Pro call). Dividing anyway raises at the end of the stream and takes the
    whole report with it."""
    r = Replay(user_id="u1", tombstones=4, tombstones_free=4, tombstones_capped=0)

    report(r, title="u1")

    out = capsys.readouterr().out
    assert "Pro calls in this history: 0" in out
    assert "out-of-family" in out
    # No share, because there is nothing to take a share of.
    assert "ceiling on what this gate can save" not in out


def test_the_census_does_not_claim_the_tombstones_are_unreplayable(capsys):
    """Tombstones have carried ``jd_parsed`` since ``score.discard_tombstone``
    started storing it, so the gate *can* be replayed over them — this run
    counts them instead."""
    r = Replay(user_id="u1", tombstones=10, tombstones_free=3, tombstones_capped=5)

    report(r, title="u1")

    out = capsys.readouterr().out
    assert "no jd_parsed" not in out
    assert "carry jd_parsed" in out
    assert "Pro calls in this history: 7" in out


# --------------------------------------------------------------------------
# The readiness report
# --------------------------------------------------------------------------


def _rec(verdict, score, *, rule="country_mismatch", **kw):
    """One ``score.shadow_geo_gate`` record, in the shape it is stored."""
    return {
        "version": 1,
        "verdict": verdict,
        "rule": rule,
        "residence_country": "US",
        "job_country": "DE",
        "pro_score": score,
        "pro_capped": score == 20,
        "pro_geo_flag": False,
        **kw,
    }


def _tallies(jobs=(), discarded=()):
    """Two tallies fed record-by-record, as ``readiness_user`` would."""
    out = {s: ShadowTally(source=s) for s in SHADOW_SOURCES}
    for source, records in (("jobs", jobs), ("discarded_jobs", discarded)):
        for rec in records:
            out[source].docs += 1
            out[source].record(rec, {"company": "Acme", "title": "Staff Engineer"})
    return out


def test_only_a_job_pro_kept_counts_as_a_false_positive():
    """The three ``ineligible`` outcomes are not interchangeable. Pro above 20
    is a live job the gate would silently dismiss; Pro at exactly 20 is a Pro
    call that bought nothing; Pro below 20 is a job that was discarded anyway,
    so the gate costs nothing there. Folding the last into the first is how a
    go/no-go number gets inflated out of harmless disagreements."""
    t = _tallies(jobs=[_rec("ineligible", 74.0)])["jobs"]
    assert (t.fp, t.tp, t.moot) == (1, 0, 0)

    t = _tallies(jobs=[_rec("ineligible", 20.0)])["jobs"]
    assert (t.fp, t.tp, t.moot) == (0, 1, 0)

    t = _tallies(jobs=[_rec("ineligible", 12.0)])["jobs"]
    assert (t.fp, t.tp, t.moot) == (0, 0, 1)


def test_abstain_is_not_in_the_agreement_denominator():
    """An abstain asserts nothing, so counting it as agreement would let a
    gate that never fires report near-perfect agreement with Pro."""
    t = _tallies(
        jobs=[
            _rec("abstain", 74.0, rule="abstain_unsettled"),
            _rec("abstain", 20.0, rule="abstain_unsettled"),
            _rec("eligible", 74.0, rule="us_remote_ok"),
        ]
    )["jobs"]

    assert t.shadow == 3
    assert t.claims == 1
    assert t.agree == 1
    # The abstain over a job Pro capped at 20 is coverage left on the table.
    assert t.missed == 1


def test_a_gate_enforced_record_is_never_graded_against_pro():
    """``score.enforced_geo_gate`` records carry no ``pro_*`` keys, because
    the gate skipped the call. Counting them as verdicts would let the gate
    grade its own homework and manufacture perfect agreement."""
    enforced = {
        "version": 1,
        "verdict": "ineligible",
        "rule": "country_mismatch",
        "residence_country": "US",
        "job_country": "DE",
        "enforced": True,
    }
    t = _tallies(discarded=[enforced])["discarded_jobs"]

    assert t.enforced == 1
    assert t.shadow == 0 and t.claims == 0 and t.avoidable == 0
    assert t.versions[1] == 1


def test_an_account_with_no_shadow_verdicts_prints_counts_not_percentages(capsys):
    """An account scored entirely before shadow recording shipped has docs and
    no verdicts. Every rate below is 0/0, so none may be printed."""
    empty = {s: ShadowTally(source=s, docs=7, no_record=7) for s in SHADOW_SOURCES}

    report_readiness(empty, title="users/u1")

    out = capsys.readouterr().out
    assert "No shadow verdicts recorded" in out
    assert "%" not in out.split("No shadow verdicts recorded")[1]
    assert "0.0%" not in out


def test_a_gate_that_only_abstains_reports_an_undefined_agreement_rate(capsys):
    """Verdicts exist but no claims, so the agreement denominator is zero.
    ``undefined`` beside the counts, never a fabricated 0.0%."""
    report_readiness(
        _tallies(jobs=[_rec("abstain", 74.0, rule="abstain_unsettled")]),
        title="users/u1",
    )

    out = capsys.readouterr().out
    assert "agree          :     0  = undefined" in out
    assert "false positives:     0  = undefined" in out


def test_the_false_positive_rate_carries_an_upper_bound(capsys):
    """Zero false positives out of 20 and out of 2,000 are very different
    grounds for a flag that silently dismisses live jobs, and a bare 0.0%
    hides which one this is."""
    report_readiness(
        _tallies(jobs=[_rec("eligible", 74.0, rule="us_remote_ok")] * 20),
        title="users/u1",
    )

    out = capsys.readouterr().out
    # Rule of three over 20 records: 300/20 = 15%.
    assert "jobs           : 0 of 20 = 0.0%, 95% upper bound 15.00%" in out


def test_the_two_corpora_are_reported_separately(capsys):
    """A reader must not be able to quote a false-positive rate off the
    tombstones, where every record scored 20 or less so Pro kept none of them
    and the rate is structurally zero."""
    report_readiness(
        _tallies(
            jobs=[_rec("ineligible", 74.0)],
            discarded=[_rec("ineligible", 20.0)] * 9,
        ),
        title="users/u1",
    )

    out = capsys.readouterr().out
    assert "jobs           : 1 of 1 = 100.0%" in out
    assert "discarded_jobs : not measurable" in out
    assert "survivorship-filtered" in out


def test_the_saving_is_priced_at_the_score_leg_not_the_blended_rate(capsys):
    """``pipeline.prefilter`` runs on ``jd_parsed``, so the parse leg is paid
    before the gate can fire and only the score leg is saved. The blended
    per-job-attempted rate must not appear as the saving: it averages in free
    pre-filter rejects and prices a job that reached Pro too low."""
    report_readiness(_tallies(discarded=[_rec("ineligible", 20.0)] * 100), title="u1")

    out = capsys.readouterr().out
    assert f"at ${rates.MEASURED_SCORE_USD:.5f} per score leg" in out
    assert f"= ${100 * rates.MEASURED_SCORE_USD:.2f} of Pro" in out
    # The rated-job rate is named as the ceiling, not quoted as the saving.
    assert f"${100 * rates.MEASURED_RATED_JOB_USD:.2f} as fully rated jobs" in out
    assert (
        f"${rates.MEASURED_COST_PER_JOB_USD:.5f} per job *attempted* is not used" in out
    )


def test_the_readiness_report_states_it_is_not_a_replay(capsys):
    """Two numbers for the same gate live in this module — recorded verdicts
    and today's recomputation — and they can disagree after a GATE_VERSION
    bump. The output has to say which one it is."""
    report_readiness(
        _tallies(jobs=[_rec("eligible", 74.0, rule="us_remote_ok")]), title="u1"
    )

    out = capsys.readouterr().out
    assert "not a replay" in out
    assert "gate versions behind those records: v1 x1" in out


def test_the_readiness_report_makes_no_recommendation(capsys):
    """Flipping ``GEO_GATE_ENFORCE`` is the operator's call. A tool that
    answered it would be read instead of the numbers."""
    report_readiness(
        _tallies(jobs=[_rec("eligible", 74.0, rule="us_remote_ok")] * 500),
        title="u1",
    )

    out = capsys.readouterr().out.lower()
    for word in ("recommend", "safe to", "should enable", "ready to enable"):
        assert word not in out


# --------------------------------------------------------------------------
# Reading the two collections
# --------------------------------------------------------------------------


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Collection:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _Doc(self._store, f"{self._path}/{doc_id}")

    async def stream(self):
        for path in sorted(self._store):
            head, _, tail = path.rpartition("/")
            if head == self._path and tail:
                yield _Snap(self._store[path])


class _Doc:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _Collection(self._store, f"{self._path}/{name}")

    async def get(self):
        return _Snap(self._store.get(self._path))


class _DB:
    """Keyed by full document path, so scoping bugs show up in the keys."""

    def __init__(self, docs):
        self.store = dict(docs)

    def collection(self, name):
        return _Collection(self.store, name)


def test_readiness_counts_both_collections_and_records_with_no_verdict():
    """Coverage has to be visible: a doc with no ``geo_gate`` map predates
    shadow recording, and silently dropping it would make a thin sample look
    complete."""
    db = _DB(
        {
            "users/u1": {"email": "x@y.z"},
            "users/u1/jobs/a": {"geo_gate": _rec("eligible", 74.0)},
            "users/u1/jobs/b": {"match": {"overall_score": 70}},
            "users/u1/discarded_jobs/c": {"geo_gate": _rec("ineligible", 20.0)},
            "users/u1/discarded_jobs/d": {"score": 4},
            # Another user's docs must not leak into u1's counts.
            "users/u2/jobs/e": {"geo_gate": _rec("ineligible", 99.0)},
        }
    )

    tallies = asyncio.run(readiness_user(db, "u1"))

    assert tallies is not None
    assert (tallies["jobs"].docs, tallies["jobs"].shadow) == (2, 1)
    assert tallies["jobs"].no_record == 1
    assert (tallies["discarded_jobs"].docs, tallies["discarded_jobs"].shadow) == (2, 1)
    assert tallies["discarded_jobs"].no_record == 1
    assert tallies["jobs"].fp == 0


def test_readiness_on_a_missing_user_returns_none_rather_than_empty_counts():
    """An empty report for a typo'd uid reads as "zero false positives"."""
    assert asyncio.run(readiness_user(_DB({}), "nope")) is None


def test_pruned_tombstones_stay_out_of_the_pro_call_denominator():
    """A prune is not a Pro call; counting it would shrink the capped share."""
    profile = {
        "user_id": "u1",
        "full_name": "Test Candidate",
        "email": "test@example.com",
        "location": "Somewhere",
        "objective_template": "{role} at {company}",
        "experience": [],
        "education": [],
        "skills": {},
        "preferences": {
            "target_role_families": ["engineering"],
            "target_titles": ["Staff Software Engineer"],
            "target_seniorities": ["staff"],
        },
        "residence": {"country": "US"},
    }
    db = _DB(
        {
            "users/u1": profile,
            "users/u1/discarded_jobs/a": {"score": 20},
            "users/u1/discarded_jobs/b": {"score": 0},
            "users/u1/discarded_jobs/c": {"score": 55},
            "users/u1/discarded_jobs/d": {"pruned": {"by": "prerank"}},
        }
    )
    r = asyncio.run(replay_user(db, "u1", with_discarded=True))
    assert r is not None
    assert (r.tombstones, r.tombstones_capped, r.tombstones_free) == (3, 1, 1)
    assert r.pro_calls == 2
