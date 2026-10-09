# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The prerank eval: what it measures, and the ways it could flatter itself.

The one that matters most is the mask. ``location``, ``jd_parsed`` and
``discovered_at`` live on job docs and mostly not on tombstones, and job docs
are mostly Pro's survivors, so a term built on them separates the collections
rather than the jobs. The gating AUC must not move when those fields appear.
"""

from __future__ import annotations

import re

import pytest
from firestore_fakes import FakeQueryDB, _Query, _QueryColl, _QueryDoc

import cli.prerank_eval as pe
from models.job import ParsedJD
from tools.matching import prerank as pr
from tools.matching.score import load_profile_and_pending

PREFS = {
    "target_role_families": ["engineering"],
    "target_titles": ["Staff Software Engineer"],
    "target_seniorities": ["staff"],
}
USER = {"preferences": PREFS, "residence": {"country": "US"}}

GOOD = "Staff Software Engineer"
BAD = "Account Executive"


@pytest.fixture(autouse=True)
def curated(monkeypatch):
    monkeypatch.setattr(pr, "curated_slugs", lambda: frozenset({"acme"}))


def _job(title: str, score: float, **extra) -> dict:
    return {
        "title": title,
        "company": "acme",
        "user_decision": "approved",
        "match": {"overall_score": score},
        **extra,
    }


def _tomb(title: str, score: float = 0) -> dict:
    return {"title": title, "company": "acme", "score": score}


def _db(jobs: dict, tombs: dict, *, uid: str = "u1", user=USER) -> FakeQueryDB:
    return FakeQueryDB(
        {
            "users": {uid: user},
            f"users/{uid}/jobs": jobs,
            f"users/{uid}/discarded_jobs": tombs,
        }
    )


def _corpus(pos: list[str], neg: list[str], prefix: str = "") -> tuple[dict, dict]:
    """Positives as scored job docs, negatives as tombstones."""
    jobs = {f"{prefix}p{i}": _job(t, 80) for i, t in enumerate(pos)}
    tombs = {f"{prefix}n{i}": _tomb(t) for i, t in enumerate(neg)}
    return jobs, tombs


async def _load(db, uid="u1", **kw) -> pe.Corpus:
    corpus = await pe.load_user(db, uid, **kw)
    assert corpus is not None
    return corpus


# --------------------------------------------------------------- the gate


@pytest.mark.asyncio
async def test_a_separating_prerank_passes():
    corpus = await _load(_db(*_corpus([GOOD] * 30, [BAD] * 30)))
    g = pe.gate(corpus.rows)
    assert isinstance(g.masked, pe.Auc)
    assert g.masked.value == 1.0
    assert (g.n_pos, g.n_neg) == (30, 30)
    assert g.verdict == "PASS", g.reasons


@pytest.mark.asyncio
async def test_a_content_free_corpus_reads_as_chance():
    mixed = [GOOD, BAD] * 15
    corpus = await _load(_db(*_corpus(mixed, mixed)))
    g = pe.gate(corpus.rows)
    assert isinstance(g.masked, pe.Auc)
    assert g.masked.value == pytest.approx(0.5)
    assert g.verdict == "FAIL"


@pytest.mark.asyncio
async def test_b1_depends_on_the_ids_alone():
    a = await _load(_db(*_corpus([GOOD] * 30, [BAD] * 30)))
    b = await _load(_db(*_corpus([BAD] * 30, [GOOD] * 30)))
    ga, gb = pe.gate(a.rows), pe.gate(b.rows)
    assert isinstance(ga.b1, pe.Auc) and isinstance(gb.b1, pe.Auc)
    assert ga.b1.value == gb.b1.value
    assert isinstance(ga.masked, pe.Auc) and isinstance(gb.masked, pe.Auc)
    assert (ga.masked.value, gb.masked.value) == (1.0, 0.0)


@pytest.mark.asyncio
async def test_below_thirty_positives_is_undefined_not_fail():
    corpus = await _load(_db(*_corpus([GOOD] * 29, [BAD] * 40)))
    g = pe.gate(corpus.rows)
    assert g.verdict == "UNDEFINED"
    assert "29 positive" in g.reasons[0]


@pytest.mark.asyncio
async def test_job_only_fields_cannot_move_the_gating_auc():
    """Give every job doc a foreign location, a rejecting parse and a
    timestamp — fields no tombstone carries. Unmasked they would bury every
    positive; the gate must not notice."""
    pos, neg = [GOOD] * 20 + [BAD] * 20, ["Backend Engineer"] * 20 + [BAD] * 20
    jobs, tombs = _corpus(pos, neg)
    before = pe.gate((await _load(_db(jobs, tombs))).rows)

    leaky = {
        job_id: {
            **doc,
            "location": "London, UK",
            "jd_parsed": {"role_family": "sales", "summary": "x"},
            "discovered_at": "2026-09-01T00:00:00Z",
        }
        for job_id, doc in jobs.items()
    }
    rows = (await _load(_db(leaky, tombs))).rows
    after = pe.gate(rows)

    assert isinstance(before.masked, pe.Auc) and isinstance(after.masked, pe.Auc)
    assert after.masked == before.masked
    unmasked = pe.auc(*pe.split_scores(rows, lambda r: r.full.score))
    assert isinstance(unmasked, pe.Auc)
    assert unmasked.value < before.masked.value  # the fields do leak, unmasked


# ------------------------------------------------------- listings, metrics


async def _foreign_positive_corpus() -> pe.Corpus:
    jobs, tombs = _corpus([GOOD] * 30, [BAD] * 30)
    jobs["job-buried"] = _job(BAD, 90, location="London, UK")
    return await _load(_db(jobs, tombs))


@pytest.mark.asyncio
async def test_a_foreign_positive_is_listed_but_does_not_gate(capsys):
    corpus = await _foreign_positive_corpus()

    assert [r.job_id for r in pe.false_buries(corpus.rows)] == ["job-buried"]
    assert pe.location_falsifications(corpus.rows) == []
    assert [r.job_id for r in pe.foreign_positives(corpus.rows)] == ["job-buried"]
    g = pe.report(corpus, half=None)
    assert g.verdict == "PASS", g.reasons
    assert g.falsifications == 0
    out = capsys.readouterr().out
    assert "3. Location falsifications: 0" in out
    assert "does not gate: 1 positive(s) carry the foreign location signal" in out
    assert "4. False buries (positives below the masked median): 1" in out
    assert out.count("job-buried") == 2


@pytest.mark.asyncio
async def test_a_negative_location_weight_turns_the_gate_back_on(monkeypatch, capsys):
    monkeypatch.setattr(pr, "W_LOCATION_FOREIGN", -30.0)
    corpus = await _foreign_positive_corpus()

    assert [r.job_id for r in pe.location_falsifications(corpus.rows)] == ["job-buried"]
    g = pe.report(corpus, half=None)
    assert g.verdict == "FAIL"
    assert "1 location falsification(s)" in g.reasons
    assert "3. Location falsifications: 1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_foreign_positives_list_at_most_ten_sorted_by_id(capsys):
    jobs, tombs = _corpus([GOOD] * 30, [BAD] * 30)
    for i in reversed(range(12)):
        jobs[f"f{i:02d}"] = _job(GOOD, 80, location="London, UK")
    corpus = await _load(_db(jobs, tombs))

    foreign = pe.foreign_positives(corpus.rows)
    assert [r.job_id for r in foreign] == [f"f{i:02d}" for i in range(12)]
    pe.report(corpus, half=None)
    out = capsys.readouterr().out
    assert "12 positive(s) carry the foreign location signal" in out
    listed = re.findall(r"^\s+(f\d\d)  Pro ", out, flags=re.MULTILINE)
    assert listed == [f"f{i:02d}" for i in range(10)]


def test_a_tie_at_the_median_is_not_a_bury():
    rows = [
        pe.Row(
            f"j{i}", pe.JOBS, {}, 80 if i < 2 else 0, pr.Prerank(1.0), pr.Prerank(1.0)
        )
        for i in range(4)
    ]
    assert pe.false_buries(rows) == []


def test_top_decile_counts_a_straddling_tie_block_fractionally():
    scores = [9.0, 9.0] + [1.0] * 8
    labels = [True, False] + [False] * 8
    assert pe.precision_at_top(scores, labels) == pytest.approx(0.5)
    # A block entirely inside the cut counts whole.
    assert pe.precision_at_top([9.0] + [1.0] * 9, [True] + [False] * 9) == 1.0


def test_pooled_auc_compares_within_users_only():
    u1 = ([5.0], [0.0])
    u2 = ([20.0], [10.0])
    assert pe.pooled_auc([u1, u2]) == 1.0
    one_global = pe.auc([5.0, 20.0], [0.0, 10.0])
    assert isinstance(one_global, pe.Auc) and one_global.value == 0.75
    assert isinstance(pe.pooled_auc([([1.0], [])]), pe.Undefined)


@pytest.mark.asyncio
async def test_feature_stats_report_fires_and_lift():
    corpus = await _load(_db(*_corpus([GOOD] * 10, [BAD] * 30)))
    stats = pe.feature_stats(corpus.rows)
    assert stats["title_exact"].fires == pytest.approx(0.25)
    assert stats["title_exact"].lift == pytest.approx(4.0)
    assert "location" not in stats


@pytest.mark.asyncio
async def test_feature_stats_read_family_masked_and_parsed_unmasked(capsys):
    """An in-family parse zeroes ``family`` in the full prerank; the table must
    still see the title family on those rows."""
    jobs = {
        f"p{i}": _job(
            GOOD, 80, jd_parsed={"role_family": "engineering", "summary": "x"}
        )
        for i in range(10)
    }
    tombs = {f"n{i}": _tomb(BAD) for i in range(10)}
    corpus = await _load(_db(jobs, tombs))
    assert all(r.full.features["family"] == 0 for r in corpus.rows if r.positive)

    stats = pe.feature_stats(corpus.rows)
    assert stats["family"].fires == 1.0
    assert stats["parsed"].fires == pytest.approx(0.5)
    assert stats["parsed"].lift == pytest.approx(2.0)

    pe.report(corpus, half=None)
    out = capsys.readouterr().out
    assert "2. Per feature, masked" in out
    assert "parsed        * fires" in out
    assert "family          fires 100.0%" in out


@pytest.mark.asyncio
async def test_location_signal_is_a_weightless_diagnostic_row(capsys):
    jobs, tombs = _corpus([GOOD] * 3, [BAD] * 5)
    jobs["p0"]["location"] = "London, UK"
    corpus = await _load(_db(jobs, tombs))
    sig = pe.location_signal_stats(corpus.rows)
    assert (sig.overall, sig.positives) == (pytest.approx(1 / 8), pytest.approx(1 / 3))
    pe.report(corpus, half=None)
    out = capsys.readouterr().out
    assert "signal only, NO WEIGHT: foreign on 12.5% of rows, 33.3% of positives" in out


# ---------------------------------------------------------------- backlog


@pytest.mark.asyncio
async def test_backlog_uses_the_scorers_pending_predicate(capsys):
    def full(job_id: str, **extra) -> dict:
        return {
            "id": job_id,
            "user_id": "u1",
            "source": "greenhouse",
            "source_id": job_id,
            "company": "acme",
            "title": GOOD,
            "url": f"https://example.com/{job_id}",
            "jd_raw": "jd",
            "discovered_at": "2026-09-01T00:00:00Z",
            **extra,
        }

    jobs = {
        "pending-1": full("pending-1", user_decision="pending"),
        "pending-2": full("pending-2", user_decision="pending", location="London, UK"),
        "pending-scored": full(
            "pending-scored", user_decision="pending", match={"overall_score": 70}
        ),
        "approved-unscored": full("approved-unscored", user_decision="approved"),
        "no-decision": full("no-decision"),
    }
    profile = {
        "user_id": "u1",
        "full_name": "Test Candidate",
        "email": "test@example.com",
        "location": "Somewhere",
        "objective_template": "{role} at {company}",
        "experience": [],
        "education": [],
        "skills": {},
        **USER,
    }
    db = _db(jobs, {}, user=profile)
    _, pending = await load_profile_and_pending(db, "u1")
    corpus = await _load(db)
    assert sorted(corpus.backlog.job_ids) == sorted(j.id for _, j in pending)
    assert sorted(corpus.backlog.job_ids) == ["pending-1", "pending-2"]
    assert corpus.backlog.foreign_signals == 1
    pe.report(corpus, half=None)
    assert "foreign location signal on 1 (no weight)" in capsys.readouterr().out


def test_tie_mass_at_the_top():
    assert pe.tie_mass_at_top([9, 5, 5, 5, 1]) == (5, 3)
    assert pe.tie_mass_at_top([9, 5]) is None


@pytest.mark.asyncio
async def test_with_cache_counts_hits_and_prefilter_rejects(monkeypatch):
    jobs = {
        "a": {"title": GOOD, "user_decision": "pending", "jd_raw": "jd-a"},
        "b": {"title": BAD, "user_decision": "pending", "jd_raw": "jd-b"},
        "c": {"title": BAD, "user_decision": "pending", "jd_raw": "jd-c"},
    }
    cached = {
        "jd-a": ParsedJD(role_family="engineering", summary="x"),
        "jd-b": ParsedJD(role_family="sales", summary="x"),
    }
    asked: list[list[str]] = []

    async def lookup_many(db, texts):
        asked.append(sorted(texts))
        return {t: cached[t] for t in texts if t in cached}

    monkeypatch.setattr(pe.jd_cache, "lookup_many", lookup_many)
    corpus = await _load(_db(jobs, {}), with_cache=True)
    assert asked == [["jd-a", "jd-b", "jd-c"]]
    assert (corpus.backlog.cache_hits, corpus.backlog.cache_rejects) == (2, 1)


# ------------------------------------------------------------- read-only


@pytest.mark.asyncio
async def test_projections_never_read_jd_raw_or_the_restore_payload():
    jobs, tombs = _corpus([GOOD], [BAD])
    tombs["n0"]["restore"] = {"location": "London, UK", "jd_raw": "big"}
    db = _db(jobs, tombs)
    corpus = await _load(db)
    selected = dict(db.selects)
    assert "jd_raw" not in selected["users/u1/jobs"]
    assert "restore" not in selected["users/u1/discarded_jobs"]
    tomb_row = next(r for r in corpus.rows if r.source == pe.TOMBSTONES)
    assert "restore" not in tomb_row.doc


@pytest.mark.asyncio
async def test_the_halves_partition_the_labelled_corpus():
    db = _db(*_corpus([GOOD] * 20, [BAD] * 20))
    every = {r.job_id for r in (await _load(db)).rows}
    even = {r.job_id for r in (await _load(db, half="even")).rows}
    odd = {r.job_id for r in (await _load(db, half="odd")).rows}
    assert even and odd
    assert even.isdisjoint(odd)
    assert even | odd == every


@pytest.mark.asyncio
async def test_the_cli_never_writes(monkeypatch, capsys):
    """A smoke check over the handles this fake models — document and
    collection writes, batches, transactions. A client the fake does not model
    would slip past; the read-only property rests on reading the module."""

    def _boom(*_a, **_k):
        raise AssertionError("prerank_eval must never write")

    for cls in (_Query, _QueryColl, _QueryDoc):
        for name in ("set", "update", "delete", "create", "add"):
            monkeypatch.setattr(cls, name, _boom, raising=False)
    for name in ("batch", "transaction", "bulk_writer"):
        monkeypatch.setattr(FakeQueryDB, name, _boom, raising=False)

    async def lookup_many(db, texts):
        return {}

    monkeypatch.setattr(pe.jd_cache, "lookup_many", lookup_many)
    jobs, tombs = _corpus([GOOD] * 3, [BAD] * 3)
    jobs["pending"] = {"title": GOOD, "user_decision": "pending", "jd_raw": "jd"}
    data = _db(jobs, tombs).data
    data["users"]["u2"] = USER
    data["users/u2/jobs"], data["users/u2/discarded_jobs"] = _corpus(
        [GOOD] * 3, [BAD] * 3, prefix="u2-"
    )
    corpora = await pe.run(
        FakeQueryDB(data),
        ["u1", "u2", "nobody"],
        half=None,
        enforce_geo=False,
        with_cache=True,
    )
    assert [c.user_id for c in corpora] == ["u1", "u2"]
    out = capsys.readouterr().out
    assert out.startswith(f"prerank v{pr.PRERANK_VERSION} · title map: ")
    assert "users/nobody: no such user, skipped" in out
    assert "pooled, within-user pairs only" in out


# --------------------------------------------------------- prune cutoffs


def _pruned(title: str, **extra) -> dict:
    return {"title": title, "company": "acme", "pruned": {"by": "prerank"}, **extra}


@pytest.mark.asyncio
async def test_pruned_tombstones_are_never_labelled_negatives():
    """A pruned tombstone was never scored. One carrying a ``score`` anyway
    must still stay out, or a prune would read as Pro agreeing with it."""
    jobs, tombs = _corpus([GOOD] * 3, [BAD] * 3)
    tombs["x-pruned"] = _pruned(BAD)
    tombs["x-pruned-scored"] = _pruned(GOOD, score=0)
    db = _db(jobs, tombs)
    corpus = await _load(db)
    ids = {r.job_id for r in corpus.rows}
    assert "x-pruned" not in ids and "x-pruned-scored" not in ids
    assert len(corpus.rows) == 6
    assert "pruned" in dict(db.selects)["users/u1/discarded_jobs"]


@pytest.mark.asyncio
async def test_cutoff_table_masks_the_corpus_and_not_the_backlog(capsys):
    """Ties at the cutoff count as pruned. A rejecting parse moves a backlog
    job below 0 but cannot move a labelled row, whose prerank is masked."""
    sales = {"role_family": "sales", "summary": "x"}
    other = {"company": "other"}
    jobs = {
        "p-good": _job(GOOD, 80, **other),
        "p-leaky": _job(GOOD, 80, jd_parsed=sales, **other),
        "p-bad": _job(BAD, 70, **other),
        "b-bad": {"title": BAD, "user_decision": "pending", **other},
        "b-worse": {"title": "Office Manager", "user_decision": "pending", **other},
        "b-good": {"title": GOOD, "user_decision": "pending", **other},
        "b-leaky": {
            "title": GOOD,
            "user_decision": "pending",
            "jd_parsed": sales,
            **other,
        },
    }
    tombs = {
        "n-bad1": {**_tomb(BAD), **other},
        "n-bad2": {**_tomb(BAD), **other},
        "n-worse": {**_tomb("Office Manager"), **other},
        "n-good": {**_tomb(GOOD, 10), **other},
        "x-pruned": _pruned(BAD, **other),
    }
    corpus = await _load(_db(jobs, tombs))
    by_id = {r.job_id: r for r in corpus.rows}
    assert by_id["p-leaky"].gate.score == 90 and by_id["p-bad"].gate.score == -40
    backlog = dict(zip(corpus.backlog.job_ids, corpus.backlog.scores, strict=True))
    assert backlog == {"b-bad": -40, "b-worse": -60, "b-good": 90, "b-leaky": -10}

    table = pe.cutoff_table(corpus.rows, corpus.backlog.scores, (0, -10, -40, -41))
    got = [(t.cutoff, t.positives, t.negatives, t.lost, t.backlog) for t in table]
    assert got == [
        (-41, 0, 1, 0.0, 1),
        (-40, 1, 3, pytest.approx(1 / 3), 2),
        (-10, 1, 3, pytest.approx(1 / 3), 3),
        (0, 1, 3, pytest.approx(1 / 3), 3),
    ]
    assert table[-1].backlog_share == pytest.approx(0.75)

    pe.report(corpus, half=None, cutoffs=(-40,))
    out = capsys.readouterr().out
    assert "7. Prune cutoffs (prune = prerank <= cutoff)" in out
    assert re.search(r"-40\s+1\s+3\s+33\.3%\s+2\s+50\.0%", out)


def test_cutoff_table_is_undefined_without_positives_or_backlog():
    rows = [pe.Row("n", pe.TOMBSTONES, {}, 0, pr.Prerank(-50.0), pr.Prerank(-50.0))]
    (row,) = pe.cutoff_table(rows, [], (-40,))
    assert (row.positives, row.negatives, row.backlog) == (0, 1, 0)
    assert isinstance(row.lost, pe.Undefined)
    assert isinstance(row.backlog_share, pe.Undefined)
