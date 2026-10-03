# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Every score records what produced it — the right model, not a nearby one.

A score with no provenance is an unattributable number: nothing in Firestore
said which model ran or which prompt it ran, so there was no way to tell
whether a December score was comparable to an October one. Every later task
that compares scores depends on this record existing *and being true*.

**The load-bearing test in this file is the batch one.** Batch prediction
rejects the ``gemini-flash-latest`` alias, so the batch path parses with
``gemini-2.5-flash`` while the online path parses with the alias — genuinely
two different models. Stamping the online constant on a batch-scored job
would record a lie, and it is a lie of the worst kind: indistinguishable
later from the truth, in the one field whose entire job is to be trustworthy.
So the models are threaded in from the callers, and these tests drive the
real scorers rather than calling ``scored_with`` directly, because a unit test
of the record builder cannot catch a caller passing it the wrong id.

The second theme is **not claiming more than happened**. ``None`` means "this
leg did not run here", and it appears in three real situations: the free
pre-filter (no model at all), a parse that came from ``jd_cache`` or a
previous run (ran, but not here, and by what is unknown), and a job scored
before this field existed. All three must stay distinguishable from a
confident wrong answer.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from types import SimpleNamespace

import pytest

import cli.purge_discarded as purge_discarded
import tools.matching.batch as batch
import tools.matching.batch_runs as batch_runs
import tools.matching.score as score
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from models.profile import MasterProfile, Residence
from tools.matching import pipeline

_SCORED_WITH_KEYS = {
    "parse_model",
    "match_model",
    "parse_prompt_version",
    "match_prompt_version",
    "scored_at",
}


# --- fixtures ---------------------------------------------------------------


def _profile() -> MasterProfile:
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
    )


def _parsed(role_family: str = "engineering") -> ParsedJD:
    return ParsedJD(role_family=role_family, seniority="staff", summary="Build.")


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
        discovered_at=datetime.fromisoformat("2026-10-01T00:00:00+00:00"),
    )
    job.jd_parsed = parsed
    return job


def _match(score_value: float) -> JobMatch:
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
        red_flags_hit=[],
        reasoning="A fine match.",
        recommendation="apply",
    )


class _KeepingRef:
    """A job doc that survives scoring: ``persist_result`` updates in place."""

    def __init__(self):
        self.updates: list[dict] = []

    async def update(self, fields: dict) -> None:
        self.updates.append(fields)

    @property
    def written(self) -> dict:
        return self.updates[-1]


class _DiscardingRef:
    """A job doc that gets tombstoned: ``persist_result`` walks parent.parent."""

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


def _flash_line(text: str, parsed: ParsedJD) -> dict:
    """One Vertex batch output line for a parse request echoing ``text``."""
    return _line(text, parsed.model_dump_json())


