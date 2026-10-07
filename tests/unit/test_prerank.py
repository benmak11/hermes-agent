# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The free prerank: each term's rule, masking, and the ranking sort key.

What matters most is what it must never do: raise on a malformed field, treat
the freeform location line as a rejection, or report a masked term.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import tools.matching.prerank as pr
from models.profile import JobPreferences
from tools.companies import CompanyEntry

PREFS = JobPreferences(
    target_role_families=["engineering"],
    target_titles=["Staff Software Engineer"],
    target_seniorities=["staff", "senior"],
)


# Bound before the autouse fixture below replaces it.
_CURATED_SLUGS = pr.curated_slugs


def _parse(role_family: str = "engineering", **extra) -> dict:
    return {"role_family": role_family, "summary": "A role.", **extra}


@pytest.fixture(autouse=True)
def curated(monkeypatch):
    monkeypatch.setattr(pr, "curated_slugs", lambda: frozenset({"acme"}))


def _f(job: dict, prefs=PREFS, residence: str | None = "US", **kw) -> dict:
    return pr.prerank(job, prefs, residence, **kw).features


# -------------------------------------------------------------------- title


def test_title_exact_is_the_prefilter_substring_rule():
    assert _f({"title": "Staff Software Engineer, Payments"})["title_exact"] == 40
    assert _f({"title": "Software Engineer"})["title_exact"] == 0


def test_a_blank_target_title_matches_nothing():
    prefs = PREFS.model_copy(update={"target_titles": ["  "]})
    assert _f({"title": "Anything"}, prefs)["title_exact"] == 0


def test_title_overlap_is_the_best_share_of_a_target_core():
    # core("Staff Software Engineer") = {software, engineer}; "staff" is a level.
    assert _f({"title": "Backend Engineer"})["title_overlap"] == pytest.approx(12.5)
    assert _f({"title": "Software Engineer"})["title_overlap"] == pytest.approx(25)
    assert _f({"title": "Account Executive"})["title_overlap"] == 0


def test_family_rewards_in_target_and_penalises_a_confident_miss():
    assert _f({"title": "Backend Engineer"})["family"] == 15
    assert _f({"title": "Account Executive"})["family"] == -40
    assert _f({"title": "Chief Vibes Officer"})["family"] == 0


# ---------------------------------------------------------------- seniority


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Staff Engineer", 10),
        ("Sr. Engineer", 10),  # sr -> senior
        ("Tech Lead", 10),  # lead -> senior or staff
        ("Junior Engineer", -20),
        ("Engineering Intern", -20),
        ("Director of Engineering", -20),
        ("Engineer", 0),
    ],
)
def test_seniority_maps_title_words_onto_the_target_vocabulary(title, expected):
    assert _f({"title": title})["seniority"] == expected


def test_seniority_targets_are_normalised_and_senior_manager_is_derived():
    prefs = PREFS.model_copy(update={"target_seniorities": ["Senior Manager"]})
    assert pr.title_levels("Sr Engineering Manager") >= {"senior-manager"}
    assert _f({"title": "Sr Engineering Manager"}, prefs)["seniority"] == 10
    assert pr.title_levels("Vice President, Engineering") == {"vp"}


# ----------------------------------------------------------------- location


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("London, UK", -30),
        ("Toronto / Canada", -30),
        ("Remote - United Kingdom", 0),  # "remote" softens it to nothing
        ("New York, NY, United States", 5),
        ("U.S.-based or Canada", 5),  # residence present wins
        ("Berlin", 0),  # no recognised country
        ("", 0),
    ],
)
def test_location_is_a_soft_country_signal(location, expected):
    assert _f({"location": location})["location"] == expected


def test_location_is_never_a_rejection():
    """The worst it can do is a fixed -30: a strong title still ranks above
    zero, and the parse-reject weight is never reached through it."""
    result = pr.prerank(
        {"title": "Staff Software Engineer", "location": "London, UK"}, PREFS, "US"
    )
    assert result.features["location"] == pr.W_LOCATION_FOREIGN > pr.W_PARSED_REJECT
    assert result.features["parsed"] == 0
    assert result.score > 0


def test_location_abstains_without_a_residence():
    assert _f({"location": "London, UK"}, residence=None)["location"] == 0


# ------------------------------------------------------------------ curated


def test_curated_matches_the_company_slug_ignoring_case():
    assert _f({"company": "ACME"})["curated"] == 5
    assert _f({"company": "Other"})["curated"] == 0


def test_curated_slugs_skip_career_site_search_queries(monkeypatch):
    known = {
        "greenhouse": [CompanyEntry(slug="Acme")],
        "google_jobs": [CompanyEntry(slug="software engineer")],
    }
    monkeypatch.setattr(pr.companies, "load_known", lambda: known)
    assert _CURATED_SLUGS.__wrapped__() == frozenset({"acme"})


