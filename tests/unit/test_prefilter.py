# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The free family pre-filter, now that it exists exactly once.

``pipeline.family_prefilter`` is the extraction of a decision that was written
out three times — once in ``match_job`` and once in each batch path, which
never call ``match_job`` at all. The pre-Pro seam it creates is what Phase 1D's
enforcement work hangs the geo gate on, so what is pinned here is less the
family test itself (four lines) than the properties the seam has to keep:

- the sentinel is always a **copy**, never the shared ``OUT_OF_FAMILY``
  singleton, or the first out-of-family job in a run would rename every later
  one by mutating module state;
- an unparsed job is ``None`` and not a tombstone, because every caller settles
  the unparsed case before asking and a parse failure must stay retryable;
- all three call sites are looking at the *same* function object, which is the
  only thing stopping one of them from quietly drifting back to its own copy.
"""

from datetime import UTC, datetime

import pytest

import tools.matching.batch as batch
import tools.matching.batch_runs as batch_runs
import tools.matching.pipeline as pipeline
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


def test_in_family_returns_none():
    """None means "no verdict, go score it" — the expensive path stays open."""
    assert pipeline.family_prefilter(_job(), _profile("engineering")) is None


def test_out_of_family_returns_the_sentinel():
    match = pipeline.family_prefilter(_job("j7"), _profile("product"))
    assert match is not None
    assert match.job_id == "j7"
    assert match.overall_score == 0
    assert match.recommendation == "skip"
    assert match.model_dump(exclude={"job_id"}) == pipeline.OUT_OF_FAMILY.model_dump(
        exclude={"job_id"}
    )


def test_profile_families_are_matched_case_insensitively():
    """``target_role_families`` is user-entered; ``role_family`` is a Literal
    the parse prompt already constrains to lowercase. Only one side needs it."""
    assert pipeline.family_prefilter(_job(), _profile("Engineering")) is None
    assert pipeline.family_prefilter(_job(), _profile("ENGINEERING")) is None


def test_no_targets_skips_everything():
    """An empty target list is not a wildcard. Preserved from the original
    three implementations, where ``not in set()`` is always true."""
    assert pipeline.family_prefilter(_job(), _profile()) is not None


@pytest.mark.parametrize("families", [("product",), ()])
def test_unparsed_job_is_never_tombstoned(families):
    """A missing parse is a *failure*, not a rejection: it has to stay
    retryable by a later run, so it must not come back as a tombstone even
    when nothing about the profile could ever match it."""
    assert (
        pipeline.family_prefilter(_job(role_family=None), _profile(*families)) is None
    )


def test_sentinel_is_a_copy_not_the_singleton():
    """Mutating a returned sentinel must not rewrite the module constant that
    every subsequent call is built from."""
    first = pipeline.family_prefilter(_job("a"), _profile("product"))
    second = pipeline.family_prefilter(_job("b"), _profile("product"))
    assert first is not pipeline.OUT_OF_FAMILY
    assert first is not second
    assert (first.job_id, second.job_id) == ("a", "b")
    assert pipeline.OUT_OF_FAMILY.job_id == ""


def test_every_scorer_shares_one_seam():
    """The point of the extraction: three scorers, one function object. If a
    path grows its own copy again, the geo gate wired onto this seam would
    silently not apply there."""
    assert batch.family_prefilter is pipeline.family_prefilter
    assert batch_runs.family_prefilter is pipeline.family_prefilter