def _line(request_text: str, response_text: str) -> dict:
    return {
        "request": {"contents": [{"role": "user", "parts": [{"text": request_text}]}]},
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": response_text}],
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


# --- the premise ------------------------------------------------------------


def test_the_two_flash_ids_are_actually_different():
    """Everything below is only worth testing because of this. If the two ever
    converge, the batch tests stop discriminating and would pass against a
    scorer that stamps the wrong constant — so this failing is the signal to
    rewrite those tests, not to delete this one."""
    assert batch.BATCH_FLASH_MODEL != pipeline.FLASH_MODEL


def test_the_prompt_versions_are_integers_next_to_their_prompts():
    assert isinstance(pipeline.PARSE_PROMPT_VERSION, int)
    assert isinstance(pipeline.MATCH_PROMPT_VERSION, int)


# --- the version cannot go stale --------------------------------------------
#
# A version constant that depends on someone remembering to bump it is worse
# than no version at all: it asserts that two scores are comparable, and the
# moment it goes stale that assertion is false and nothing says so. The
# documents are already written by then and cannot be corrected.
#
# So the prompt text is digest-pinned *per version*. Both failure directions
# are covered by one dict:
#
# - edit a prompt, leave the version alone → the digest under that version no
#   longer matches, red;
# - bump the version, forget the digest → no entry for the new version, red.
#
# ``hashlib``, not ``hash()``: the builtin is salted per process, so a pinned
# value would differ on every run. The digests are over the raw template text
# exactly as the module holds it — a whitespace-only edit counts, because the
# model sees whitespace.

_PARSE_PROMPT_DIGESTS = {
    1: "220d4517c5980e932c453d44102243de65b92654086ac825e43bcd533d740d66",
}

#: Over the two templates **concatenated**, in the order the model sees them
#: (context block, then job block) — the thing versioned is the assembled
#: prompt, so the guard has to be over the assembled text. Concatenation, not
#: a pair of digests, for the same reason the version is one number.
_MATCH_PROMPT_DIGESTS = {
    1: "28dbe915efe9e1953ae6222258afdc3491908f0cce8ed00d5fd868c9ca6c6e93",
}


def _digest(*parts: str) -> str:
    return hashlib.sha256("".join(parts).encode()).hexdigest()


@pytest.mark.parametrize(
    ("name", "version", "digests", "text"),
    [
        (
            "PARSE_PROMPT_VERSION",
            pipeline.PARSE_PROMPT_VERSION,
            _PARSE_PROMPT_DIGESTS,
            (pipeline.PARSE_JD_PROMPT,),
        ),
        (
            "MATCH_PROMPT_VERSION",
            pipeline.MATCH_PROMPT_VERSION,
            _MATCH_PROMPT_DIGESTS,
            (pipeline.MATCH_CONTEXT_TEMPLATE, pipeline.MATCH_JOB_TEMPLATE),
        ),
    ],
)
def test_a_prompt_cannot_change_without_its_version_changing(
    name, version, digests, text
):
    assert version in digests, (
        f"{name} was bumped to {version} but no digest is pinned for it. "
        f"Add {version}: {_digest(*text)!r} to this test."
    )
    assert digests[version] == _digest(*text), (
        f"The prompt behind {name} changed but {name} is still {version}. "
        f"Bump {name} in tools/matching/pipeline.py and pin the new digest "
        f"{_digest(*text)!r} here. Every score already written claims to be "
        f"comparable to every score written next; leaving the version alone "
        f"makes that claim false and nothing downstream can detect it."
    )


# --- the record on the documents --------------------------------------------


def test_a_scored_job_doc_carries_all_five_keys():
    ref = _KeepingRef()
    provenance = score.scored_with(
        parse_model=pipeline.FLASH_MODEL, match_model=pipeline.PRO_MODEL
    )
    asyncio.run(score.persist_result(ref, _job(), _match(85), provenance=provenance))

    assert set(ref.written["scored_with"]) == _SCORED_WITH_KEYS
    assert ref.written["scored_with"] == provenance


def test_a_tombstone_carries_it_too():
    """~71% of everything ever scored lands in ``discarded_jobs``. Provenance
    on the survivors only cannot date the negatives, which is the half #97 just
    made usable as training features."""
    ref = _DiscardingRef()
    provenance = score.scored_with(
        parse_model=pipeline.FLASH_MODEL, match_model=pipeline.PRO_MODEL
    )
    outcome = asyncio.run(
        score.persist_result(ref, _job(), _match(10), provenance=provenance)
    )

    assert outcome == "discarded"
    assert set(ref.written["scored_with"]) == _SCORED_WITH_KEYS
    assert ref.written["scored_with"] == provenance


def test_scored_at_is_a_tz_aware_instant():
    """A naive ``utcnow()`` string compares wrong against every other
    timestamp written beside it, and silently: it parses fine and sorts
    plausibly."""
    stamp = score.scored_with(parse_model="m", match_model="m")["scored_at"]

    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert stamp.endswith("+00:00")


def test_a_leg_that_did_not_run_gets_no_prompt_version():
    """A ``parse_prompt_version`` beside a ``parse_model: None`` would claim to
    know which prompt produced a parse this call never made."""
    rec = score.scored_with(parse_model=None, match_model=pipeline.PRO_MODEL)

    assert rec["parse_model"] is None and rec["parse_prompt_version"] is None
    assert rec["match_prompt_version"] == pipeline.MATCH_PROMPT_VERSION


# --- the right model, per path ----------------------------------------------


def _online_run(monkeypatch, ref, job, *, cache_hit: ParsedJD | None = None):
    profile = _profile()

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_parse(j):
        return _parsed()

    async def fake_match(j, prof, cached_content=None):
        return _match(85)

    async def fake_cache_lookup(db, text):
        return cache_hit

    async def fake_cache_store(db, text, parsed, *, model):
        pass

    async def no_cache(prof, *a, **kw):
        return None

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "parse_jd", fake_parse)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.jd_cache, "lookup", fake_cache_lookup)
    monkeypatch.setattr(score.jd_cache, "store", fake_cache_store)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)
    return asyncio.run(score.score_pending_jobs("u1"))


