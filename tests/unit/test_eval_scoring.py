# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The scoring eval: what it measures, and what it refuses to claim.

Four properties carry the whole thing, and each one is a way the report would
be confidently wrong rather than visibly broken:

- It joins against ``discarded_jobs`` as well as ``jobs``. Everything the
  scorer rated at or below 20 was tombstoned, so reading ``jobs`` alone drops
  the negative class and computes AUC over positives only.
- AUC is tie-corrected. Geographically ineligible jobs all cap at exactly 20
  and one past corpus held a single distinct score end to end, so ties are
  the normal case, not an edge case.
- A metric the labels cannot support prints ``undefined``, never 0.5 and
  never 0.
- Parse fields are counted per field, and only where a label actually
  corrects one.

The ``--rescore`` path is tested with a stubbed scorer rather than a ``billed``
marker: ``addopts = "-m 'not billed'"`` deselects billed tests by default, so
one here would never run. What is worth pinning is free anyway — that no model
call is reachable without a confirmed quote.
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import cli.eval_scoring as ev
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile, Residence
from tools import genai_client
from tools.matching import batch_runs, rates

# ----------------------------------------------------------------- fixtures


class _Snap:
    def __init__(self, doc):
        self._doc = doc
        self.exists = doc is not None

    def to_dict(self):
        return dict(self._doc or {})


class _Doc:
    def __init__(self, doc, collections=None):
        self._doc = doc
        self._collections = collections or {}

    async def get(self):
        return _Snap(self._doc)

    def collection(self, name):
        return _Coll(self._collections.get(name, {}))


class _Coll:
    def __init__(self, docs):
        self._docs = docs

    def document(self, doc_id):
        return _Doc(self._docs.get(doc_id))


class _FakeDB:
    """Reads only. Any write would raise ``AttributeError``, which is how the
    rescore tests prove nothing is persisted back over the stored scores."""

    def __init__(self, user_id="u1", *, profile=None, jobs=None, tombstones=None):
        self._user_id = user_id
        self._doc = _Doc(
            profile,
            {"jobs": jobs or {}, "discarded_jobs": tombstones or {}},
        )

    def collection(self, name):
        assert name == "users", name
        return _Users(self._user_id, self._doc)


class _Users:
    def __init__(self, user_id, doc):
        self._user_id = user_id
        self._doc = doc

    def document(self, doc_id):
        return self._doc if doc_id == self._user_id else _Doc(None)


def _label(job_id="j1", *, fit=True, parse=None) -> ev.Label:
    return ev.Label(job_id=job_id, fit=fit, parse=dict(parse or {}))


def _row(score=None, *, fit=True, source=ev.JOBS, parse=None, job_id="j1", doc=None):
    if doc is None:
        doc = {} if score is None else {"match": {"overall_score": score}}
        if parse is not None:
            doc["jd_parsed"] = parse
    return ev.Joined(_label(job_id, fit=fit), source, doc)


def _profile() -> dict:
    return MasterProfile(
        user_id="u1",
        full_name="Test Candidate",
        email="test@example.com",
        location="Somewhere, Elsewhere",
        residence=Residence(country="US"),
        objective_template="{role} at {company}",
        experience=[],
        education=[],
        skills={},
        preferences={
            "target_role_families": ["engineering"],
            "target_titles": ["Staff Software Engineer"],
            "target_seniorities": ["staff"],
        },
    ).model_dump(mode="json")


def _job_doc(job_id: str, score: float | None = None, **extra) -> dict:
    doc = Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id=job_id,
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=f"Build things {job_id}.",
        discovered_at=datetime.now(UTC),
    ).model_dump(mode="json")
    if score is not None:
        doc["match"] = {"overall_score": score}
    doc.update(extra)
    return doc


class _AGeminiCallIsAboutToBeBilled(BaseException):
    """Not an ``Exception``: ``rescore``'s per-job handler catches those so one
    bad posting cannot abandon a paid run, and it would swallow the one signal
    that says the quote was bypassed."""


async def _refuse_to_bill(*args, **kwargs):
    raise _AGeminiCallIsAboutToBeBilled("cli.eval_scoring reached a model")


