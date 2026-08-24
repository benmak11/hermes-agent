# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The deterministic geo gate: what it must decide, and what it must refuse to.

``tools.matching.geo`` exists to skip Pro calls, so every test here is really
asking one of two questions. "Does it fire when it provably can?" — a missed
firing costs money. "Does it stay quiet otherwise?" — a wrong ``ineligible``
costs the user a job they will never see, and that is the failure this module
was designed around, at a measured bar of ≤0.5% false positives over 1,127
historical scores.

Three groups below carry most of the weight:

- the **invariance** test, which pins the two clauses Rule 6 asks for and this
  gate deliberately omits (state/city comparison at 2.48% FP, work-style at
  a scale nobody measured). It exists so that re-adding either one turns red
  instead of quietly shipping.
- the **timezone** cases, which are the single guard that took the stated-scope
  clause from 0.44% FP to zero. "EST and EU" is a meeting window, not an
  immigration rule.
- the **regression fixtures**, lifted field-for-field from real documents —
  above all the Vanta shape (``"Remote - USA"`` with a null ``job_country``)
  that a naive Rule 6 port calls ineligible and that Pro scored 86.

Zero I/O, zero LLM: ``evaluate`` is pure by construction.
"""

import pytest

from models.job import ParsedJD
from models.profile import MasterProfile, Residence
from tools.matching import geo


def _profile(country: str | None = "US", **residence_kw) -> MasterProfile:
    """A minimal profile whose only interesting field is where they live."""
    return MasterProfile(
        user_id="u1",
        full_name="Test Candidate",
        email="test@example.com",
        # Deliberately a *different* place than `residence`: nothing in the
        # gate may read this string, and if something starts to, these tests
        # should be the ones that notice.
        location="Somewhere, Elsewhere",
        residence=(
            Residence(country=country, **residence_kw) if country is not None else None
        ),
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
    return ParsedJD(summary="A job.", **kw)


def _verdict(parsed: ParsedJD, profile: MasterProfile | None = None) -> tuple[str, str]:
    d = geo.evaluate(parsed, profile or _profile())
    return d.verdict, d.rule


# ------------------------------------------------------------- normalization


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The live profiles genuinely disagree on format — one stores "US",
        # two store "United States" — and job_country carries all three.
        ("US", "US"),
        ("us", "US"),
        ("USA", "US"),
        ("U.S.", "US"),
        ("u.s.", "US"),
        ("United States", "US"),
        ("united states of america", "US"),
        ("America", "US"),
        ("Germany", "DE"),
        ("United Kingdom", "GB"),
        ("UAE", "AE"),
        # Junk sentinels: "null" is the literal word, 18 times in live data.
        ("null", None),
        ("None", None),
        ("Remote", None),
        ("Various", None),
        ("Multiple", None),
        ("", None),
        (None, None),
        # Never guessed at. An unrecognized string abstains; it must not be
        # allowed to look like a mismatch.
        ("Freedonia", None),
        ("Remote - USA", None),
    ],
)
def test_country_normalization(raw, expected):
    assert geo.normalize_country(raw) == expected


def test_country_names_that_collide_with_us_states_are_not_aliased():
    """A US posting saying "Georgia" must never read as a foreign country —
    that would turn a local role into a country_mismatch."""
    assert geo.normalize_country("Georgia") is None


def test_whole_tokens_only_never_substrings():
    """ "Cyprus" and "Australia" both contain "us"; a substring matcher would
    read either as the United States."""
    assert geo.normalize_country("Cyprus") == "CY"
    assert geo.normalize_country("Australia") == "AU"
    assert geo._classify_scope("Cyprus", "US") == "excludes"


# ------------------------------------------------------------ rule 1: no residence


def test_missing_residence_abstains():
    parsed = _parsed(job_country="Germany", remote_scope=None)
    assert _verdict(parsed, _profile(country=None)) == ("abstain", "no_residence")


def test_unrecognized_residence_country_abstains():
    parsed = _parsed(job_country="Germany")
    assert _verdict(parsed, _profile(country="Freedonia")) == (
        "abstain",
        "no_residence",
    )


def test_residence_is_never_read_from_the_freeform_location():
    """``pipeline._residence_str`` falls back to ``profile.location`` for the
    prompt. If that fallback ever leaks in here, this profile — whose
    ``location`` says "United States" while ``residence`` is unset — would
    start producing mismatches instead of abstaining."""
    profile = _profile(country=None)
    profile.location = "Austin, TX, United States"
    assert _verdict(_parsed(job_country="Germany"), profile) == (
        "abstain",
        "no_residence",
    )


# --------------------------------------------------- rule 2: the us_remote_ok valve


def test_us_remote_ok_beats_a_foreign_office():
    """The dominant safety valve — 73.4% of the kept corpus lands here. An
    explicit "US remote welcome" settles it wherever the company sits."""
    parsed = _parsed(job_country="Germany", us_remote_ok=True)
    assert _verdict(parsed) == ("eligible", "us_remote_ok")


def test_us_remote_ok_beats_an_excluding_scope():
    parsed = _parsed(job_country="Germany", remote_scope="Europe", us_remote_ok=True)
    assert _verdict(parsed) == ("eligible", "us_remote_ok")


def test_us_remote_ok_does_nothing_for_a_non_us_resident():
    parsed = _parsed(job_country="Germany", us_remote_ok=True)
    assert _verdict(parsed, _profile(country="Canada")) == (
        "ineligible",
        "country_mismatch",
    )


# ------------------------------------------------------- rule 3: the scope clause


@pytest.mark.parametrize(
    "scope",
    [
        "United States",
        "US-only",
        "U.S.",
        "USA",
        "US or Canada",
        "United States, Canada",
        "US, Canada, UK",
        "Canada/USA",
        "East Coast, USA",
        "North America",
        "North America and Europe",
        "Americas",
        "Worldwide",
        "Global",
        "Anywhere",
        "US-based",
    ],
)
def test_scopes_that_include_the_resident_never_reject(scope):
    assert _classify(scope) == "includes"
    assert _verdict(_parsed(remote_scope=scope))[0] != "ineligible"


@pytest.mark.parametrize(
    "scope", ["Europe", "EMEA", "LATAM", "Latin America", "Germany", "UK", "APAC"]
)
def test_scopes_that_name_only_other_places_reject(scope):
    assert _classify(scope) == "excludes"
    assert _verdict(_parsed(remote_scope=scope)) == (
        "ineligible",
        "scope_excludes_country",
    )


@pytest.mark.parametrize(
    "scope",
    [
        # Every one of these is a real value from the corpus, and every one of
        # them is a *time* constraint wearing a geography's clothes.
        "EST and EU",
        "ET to CET time zones",
        "UTC-06:00 to UTC+01:00",
        # Escaped, not typed: the live value really does use an en dash, and
        # a fixture that silently ASCII-fied it would stop being the fixture.
        "US Time Zones (EST\u2013PST)",
        "US West Coast working hours",
        "Eastern Time (ET) zone in the US and Canada",
        "UK, Europe, or ET (Eastern Time)",
    ],
)
def test_timezone_shaped_scopes_are_unknown_by_construction(scope):
    """The guard that took this clause from 0.44% FP to zero. A timezone scope
    says when you work, not where you may live, so it settles nothing."""
    assert _classify(scope) == "unknown"
    assert _verdict(_parsed(remote_scope=scope)) == ("abstain", "scope_unparsed")


@pytest.mark.parametrize(
    "scope",
    [
        "AZ, CA, CO, CT, FL, GA, ID, IL",  # a bare US state list
        "California, Hawaii, Oregon, Nevada, Washington",
        "Remote",
        "Various",
        "Freedonia and Ruritania",
    ],
)
def test_scopes_naming_no_recognizable_place_abstain(scope):
    """Recognizing nothing is not the same as recognizing an exclusion — the
    ``excludes`` branch is only worth having while it is certain."""
    assert _classify(scope) == "unknown"
    assert _verdict(_parsed(remote_scope=scope))[0] == "abstain"


def test_south_america_does_not_read_as_america():
    """Longest-phrase-first consumption. Without it, "South America" contains
    the token "america" and would wrongly cover a US resident."""
    assert _classify("South America") == "excludes"
    assert _classify("South Africa") == "excludes"


def test_a_nullish_scope_string_counts_as_no_scope_at_all():
    """The parser writes the word "null" into the field. That is an absent
    scope, which lets the country-mismatch rule below see the job."""
    assert _verdict(_parsed(job_country="Germany", remote_scope="null")) == (
        "ineligible",
        "country_mismatch",
    )


def _classify(scope: str) -> str:
    return geo._classify_scope(scope, "US")


# --------------------------------------------------- rule 4: the country mismatch


def test_foreign_country_with_no_scope_rejects():
    """The only clause of Rule 6 that replayed at 0.00% false positives."""
    assert _verdict(_parsed(job_country="Germany")) == (
        "ineligible",
        "country_mismatch",
    )


def test_a_stated_scope_outranks_the_office_country():
    """A remote role's office address is not where the work happens: the JD
    saying "United States" corrects a job_country of Germany, and the gate
    must not reject on the stale field.

    Labelled ``scope_covers_foreign_office`` rather than ``country_unknown``:
    the country was read perfectly well and then judged not to matter, which
    is a different fact from failing to read one. Phase 3 persists these
    strings, so the two cases have to stay separable after the fact.
    """
    parsed = _parsed(job_country="Germany", remote_scope="United States")
    assert _verdict(parsed) == ("abstain", "scope_covers_foreign_office")


def test_the_two_abstain_labels_are_not_interchangeable():
    """``country_unknown`` means the country was unreadable; the other means it
    was read and overridden. Same verdict, different diagnosis — and only the
    second one has a job_country worth reporting."""
    unreadable = geo.evaluate(
        _parsed(job_country=None, remote_scope="Worldwide"), _profile()
    )
    overridden = geo.evaluate(
        _parsed(job_country="Germany", remote_scope="Worldwide"), _profile()
    )

    assert unreadable.verdict == overridden.verdict == "abstain"
    assert unreadable.rule == "country_unknown"
    assert unreadable.job_country is None
    assert overridden.rule == "scope_covers_foreign_office"
    assert overridden.job_country == "DE"  # normalized, as the field promises


def test_an_unrecognized_country_abstains_rather_than_mismatching():
    assert _verdict(_parsed(job_country="Freedonia")) == (
        "abstain",
        "country_unknown",
    )


def test_a_null_country_abstains():
    assert _verdict(_parsed(job_country=None)) == ("abstain", "country_unknown")
    assert _verdict(_parsed(job_country="null")) == ("abstain", "country_unknown")


# ------------------------------------------------------------ rule 5: same country


@pytest.mark.parametrize("value", ["US", "USA", "United States", "u.s."])
def test_same_country_abstains_whatever_the_spelling(value):
    """Same country is not proof of eligibility — it is only proof that this
    gate has nothing to say. Pro still scores it."""
    assert _verdict(_parsed(job_country=value)) == ("abstain", "same_country")


# ------------------------------------------------------------------ invariance

# The guard against re-adding the clauses that were measured and rejected.


def test_state_city_and_work_style_never_change_the_verdict():
    """Rule 6 asks for a state match (2.48% FP) and a work-style check. Both
    are deliberate non-goals. Mutating every field they would read must leave
    every verdict byte-identical — if this fails, one of them came back."""
    profile = _profile(country="US", state="TX", city="Austin")
    profile.preferences.remote_policy = ["remote"]

    baselines = [
        _parsed(job_country="United States"),
        _parsed(job_country="Germany"),
        _parsed(job_country=None, remote_scope="Europe"),
        _parsed(job_country="United States", us_remote_ok=True),
        _parsed(job_country=None),
    ]
    for base in baselines:
        expected = geo.evaluate(base, profile)
        for state, city in [("NY", "New York"), (None, None), ("TX", "Dallas")]:
            for policy in (["remote"], ["onsite"], ["remote", "hybrid", "onsite"]):
                for job_policy in ("remote", "hybrid", "onsite", "unspecified"):
                    variant = base.model_copy(
                        update={
                            "job_state": state,
                            "job_city": city,
                            "remote_policy": job_policy,
                        }
                    )
                    profile.preferences.remote_policy = policy
                    assert geo.evaluate(variant, profile) == expected, (
                        f"{base!r} changed under state={state} city={city} "
                        f"policy={policy} job_policy={job_policy}"
                    )


def test_an_onsite_role_in_the_same_country_is_not_rejected_on_state():
    """The 2.48% clause, stated as its own case: a Washington DC resident and
    a New York onsite role. Rule 6 says ineligible; the measurement says that
    rule rejects one in forty jobs the user actually wanted."""
    parsed = _parsed(
        job_country="United States",
        job_state="NY",
        job_city="New York",
        remote_policy="onsite",
    )
    profile = _profile(country="US", state="DC", city="Washington")
    assert _verdict(parsed, profile) == ("abstain", "same_country")


# --------------------------------------------------- regression fixtures (real docs)


def test_the_vanta_case_abstains():
    """The document that disproves a literal Rule 6 port. Location line
    ``"Remote - USA"``, ``job_country`` null, ``us_remote_ok`` false — Rule 6
    read literally ("scope unstated → ineligible unless us_remote_ok") calls
    this ineligible. Pro scored it **86, strong_apply**, because Pro reads the
    freeform location the parse dropped.

    The gate never sees ``Job.location``, so its only honest answer is
    abstain, and this is the test that keeps it honest."""
    parsed = _parsed(
        role_family="engineering",
        seniority="senior",
        remote_policy="remote",
        job_country=None,
        job_state=None,
        job_city=None,
        remote_scope=None,
        us_remote_ok=False,
    )
    assert _verdict(parsed) == ("abstain", "country_unknown")


def test_an_unstated_scope_alone_never_rejects():
    """The 3.19% clause, generalized: no scope and no country is the single
    most common parse shape, and it is not evidence of anything."""
    for policy in ("remote", "hybrid", "onsite", "unspecified"):
        assert _verdict(_parsed(remote_policy=policy))[0] == "abstain"


def test_gate_version_is_pinned():
    """A recorded decision is only traceable if this moves when a rule does."""
    assert geo.GATE_VERSION == 1