# ------------------------------------------------------------------- parsed


def test_an_in_family_parse_replaces_the_title_family():
    f = _f({"title": "Account Executive", "jd_parsed": _parse("engineering")})
    assert (f["parsed"], f["family"]) == (15, 0)


def test_a_prefilter_reject_is_minus_one_hundred_and_keeps_the_family():
    f = _f({"title": "Account Executive", "jd_parsed": _parse("sales")})
    assert (f["parsed"], f["family"]) == (-100, -40)


def test_parsed_follows_the_geo_enforce_flag(monkeypatch):
    monkeypatch.setenv("GEO_GATE_HOLDOUT", "0")
    job = {"id": "job-1", "jd_parsed": _parse(job_country="Germany")}
    assert _f(job)["parsed"] == 15
    assert _f(job, enforce_geo=True)["parsed"] == -100


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not a mapping",
        {"role_family": "engineering"},  # no summary: does not validate
        {"role_family": "astrology", "summary": "x"},
    ],
)
def test_an_absent_or_malformed_parse_scores_zero(raw):
    f = _f({"title": "Backend Engineer", "jd_parsed": raw})
    assert f["parsed"] == 0
    assert f["family"] == 15


# ---------------------------------------------------------- robustness, mask


def test_no_preferences_zeroes_every_profile_term():
    result = pr.prerank(
        {"title": "Staff Software Engineer", "company": "acme", "jd_parsed": _parse()},
        None,
        "US",
    )
    assert result.features == {
        "title_exact": 0,
        "title_overlap": 0,
        "family": 0,
        "seniority": 0,
        "location": 0,
        "curated": 5,
        "parsed": 0,
    }
    assert result.score == 5
    assert result.version == pr.PRERANK_VERSION


def test_malformed_fields_score_zero_and_never_raise():
    result = pr.prerank(
        {
            "title": None,
            "company": 123,
            "location": ["London"],
            "jd_parsed": [1],
            "id": 7,
        },
        PREFS,
        "US",
    )
    assert result.score == 0
    assert set(result.features) == set(pr.FEATURES)


def test_a_masked_term_is_absent_and_contributes_nothing():
    job = {"title": "Staff Software Engineer", "location": "London, UK"}
    full = pr.prerank(job, PREFS, "US")
    masked = pr.prerank(job, PREFS, "US", mask=frozenset({"title_exact", "location"}))
    assert "title_exact" not in masked.features
    assert "location" not in masked.features
    assert masked.score == pytest.approx(full.score - 40 + 30)
    assert masked.score == pytest.approx(sum(masked.features.values()))


def test_masking_parsed_leaves_the_title_family_alone():
    job = {"title": "Account Executive", "jd_parsed": _parse("engineering")}
    f = _f(job, mask=frozenset({"parsed"}))
    assert "parsed" not in f
    assert f["family"] == -40


# ------------------------------------------------------------------ helpers


def test_residence_country_resolves_like_the_geo_gate():
    assert pr.residence_country({"residence": {"country": "United States"}}) == "US"
    assert pr.residence_country({"location": "Austin, TX, USA"}) is None
    assert pr.residence_country({"residence": "US"}) is None
    assert pr.residence_country(None) is None


def test_preferences_from_tolerates_an_unusable_doc():
    assert pr.preferences_from({}) is None
    assert pr.preferences_from({"preferences": {"target_titles": []}}) is None
    assert pr.preferences_from({"preferences": PREFS.model_dump()}) == PREFS


# ----------------------------------------------------------------- sort key


def test_sort_key_orders_score_then_newest_then_id():
    new = datetime(2026, 9, 2, tzinfo=UTC)
    old = "2026-09-01T00:00:00Z"
    rows = [
        ("job-a", 10.0, old),
        ("job-b", 10.0, None),  # missing sorts as oldest
        ("job-c", 10.0, new),
        ("job-e", 50.0, None),
        ("job-d", 10.0, "garbage"),  # unreadable == missing; id breaks the tie
    ]
    ranked = sorted(rows, key=lambda r: pr.sort_key(r[1], r[2], r[0]))
    assert [r[0] for r in ranked] == ["job-e", "job-c", "job-a", "job-b", "job-d"]


def test_sort_key_reads_a_naive_datetime_as_utc():
    naive = datetime(2026, 9, 2)
    aware = datetime(2026, 9, 2, tzinfo=UTC)
    assert pr.sort_key(1.0, naive, "x") == pr.sort_key(1.0, aware, "x")