def _match(value: float) -> JobMatch:
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
        recommendation="apply",
        reasoning="Fine.",
    )


# ------------------------------------------------------------------- labels


def test_labels_skip_blanks_and_comments(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(
        "# a comment\n"
        "\n"
        '{"job_id": "a", "fit": true}\n'
        '{"job_id": "b", "fit": false, "parse": {"seniority": "staff"}}\n'
    )
    labels = ev.load_labels(path)
    assert [(label.job_id, label.fit) for label in labels] == [
        ("a", True),
        ("b", False),
    ]
    assert labels[1].parse == {"seniority": "staff"}


@pytest.mark.parametrize(
    "line",
    [
        '{"job_id": "a"}',  # no fit
        '{"job_id": "a", "fit": "yes"}',  # fit is not a bool
        '{"fit": true}',  # no job_id
        '{"job_id": "a", "fit": true, "parse": {"senority": "staff"}}',  # typo
        "not json",
    ],
)
def test_malformed_labels_are_refused_not_dropped(tmp_path, line):
    """A silently dropped row makes the metric a measurement of a corpus
    nobody chose."""
    path = tmp_path / "labels.jsonl"
    path.write_text(line + "\n")
    with pytest.raises(SystemExit):
        ev.load_labels(path)


def test_duplicate_job_ids_are_refused(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"job_id": "a", "fit": true}\n{"job_id": "a", "fit": false}\n')
    with pytest.raises(SystemExit):
        ev.load_labels(path)


def test_the_committed_example_matches_the_schema():
    """``data/eval/labels.jsonl`` ships as a worked example of the schema, so
    it has to stay loadable by the loader it documents."""
    labels = ev.load_labels(ev.DEFAULT_LABELS)
    assert labels
    assert all(set(label.parse) <= set(ev.PARSE_FIELDS) for label in labels)


# -------------------------------------------------------------------- join


def test_join_reads_tombstones_as_well_as_jobs():
    """The negative class lives in ``discarded_jobs``: ``persist_result``
    moves everything at or below 20 there. Joining over ``jobs`` alone finds
    only the jobs the scorer already liked."""
    db = _FakeDB(
        jobs={"kept": _job_doc("kept", 72.0)},
        tombstones={"tossed": {"score": 20.0, "jd_parsed": {"seniority": "junior"}}},
    )
    labels = [_label("kept"), _label("tossed", fit=False), _label("gone")]
    rows = asyncio.run(ev.join_labels(db, "u1", labels))

    assert [row.source for row in rows] == [ev.JOBS, ev.TOMBSTONES, ev.MISSING]
    assert [row.score for row in rows] == [72.0, 20.0, None]
    assert ev.split(rows) == {
        "labels": 3,
        "jobs": 1,
        "discarded_jobs": 1,
        "missing": 1,
        "scored": 2,
    }


def test_a_found_but_unscored_job_has_no_score():
    """A pending job that was never scored is found, not missing, and still
    contributes nothing to AUC."""
    db = _FakeDB(jobs={"j1": _job_doc("j1")})
    rows = asyncio.run(ev.join_labels(db, "u1", [_label("j1")]))
    assert rows[0].source == ev.JOBS
    assert rows[0].score is None
    assert ev.split(rows)["scored"] == 0


# --------------------------------------------------------------------- AUC


def test_auc_is_tie_corrected():
    """fit [60, 60] against unfit [60, 20]. Average ranks put the three 60s
    at rank 3, giving 0.75. Counting strict wins instead scores both ties as
    losses and reads 0.5."""
    result = ev.auc([60.0, 60.0], [60.0, 20.0])
    assert isinstance(result, ev.Auc)
    assert result.value == pytest.approx(0.75)
    assert result.tied_pairs == 2


def test_a_single_distinct_score_is_exactly_chance():
    """The corpus this was built for: one distinct score throughout. The
    tie-corrected answer is 0.5; a strict-wins count would say 0.0 and read
    as a scorer that ranks backwards."""
    result = ev.auc([20.0, 20.0], [20.0, 20.0, 20.0])
    assert isinstance(result, ev.Auc)
    assert result.value == pytest.approx(0.5)
    assert result.tied_pairs == 6


@pytest.mark.parametrize(
    ("fit", "unfit"),
    [([80.0, 70.0], []), ([], [20.0, 30.0]), ([], [])],
)
def test_auc_is_undefined_without_both_classes(fit, unfit):
    """Never 0.5, never 0, never a crash — and the reason carries the counts
    so the report says what is missing."""
    result = ev.auc(fit, unfit)
    assert isinstance(result, ev.Undefined)
    assert f"{len(fit)} fit / {len(unfit)} unfit" in result.reason


def test_a_small_set_reports_an_interval_that_spans_half():
    result = ev.auc([20.0, 20.0], [20.0, 20.0, 20.0])
    assert result.lo <= 0.5 <= result.hi
    assert result.spans_half is True


def test_a_cleanly_separated_set_does_not_span_half():
    result = ev.auc([90.0] * 20, [10.0] * 20)
    assert result.value == pytest.approx(1.0)
    assert result.spans_half is False


def test_a_perfect_auc_on_four_labels_is_not_claimed_as_certain():
    """The Hanley-McNeil standard error is exactly zero at AUC 1.0 whatever
    the sample size, which would print [1.000, 1.000] off two jobs a side.
    The rule-of-three bound over the pair count keeps the interval honest."""
    result = ev.auc([72.0, 64.0], [20.0, 20.0])
    assert result.value == pytest.approx(1.0)
    assert result.lo == pytest.approx(1.0 - 3.0 / 4)
    assert result.spans_half is True


def test_a_perfect_auc_narrows_as_the_set_grows():
    small = ev.auc([90.0] * 3, [10.0] * 3)
    large = ev.auc([90.0] * 20, [10.0] * 20)
    assert large.lo > small.lo
    assert large.spans_half is False


def test_average_ranks_share_rank_across_ties():
    assert ev.average_ranks([20.0, 60.0, 60.0, 60.0]) == [1.0, 3.0, 3.0, 3.0]


# -------------------------------------------------------- precision / recall


def test_precision_and_recall_at_the_queue_threshold():
    rows = [
        _row(90.0, fit=True),
        _row(70.0, fit=False),
        _row(40.0, fit=True),
        _row(20.0, fit=False),
        _row(None, fit=True),  # unscored: contributes to neither
    ]
    table = ev.at_threshold(rows, ev.QUEUE_DEFAULT_MIN_SCORE)
    assert (table.tp, table.fp, table.fn, table.tn) == (1, 1, 1, 1)
    assert table.precision == pytest.approx(0.5)
    assert table.recall == pytest.approx(0.5)


def test_precision_is_undefined_when_nothing_clears_the_threshold():
    table = ev.at_threshold([_row(20.0, fit=True), _row(30.0, fit=False)], 60)
    assert isinstance(table.precision, ev.Undefined)
    assert "no job scored >= 60" in table.precision.reason
    assert table.recall == pytest.approx(0.0)


def test_recall_is_undefined_without_a_labelled_fit():
    table = ev.at_threshold([_row(90.0, fit=False)], 60)
    assert isinstance(table.recall, ev.Undefined)


# ------------------------------------------------------------- parse fields


def test_parse_fields_are_reported_per_field_and_never_averaged():
    rows = [
        ev.Joined(
            _label("a", parse={"seniority": "staff", "job_country": "US"}),
            ev.JOBS,
            {
                "match": {"overall_score": 90.0},
                "jd_parsed": {"seniority": "staff", "job_country": "CA"},
            },
        ),
        ev.Joined(
            _label("b", parse={"seniority": "staff"}),
            ev.JOBS,
            {
                "match": {"overall_score": 80.0},
                "jd_parsed": {"seniority": "senior", "job_country": "US"},
            },
        ),
    ]
    tallies = ev.parse_field_accuracy(rows)
    assert set(tallies) == {"seniority", "job_country"}
    assert (tallies["seniority"].agreed, tallies["seniority"].checked) == (1, 2)
    assert (tallies["job_country"].agreed, tallies["job_country"].checked) == (0, 1)


def test_a_field_a_label_omits_is_not_checked():
    """Not "agreed": counting unexamined fields as agreement hands every
    field the label never looked at a free point."""
    rows = [
        ev.Joined(
            _label("a", parse={"seniority": "staff"}),
            ev.JOBS,
            {"jd_parsed": {"seniority": "staff", "job_country": "US"}},
        )
    ]
    tallies = ev.parse_field_accuracy(rows)
    assert set(tallies) == {"seniority"}
    assert "job_country" not in tallies
    assert "remote_policy" not in tallies


def test_a_job_with_no_stored_parse_is_checked_but_not_agreed():
    rows = [ev.Joined(_label("a", parse={"seniority": "staff"}), ev.JOBS, {})]
    tally = ev.parse_field_accuracy(rows)["seniority"]
    assert (tally.checked, tally.agreed, tally.no_parse) == (1, 0, 1)


def test_field_comparison_ignores_case_and_surrounding_space():
    rows = [
        ev.Joined(
            _label("a", parse={"job_country": "us"}),
            ev.JOBS,
            {"jd_parsed": {"job_country": " US "}},
        )
    ]
    assert ev.parse_field_accuracy(rows)["job_country"].agreed == 1


# ------------------------------------------------------------- default mode


def test_default_mode_makes_no_model_call(monkeypatch, capsys, tmp_path):
    """The whole free path — load, join, report — with the Vertex client
    booby-trapped. A ``BaseException`` so no broad ``except Exception`` in the
    code under test can swallow it."""

    def _refuse(*args, **kwargs):
        raise _AGeminiCallIsAboutToBeBilled("cli.eval_scoring reached a model")

    monkeypatch.setattr(genai_client, "vertex_client", _refuse)

    path = tmp_path / "labels.jsonl"
    path.write_text(
        '{"job_id": "kept", "fit": true}\n{"job_id": "tossed", "fit": false}\n'
    )
    db = _FakeDB(
        jobs={"kept": _job_doc("kept", 72.0)},
        tombstones={"tossed": {"score": 20.0}},
    )
    labels = ev.load_labels(path)
    rows = asyncio.run(ev.join_labels(db, "u1", labels))
    ev.report(rows, user_id="u1", labels_path=path, title="stored scores")

    out = capsys.readouterr().out
    assert "found 1 in jobs, 1 in discarded_jobs, 0 missing" in out
    assert "AUC of overall_score against fit" in out


def test_match_job_is_reachable_only_from_rescore():
    """The structural half of "default mode spends nothing": the paid call
    appears in exactly one function, and it is the one behind the quote."""
    tree = ast.parse(Path(ev.__file__).read_text())
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", None) == "match_job"
            ):
                callers.add(node.name)
    assert callers == {"rescore"}


