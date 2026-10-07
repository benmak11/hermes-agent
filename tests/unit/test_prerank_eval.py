# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The prerank eval: what it measures, and the ways it could flatter itself.

The one that matters most is the mask. ``location``, ``jd_parsed`` and
``discovered_at`` live on job docs and mostly not on tombstones, and job docs
are mostly Pro's survivors, so a term built on them separates the collections
rather than the jobs. The gating AUC must not move when those fields appear.
"""

from __future__ import annotations

import pytest
from firestore_fakes import FakeQueryDB, _Query, _QueryColl, _QueryDoc, _QuerySnap

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


@pytest.mark.asyncio
async def test_a_buried_positive_and_a_location_falsification_are_listed(capsys):
    jobs, tombs = _corpus([GOOD] * 30, [BAD] * 30)
    jobs["job-buried"] = _job(BAD, 90, location="London, UK")
    corpus = await _load(_db(jobs, tombs))

    assert [r.job_id for r in pe.false_buries(corpus.rows)] == ["job-buried"]
    assert [r.job_id for r in pe.location_falsifications(corpus.rows)] == ["job-buried"]
    g = pe.report(corpus, half=None)
    assert g.verdict == "FAIL"
    assert "1 location falsification(s)" in g.reasons
    out = capsys.readouterr().out
    assert "3. Location falsifications: 1" in out
    assert "4. False buries (positives below the masked median): 1" in out
    assert out.count("job-buried") == 2


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
    assert isinstance(stats["location"].lift, pe.Undefined)


# ---------------------------------------------------------------- backlog


@pytest.mark.asyncio
async def test_backlog_uses_the_scorers_pending_predicate(monkeypatch):
    monkeypatch.setattr(
        _QuerySnap, "reference", property(lambda s: s.id), raising=False
    )

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
    assert corpus.backlog.location_fires == 1


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