def test_an_online_scored_job_records_the_online_models(monkeypatch, unlimited_budget):
    ref, job = _KeepingRef(), _job()
    counts = _online_run(monkeypatch, ref, job)

    assert counts["scored"] == 1
    rec = ref.written["scored_with"]
    assert rec["parse_model"] == pipeline.FLASH_MODEL
    assert rec["match_model"] == pipeline.PRO_MODEL
    # The discriminator: this path did not run the batch Flash.
    assert rec["parse_model"] != batch.BATCH_FLASH_MODEL
    assert rec["parse_prompt_version"] == pipeline.PARSE_PROMPT_VERSION
    assert rec["match_prompt_version"] == pipeline.MATCH_PROMPT_VERSION


def test_a_parse_this_run_did_not_pay_for_is_not_attributed(
    monkeypatch, unlimited_budget
):
    """A ``jd_cache`` hit was produced by some earlier run — possibly the batch
    one, under the other Flash. Naming today's constant there is the same error
    as naming it on a batch job, just quieter."""
    ref, job = _KeepingRef(), _job()
    _online_run(monkeypatch, ref, job, cache_hit=_parsed())

    rec = ref.written["scored_with"]
    assert rec["parse_model"] is None
    assert rec["match_model"] == pipeline.PRO_MODEL  # Pro really did run