def test_report_says_undefined_rather_than_printing_a_number(capsys):
    rows = [_row(90.0, fit=True), _row(80.0, fit=True)]
    ev.report(rows, user_id="u1", labels_path=Path("x.jsonl"), title="stored scores")
    out = capsys.readouterr().out
    assert "undefined (needs both classes; have 2 fit / 0 unfit)" in out
    assert "0.500" not in out


# ----------------------------------------------------------------- rescore


def test_the_quote_uses_the_rated_job_rate_not_the_blended_one():
    """Every eval job pays both legs — a Flash parse and a Pro score — so the
    blended per-attempted rate understates the quote by about half."""
    quote = ev.rescore_quote(10)
    assert quote.rate_usd == pytest.approx(rates.MEASURED_RATED_JOB_USD)
    assert quote.rate_source == rates.SOURCE_MEASURED_RATED
    assert quote.usd_low == pytest.approx(round(10 * rates.MEASURED_RATED_JOB_USD, 2))
    # The trap, stated as a number: the blended constant is ~half of this.
    blended = round(10 * rates.MEASURED_COST_PER_JOB_USD, 2)
    assert quote.usd_low > blended


def test_the_quote_does_not_offer_the_batch_price():
    """``estimate.quote`` discounts its floor to the batch rate above 50
    units. A rescore calls ``match_job`` per job and has no batch path, so
    that floor would advertise a price it cannot deliver."""
    units = batch_runs.BATCH_MIN_PENDING + 10
    quote = ev.rescore_quote(units)
    assert quote.usd_low == pytest.approx(
        round(units * rates.MEASURED_RATED_JOB_USD, 2)
    )
    batch_floor = round(
        units * rates.MEASURED_RATED_JOB_USD * rates.BATCH_MULTIPLIER, 2
    )
    assert quote.usd_low > batch_floor


