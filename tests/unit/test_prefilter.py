# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The free pre-filter, now that it exists exactly once.

``pipeline.prefilter`` is the extraction of a decision that was written out
three times — once in ``match_job`` and once in each batch path, which never
call ``match_job`` at all. Phase 1D widened it to carry the geo gate as well,
so what is pinned here is less the family test itself (four lines) than the
properties the seam has to keep:

- the sentinels are always a **copy**, never a shared module singleton, or the
  first rejected job in a run would rename every later one by mutating module
  state;
- an unparsed job is ``None`` and not a tombstone, because every caller settles
  the unparsed case before asking and a parse failure must stay retryable;
- all three call sites are looking at the *same* function object, which is the
  only thing stopping one of them from quietly drifting back to its own copy;
- **with ``enforce=False`` the gate is not consulted at all**, which is what
  makes the geo work inert on merge.

The enforcement behavior itself lives in ``test_geo_enforce.py``.
"""

from datetime import UTC, datetime

import pytest

import tools.matching.batch as batch
import tools.matching.batch_runs as batch_runs
import tools.matching.pipeline as pipeline
import tools.matching.score as score
from models.job import Job, ParsedJD
from models.profile import MasterProfile


def _profile(*families: str) -> MasterProfile:
    return MasterProfile(
        user_id="u1",
        full_name="Test Candidate",
        email="test@example.com",
        location="Somewhere, Elsewhere",
        objective_template="{role} at {company}",
        experience=[],
        education=[],
        skills={},
        preferences={
            "target_role_families": list(families),
            "target_titles": ["Staff Software Engineer"],
            "target_seniorities": ["staff"],
        },
    )


def _job(job_id: str = "j1", *, role_family: str | None = "engineering") -> Job:
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
    if role_family is not None:
        job.jd_parsed = ParsedJD(
            role_family=role_family, seniority="staff", summary="Build."
        )
    return job


def _family(job: Job, profile: MasterProfile):
    """The family test as every caller sees it with the flag off."""
    return pipeline.prefilter(job, profile, enforce=False)


def test_in_family_returns_none():
    """None means "no verdict, go score it" — the expensive path stays open."""
    assert _family(_job(), _profile("engineering")) == (None, None)


def test_out_of_family_returns_the_sentinel():
    match, decision = _family(_job("j7"), _profile("product"))
    assert match is not None
    assert match.job_id == "j7"
    assert match.overall_score == 0
    assert match.recommendation == "skip"
    assert match.model_dump(exclude={"job_id"}) == pipeline.OUT_OF_FAMILY.model_dump(
        exclude={"job_id"}
    )
    # No gate was consulted, so there is nothing to record — and the absent
    # decision is exactly how a caller tells this apart from a geo skip.
    assert decision is None


def test_profile_families_are_matched_case_insensitively():
    """``target_role_families`` is user-entered; ``role_family`` is a Literal
    the parse prompt already constrains to lowercase. Only one side needs it."""
    assert _family(_job(), _profile("Engineering"))[0] is None
    assert _family(_job(), _profile("ENGINEERING"))[0] is None


def test_no_targets_skips_everything():
    """An empty target list is not a wildcard. Preserved from the original
    three implementations, where ``not in set()`` is always true."""
    assert _family(_job(), _profile())[0] is not None


@pytest.mark.parametrize("families", [("product",), ()])
def test_unparsed_job_is_never_tombstoned(families):
    """A missing parse is a *failure*, not a rejection: it has to stay
    retryable by a later run, so it must not come back as a tombstone even
    when nothing about the profile could ever match it."""
    assert _family(_job(role_family=None), _profile(*families)) == (None, None)


def test_sentinel_is_a_copy_not_the_singleton():
    """Mutating a returned sentinel must not rewrite the module constant that
    every subsequent call is built from."""
    first, _ = _family(_job("a"), _profile("product"))
    second, _ = _family(_job("b"), _profile("product"))
    assert first is not pipeline.OUT_OF_FAMILY
    assert first is not second
    assert (first.job_id, second.job_id) == ("a", "b")
    assert pipeline.OUT_OF_FAMILY.job_id == ""


def test_every_scorer_shares_one_seam():
    """The point of the extraction: three scorers, one function object. If a
    path grows its own copy again, the geo gate wired onto this seam would
    silently not apply there."""
    assert score.prefilter is pipeline.prefilter
    assert batch.prefilter is pipeline.prefilter
    assert batch_runs.prefilter is pipeline.prefilter