def test_a_batch_scored_job_records_the_batch_models(monkeypatch, unlimited_budget):
    """THE ONE. Both legs ran in batch, so both ids must be the batch ids.

    ``FLASH_MODEL`` here would be a wrong attribution that no later query, no
    replay and no audit could ever detect — which is precisely the failure this
    whole task exists to prevent."""
    ref, job = _KeepingRef(), _job()  # unparsed: forces the Flash batch leg
    profile = _profile()

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_run_batch(*, model, lines, **kw):
        if model == batch.BATCH_FLASH_MODEL:
            return [_flash_line(job.jd_raw, _parsed())]
        assert model == batch.BATCH_PRO_MODEL
        return [_line("CTX\n\nBLOCK", _match(85).model_dump_json())]

    async def cache_miss_many(db, texts):
        return {}

    async def cache_store_many(db, parses, *, model):
        pass

    monkeypatch.setattr(batch, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(batch, "_run_batch", fake_run_batch)
    monkeypatch.setattr(batch, "build_match_context", lambda p: "CTX")
    monkeypatch.setattr(batch, "build_match_job_block", lambda j: "BLOCK")
    monkeypatch.setattr(batch, "batch_bucket_name", lambda: "test-bucket")
    monkeypatch.setattr(batch.jd_cache, "lookup_many", cache_miss_many)
    monkeypatch.setattr(batch.jd_cache, "store_many", cache_store_many)
    monkeypatch.setattr(batch.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(batch.batch_score_pending_jobs("u1"))

    assert counts["scored"] == 1
    rec = ref.written["scored_with"]
    assert rec["parse_model"] == batch.BATCH_FLASH_MODEL == "gemini-2.5-flash"
    assert rec["match_model"] == batch.BATCH_PRO_MODEL
    # Spelled out rather than left implicit in the equality above: this is the
    # exact substitution the task is about.
    assert rec["parse_model"] != pipeline.FLASH_MODEL


def test_the_resumable_ingest_records_the_batch_pro_model(monkeypatch):
    """``batch_runs`` finishes a batch in a *different process* from the one
    that submitted it. It still knows which model produced the output it is
    ingesting; it does not know what parsed the jobs, and must not guess."""
    ref, job = _KeepingRef(), _job(parsed=_parsed())
    persisted: list[dict | None] = []

    async def fake_load(db, user_id):
        return _profile(), [(ref, job)]

    async def fake_download(path):
        return "CTX"

    async def fake_fetch(path):
        return [_line("CTX\n\nBLOCK", _match(85).model_dump_json())]

    async def fake_persist_result(
        r, j, m, *, profile=None, geo_gate=None, provenance=None
    ):
        persisted.append(provenance)
        return "scored"

    monkeypatch.setattr(batch_runs, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(batch_runs, "download_text", fake_download)
    monkeypatch.setattr(batch_runs, "fetch_batch_output", fake_fetch)
    monkeypatch.setattr(batch_runs, "persist_result", fake_persist_result)
    monkeypatch.setattr(batch_runs, "build_match_job_block", lambda j: "BLOCK")

    run_ref = SimpleNamespace(id="tag", update=_async_noop)
    asyncio.run(
        batch_runs._ingest_score(
            None, run_ref, {"user_id": "u1", "gcs_root": "gs://b/r", "counts": {}}
        )
    )

    assert len(persisted) == 1
    rec = persisted[0]
    assert rec is not None
    assert rec["match_model"] == batch.BATCH_PRO_MODEL
    assert rec["match_prompt_version"] == pipeline.MATCH_PROMPT_VERSION
    # Unknown, not assumed: the parse leg ran elsewhere.
    assert rec["parse_model"] is None and rec["parse_prompt_version"] is None


async def _async_noop(*a, **kw) -> None:
    return None


# --- the free pre-filter claims nothing -------------------------------------


def test_the_batch_prefilter_tombstone_claims_no_model_ran(monkeypatch):
    """``_persist_prefiltered`` writes OUT_OF_FAMILY / GEO_INELIGIBLE stones at
    score 0 off a free local rule, so no scoring model ran. The parse is not
    attributable from that shim either — it reaches it through reloaded
    documents, which carry no model — so the tombstone carries no
    ``scored_with`` at all rather than a record naming one. See the docstring
    there for the accepted information loss against ``batch.py``."""
    persisted: list[dict | None] = []

    async def fake_persist_result(
        r, j, m, *, profile=None, geo_gate=None, provenance=None
    ):
        persisted.append(provenance)
        return "discarded"

    async def no_submit(**kw):
        raise AssertionError("a Pro batch was submitted for a pre-filtered job")

    monkeypatch.setattr(batch_runs, "persist_result", fake_persist_result)
    monkeypatch.setattr(batch_runs, "submit_batch", no_submit)

    run_ref = SimpleNamespace(id="tag", update=_async_noop)
    stage = asyncio.run(
        batch_runs._submit_score_stage(
            None,
            run_ref,
            "tag",
            "u1",
            "gs://b/r",
            _profile(),
            [(_KeepingRef(), _job(parsed=_parsed("marketing")))],
            {"scored": 0, "discarded": 0, "failed": 0},
        )
    )

    assert stage == "done"
    assert persisted == [None]


def test_the_online_prefilter_skip_claims_no_match_model(monkeypatch, unlimited_budget):
    """Same rule on the online path, with one difference that is worth the
    asymmetry: that scorer may have just paid for the Flash parse itself, and
    the tombstone keeps that parse as its only features. So the parse leg is
    attributed and the match leg is explicitly ``None``."""
    ref, job = _DiscardingRef(), _job()
    profile = _profile()

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, job)]

    async def fake_parse(j):
        return _parsed("marketing")  # outside target_role_families

    async def exploding_match(*a, **kw):
        raise AssertionError("Pro was called on a pre-filtered job")

    async def cache_miss(db, text):
        return None

    async def cache_store(db, text, parsed, *, model):
        pass

    async def no_cache(prof, *a, **kw):
        return None

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score, "parse_jd", fake_parse)
    monkeypatch.setattr(score, "match_job", exploding_match)
    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.jd_cache, "lookup", cache_miss)
    monkeypatch.setattr(score.jd_cache, "store", cache_store)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    counts = asyncio.run(score.score_pending_jobs("u1"))

    assert counts["discarded"] == 1
    rec = ref.written["scored_with"]
    assert rec["match_model"] is None and rec["match_prompt_version"] is None
    assert rec["parse_model"] == pipeline.FLASH_MODEL