def test_confirm_takes_only_a_typed_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    assert ev.confirm("?", assume_yes=False) is True
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert ev.confirm("?", assume_yes=False) is False
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert ev.confirm("?", assume_yes=False) is False

    def _closed(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", _closed)
    assert ev.confirm("?", assume_yes=False) is False


def test_only_the_yes_flag_skips_the_prompt(monkeypatch):
    def _refuse(*_):
        raise AssertionError("asked for confirmation despite --yes")

    monkeypatch.setattr("builtins.input", _refuse)
    assert ev.confirm("?", assume_yes=True) is True


def test_rescore_makes_no_model_call_without_confirmation(
    monkeypatch, capsys, tmp_path, unlimited_budget
):
    """The behaviour the plan wanted a ``billed`` test for, tested for free:
    declining the quote has to be indistinguishable from never having run."""
    monkeypatch.setattr(ev, "match_job", _refuse_to_bill)
    monkeypatch.setattr("builtins.input", lambda *_: "no")

    db = _FakeDB(profile=_profile(), jobs={"j1": _job_doc("j1", 72.0)})
    rows = asyncio.run(ev.join_labels(db, "u1", [_label("j1")]))
    out_path = tmp_path / "rescore.jsonl"
    result = asyncio.run(
        ev.rescore(
            db,
            "u1",
            rows,
            assume_yes=False,
            ignore_budget=False,
            out_path=out_path,
        )
    )
    assert result is None
    assert not out_path.exists()
    assert "No model call was made." in capsys.readouterr().out
    assert unlimited_budget == []  # not even a budget slot was reserved


def test_rescore_scores_and_writes_each_result_as_it_lands(
    monkeypatch, tmp_path, unlimited_budget
):
    calls = []

    async def _fake_match(job, profile):
        calls.append(job.id)
        job.jd_parsed = ParsedJD(role_family="engineering", summary="Build.")
        return _match(88.0)

    monkeypatch.setattr(ev, "match_job", _fake_match)

    db = _FakeDB(profile=_profile(), jobs={"j1": _job_doc("j1", 72.0)})
    rows = asyncio.run(ev.join_labels(db, "u1", [_label("j1")]))
    out_path = tmp_path / "rescore.jsonl"
    fresh = asyncio.run(
        ev.rescore(
            db, "u1", rows, assume_yes=True, ignore_budget=False, out_path=out_path
        )
    )

    assert calls == ["j1"]
    assert fresh is not None
    assert [row.score for row in fresh] == [88.0]
    written = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert written[0]["job_id"] == "j1"
    assert written[0]["score"] == 88.0
    assert written[0]["jd_parsed"]["role_family"] == "engineering"


def test_rescore_skips_jobs_it_cannot_rebuild(monkeypatch, capsys, tmp_path):
    """A tombstone keeps no ``jd_raw``, so most rejected jobs cannot be
    rescored at all — said out loud rather than quietly dropped."""

    monkeypatch.setattr(ev, "match_job", _refuse_to_bill)
    db = _FakeDB(profile=_profile(), tombstones={"j1": {"score": 20.0}})
    rows = asyncio.run(ev.join_labels(db, "u1", [_label("j1", fit=False)]))
    result = asyncio.run(
        ev.rescore(
            db,
            "u1",
            rows,
            assume_yes=True,
            ignore_budget=False,
            out_path=tmp_path / "rescore.jsonl",
        )
    )
    assert result is None
    assert "Nothing to rescore" in capsys.readouterr().out
