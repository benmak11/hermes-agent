# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The free prerank: each term's rule, masking, and the ranking sort key.

What matters most is what it must never do: raise on a malformed field, let
the freeform location line move the score or reject a job, or report a masked
term.
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


def _seniority(title: str, *targets: str) -> float:
    prefs = PREFS.model_copy(update={"target_seniorities": list(targets)})
    return _f({"title": title}, prefs)["seniority"]


def test_this_is_version_two():
    assert pr.PRERANK_VERSION == 2
    assert pr.prerank({}, PREFS, "US").version == 2


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


@pytest.mark.parametrize(
    "title",
    [
        "Technical Program Manager, Platform",
        "Program Manager",
        "Product Manager",
        "Project Manager",
    ],
)
def test_a_program_product_or_project_manager_is_not_a_level(title):
    assert "manager" not in pr.title_levels(title)


def test_other_level_words_beside_a_role_manager_still_count():
    assert pr.title_levels("Senior Product Manager") == {"senior"}
    assert pr.title_levels("Engineering Manager") == {"manager"}
    assert pr.title_levels("Product Engineering Manager") == {"manager"}


@pytest.mark.parametrize(
    ("title", "targets", "expected"),
    [
        ("Principal Engineer", ("staff",), 0),  # one step up
        ("Principal Engineer", ("senior",), -20),  # two steps
        ("Associate Engineer", ("senior",), 0),  # associate -> junior or mid
        ("Junior Engineer", ("senior",), -20),
        ("Intern", ("junior",), 0),
        ("Director of Engineering", ("manager",), -20),  # two steps
        ("Director of Engineering", ("senior manager",), 0),
        ("VP Engineering", ("director",), 0),
        ("Engineering Manager", ("senior",), -20),  # another ladder
        ("Staff Engineer", ("chief wizard",), -20),  # unknown target
        ("Staff Engineer", ("chief wizard", "senior"), 0),
        ("Tech Lead", ("principal",), 0),  # lead -> senior or staff
    ],
)
def test_an_adjacent_level_on_the_same_ladder_is_neutral(title, targets, expected):
    assert _seniority(title, *targets) == expected


def test_seniority_targets_are_normalised_and_senior_manager_is_derived():
    prefs = PREFS.model_copy(update={"target_seniorities": ["Senior Manager"]})
    assert pr.title_levels("Sr Engineering Manager") >= {"senior-manager"}
    assert _f({"title": "Sr Engineering Manager"}, prefs)["seniority"] == 10
    assert pr.title_levels("Vice President, Engineering") == {"vp"}


# ----------------------------------------------------------------- location


@pytest.mark.parametrize(
    ("location", "signal"),
    [
        ("London, UK", "foreign"),
        ("Toronto / Canada", "foreign"),
        ("Remote - United Kingdom", "none"),  # "remote" softens it to nothing
        ("New York, NY, United States", "home"),
        ("U.S.-based or Canada", "home"),  # residence present wins
        ("Berlin", "none"),  # no recognised country
        ("", "none"),
    ],
)
def test_location_is_observed_but_carries_no_weight(location, signal):
    job = {"title": "Staff Software Engineer", "location": location}
    result = pr.prerank(job, PREFS, "US")
    assert result.signals == {"location": signal}
    assert result.features["location"] == 0
    assert result.score == pr.prerank({"title": job["title"]}, PREFS, "US").score


def test_location_is_never_a_rejection_even_when_reweighted(monkeypatch):
    """Re-weighted, the term only shifts the score by its constant: a strong
    title still ranks above zero and the parse term is untouched."""
    monkeypatch.setattr(pr, "W_LOCATION_FOREIGN", -30.0)
    result = pr.prerank(
        {"title": "Staff Software Engineer", "location": "London, UK"}, PREFS, "US"
    )
    assert result.features["location"] == -30
    assert result.features["parsed"] == 0
    assert result.score > 0


def test_location_signal_survives_the_mask():
    job = {"location": "London, UK"}
    masked = pr.prerank(job, PREFS, "US", mask=frozenset({"location"}))
    assert "location" not in masked.features
    assert masked.signals == {"location": "foreign"}


@pytest.mark.parametrize("residence", [None, ""])
def test_location_abstains_without_a_residence(residence):
    result = pr.prerank({"location": "London, UK"}, PREFS, residence)
    assert result.features["location"] == 0
    assert result.signals == {"location": "none"}


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
    assert result.signals == {"location": "none"}


def test_a_masked_term_is_absent_and_contributes_nothing():
    job = {"title": "Staff Software Engineer", "location": "London, UK"}
    full = pr.prerank(job, PREFS, "US")
    masked = pr.prerank(job, PREFS, "US", mask=frozenset({"title_exact", "location"}))
    assert "title_exact" not in masked.features
    assert "location" not in masked.features
    assert masked.score == pytest.approx(full.score - 40)
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