def test_a_tombstone_with_nothing_to_say_carries_no_key():
    """Absent, not a dict of nulls. A record whose only non-null field is
    ``scored_at`` dates a *write*, not a score, and would make "a free rule
    decided this" indistinguishable from "a model ran and we lost the id"."""
    stone = score.discard_tombstone(_job(), _match(0))

    assert "scored_with" not in stone


def test_the_purge_backfill_carries_the_provenance_it_is_holding():
    """``cli.purge_discarded`` demotes already-scored jobs when
    ``DISCARD_AT_OR_BELOW`` is raised. Those job docs carry a real
    ``scored_with``, and dropping it on the way to the tombstone is worse than
    never having had it: absence means "no scoring model ran", so a job Pro
    really did score would end up asserting the exact opposite of the truth.
    """
    provenance = score.scored_with(
        parse_model=pipeline.FLASH_MODEL, match_model=pipeline.PRO_MODEL
    )
    doc = {"scored_run_id": "run-that-paid", "scored_with": provenance}

    stone = purge_discarded.backfill_tombstone(_job(), _match(25), doc)

    assert stone["scored_with"] == provenance
    # Carried from the document, not stamped from this (free) purge run.
    assert stone["scored_run_id"] == "run-that-paid"


def test_the_purge_backfill_invents_nothing_for_a_legacy_doc():
    """The other half: a job scored before the field existed still produces a
    tombstone with no ``scored_with``, rather than one built from whatever the
    constants happen to say on the day the purge runs."""
    stone = purge_discarded.backfill_tombstone(_job(), _match(25), {})

    assert "scored_with" not in stone


# --- nothing else moved -----------------------------------------------------


#: Exactly what ``persist_result`` writes onto a surviving job doc, and what
#: ``discard_tombstone`` writes onto a stone, with no ``geo_gate`` /
#: ``restore`` / ``exploration`` in play. **Spelled out rather than diffed
#: against a control run**, because a diff of two runs of the same function
#: cancels any key the change added to *both* of them — an extra field would
#: slip through invisibly, which is the one thing a no-behaviour-change test
#: has to catch. ``/jobs/pending`` returns these documents verbatim, so this
#: is also the response shape.
_JOB_DOC_KEYS = {"match", "jd_parsed", "scored_at", "scored_run_id"}
_TOMBSTONE_KEYS = {
    "job_id",
    "company",
    "title",
    "url",
    "score",
    "recommendation",
    "reasoning",
    "discarded_at",
    "scored_run_id",
    "jd_parsed",
}


@pytest.mark.parametrize(
    ("score_value", "expected_keys", "outcome"),
    [(85.0, _JOB_DOC_KEYS, "scored"), (10.0, _TOMBSTONE_KEYS, "discarded")],
)
def test_the_scoring_outcome_is_otherwise_unchanged(
    score_value, expected_keys, outcome
):
    """This PR adds exactly one key, and only when there is one to add."""
    without = _KeepingRef() if score_value > 20 else _DiscardingRef()
    with_it = _KeepingRef() if score_value > 20 else _DiscardingRef()
    provenance = score.scored_with(
        parse_model=pipeline.FLASH_MODEL, match_model=pipeline.PRO_MODEL
    )

    a = asyncio.run(score.persist_result(without, _job(), _match(score_value)))
    b = asyncio.run(
        score.persist_result(
            with_it, _job(), _match(score_value), provenance=provenance
        )
    )

    assert a == b == outcome
    assert set(without.written) == expected_keys
    assert set(with_it.written) == expected_keys | {"scored_with"}
    # Values, not just key sets: timestamps are clocks, not content.
    volatile = {"scored_at", "discarded_at", "scored_with"}
    assert {k: v for k, v in without.written.items() if k not in volatile} == {
        k: v for k, v in with_it.written.items() if k not in volatile
    }
