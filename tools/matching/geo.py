# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deterministic geographic eligibility — the part of Rule 6 Python can *prove*.

Rule 6 of the scoring prompt (``pipeline.MATCH_CONTEXT_TEMPLATE``) is a hard
geo gate that Pro enforces *inside* the expensive call: an ineligible job gets
its ``overall_score`` capped at exactly 20, which is also
``score.DISCARD_AT_OR_BELOW``, so it is tombstoned immediately. On the main
user that is **69.7% of every Pro call ever made** — 2,534 of 3,634 scores
landed on exactly 20. Every one of those is a Pro call bought to be told the
candidate lives in the wrong country.

This module is that decision, in Python, for free. Nothing here is wired into
scoring yet (Phase 1C ships no behavior change); it exists so the decision can
be replayed against history (``cli.geo_replay``) and measured before it is
trusted with money.

**The gate is defined by what it can prove, not by what Rule 6 says.** A
literal port of Rule 6 does not work, and this is not a matter of tuning. The
candidate clauses were replayed over 1,127 historical scored+parsed job docs —
every one of which Pro judged *eligible*, so any INELIGIBLE verdict there is a
false positive, a job the user would never have seen:

===============================================================  =========
Clause, as literally written in Rule 6                            FP rate
===============================================================  =========
"``remote_scope`` unstated → ineligible unless ``us_remote_ok``"    3.19%
onsite/hybrid, same country, **state** mismatch                     2.48%
``remote_scope`` stated, no token for the residence country         0.44%
foreign ``job_country``, no scope, no ``us_remote_ok``              0.00%
===============================================================  =========

The bar is ≤0.5%. The first two clauses are **deleted, not softened** — see the
non-goals below. The third survives only because of the timezone guard in
:func:`_classify_scope`: all five of its measured failures were
timezone-shaped scopes ("EST and EU", "ET to CET time zones", "UTC-06:00 to
UTC+01:00"), which say something about *working hours* and nothing about
where a worker may live.

The reason a literal port fails is that the parse is thinner than the JD. 40.1%
of engineering parses leave ``job_country`` null while the freeform
``Job.location`` states it plainly, and Pro reads the location line and the JD
body and overrides the nulls. The worked example: a Vanta role listed
``"Remote U.S."`` with ``job_country`` null and ``us_remote_ok`` false — Rule 6
read literally calls that ineligible; Pro scored it **86, strong_apply**. So
every rule here is written to abstain unless the *parsed fields alone* settle
it, and ``abstain`` means "let Pro decide", which is exactly the status quo.

That asymmetry runs through the whole module: a verdict that is wrongly
``eligible`` or wrongly ``abstain`` costs coverage — a Pro call we could have
skipped but didn't. A verdict that is wrongly ``ineligible`` costs the user a
job they will never see. So the scope classifier is generous about *including*
and stingy about *excluding*, and unrecognized input always lands on abstain.

**Non-goals — deliberate omissions, not gaps.** Do not "complete" these:

- **No state or city comparison.** Rule 6 asks for it; it costs 2.48% FP,
  five times the bar. Postings say "New York" for a role in Jersey City, and
  ``job_state`` is null far more often than it is wrong.
- **No work-style (``remote_policy``) clause.** Rule 6's "honor the accepted
  work styles" line is a *preference* filter, not geography, and it belongs
  wherever preferences are enforced. Note also that the field it would read is
  ``profile.preferences.remote_policy`` (``models/profile.py``), whose default
  is ``["remote"]``; the prompt merely *labels* it "Accepted work styles". A
  gate that skipped every onsite role for every default profile would be an
  enormous silent behavior change hiding inside a cost optimization.
- **No parsing of ``Job.location`` or ``profile.location``.** Both are
  freeform, and the freeform strings are precisely where Pro's advantage lives
  — the Vanta case above. ``_residence_str`` (``pipeline.py``) does fall back
  to ``profile.location`` for the *prompt*; it must not leak in here, because
  "Austin, TX" and "Remote" are the same type to this module.

``GATE_VERSION`` is bumped whenever a rule changes, so a recorded decision can
be traced to the logic that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from models.job import ParsedJD
from models.profile import MasterProfile

#: Bump on any change to a rule, an alias, or the scope classifier. Recorded
#: alongside decisions so a replay can tell which logic produced them.
GATE_VERSION = 1

#: ``ineligible`` — provably out of reach, safe to skip the Pro call.
#: ``eligible`` — provably reachable (only the US-remote exception gets here).
#: ``abstain`` — not settled by the parsed fields; Pro decides, as today.
Verdict = Literal["ineligible", "eligible", "abstain"]


@dataclass(frozen=True)
class GeoDecision:
    """One gate outcome, plus enough to explain it in a log line or a replay.

    ``rule`` is which clause fired, not free text — a replay groups by it, so
    a regression shows up as a shifting distribution rather than a shifting
    total. The two normalized countries are carried because "why did this
    abstain?" is almost always answered by one of them being ``None``.
    """

    verdict: Verdict
    rule: str
    residence_country: str | None
    job_country: str | None


# --------------------------------------------------------------- normalization

# Strings that appear where a country name should be. ``"null"`` — the literal
# four characters, not a null — appears 18 times in the live corpus, which is
# the model writing the JSON word out. The rest are the parser answering a
# different question than the one asked.
#
# ``_NULLISH`` alone decides whether ``remote_scope`` was *stated at all*: a
# scope of "Remote" or "Various" is not a null, it is a scope we cannot read,
# and those must reach :func:`_classify_scope` and abstain there rather than
# be treated as absent (an absent scope lets the country-mismatch rule fire).
_NULLISH = frozenset(
    {
        "",
        "-",
        "--",
        "n a",
        "na",
        "nil",
        "none",
        "not specified",
        "null",
        "tbd",
        "unknown",
        "unspecified",
    }
)

# Everything above, plus the non-answers seen specifically in ``job_country``.
# Listed explicitly even though an unrecognized string already normalizes to
# ``None``, so that a later reader extending ``_COUNTRY_ALIASES`` can see these
# were considered and rejected rather than merely missed.
_JUNK_COUNTRY = _NULLISH | frozenset(
    {
        "anywhere",
        "global",
        "multiple",
        "multiple locations",
        "remote",
        "various",
        "worldwide",
    }
)

# Every country observed in the corpus (24 of them), plus the ones that appear
# *inside* observed ``remote_scope`` strings (Poland, Czech Republic, Ukraine,
# Colombia, Dominican Republic), plus common remote-hiring countries whose
# names are unambiguous. Deliberately absent: country names that collide with
# a US state — "Georgia" is the one that matters — since a US posting saying
# Georgia must never normalize to a foreign country.
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    # The alias list bridges a real disagreement in live data: one profile
    # stores country "US", two store "United States", and ``job_country``
    # carries "US", "USA" and "United States" in the same collection.
    "US": (
        "us",
        "u s",
        "usa",
        "u s a",
        "united states",
        "united states of america",
        "america",
    ),
    "CA": ("canada",),
    "MX": ("mexico",),
    "BR": ("brazil",),
    "AR": ("argentina",),
    "CO": ("colombia",),
    "DO": ("dominican republic",),
    "GB": (
        "uk",
        "u k",
        "united kingdom",
        "great britain",
        "britain",
        "england",
        "scotland",
        "wales",
    ),
    "IE": ("ireland",),
    "DE": ("germany", "deutschland"),
    "NL": ("netherlands", "holland"),
    "FR": ("france",),
    "ES": ("spain",),
    "PT": ("portugal",),
    "IT": ("italy",),
    "CH": ("switzerland",),
    "AT": ("austria",),
    "BE": ("belgium",),
    "PL": ("poland",),
    "CZ": ("czech republic", "czechia"),
    "RO": ("romania",),
    "UA": ("ukraine",),
    "SE": ("sweden",),
    "NO": ("norway",),
    "DK": ("denmark",),
    "FI": ("finland",),
    "GR": ("greece",),
    "CY": ("cyprus",),
    "TR": ("turkey", "turkiye"),
    "IL": ("israel",),
    "AE": ("united arab emirates", "uae"),
    "SA": ("saudi arabia",),
    "ZA": ("south africa",),
    "IN": ("india",),
    "SG": ("singapore",),
    "JP": ("japan",),
    "PH": ("philippines",),
    "CN": ("china",),
    "AU": ("australia",),
    "NZ": ("new zealand",),
}

_ALIAS_TO_COUNTRY: dict[str, str] = {
    alias: code for code, aliases in _COUNTRY_ALIASES.items() for alias in aliases
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm_text(value: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed.

    Punctuation becoming a *separator* rather than being stripped is what
    makes "U.S." read as the two tokens ``u s`` (an alias below) instead of
    the single token ``us``, and what keeps "Canada/USA" from becoming one
    unmatchable word. Everything downstream then matches whole token runs,
    never substrings — which is the only reason "Cyprus" and "Australia" do
    not both look like the United States.
    """
    return _NON_ALNUM.sub(" ", value.casefold()).strip()


def normalize_country(value: str | None) -> str | None:
    """A country string to its code, or ``None`` when we cannot be sure.

    Deliberately an **exact** alias match on the normalized string, not a
    search within it: "Remote - USA" sitting in ``job_country`` is a parse
    that answered the wrong question, and reading a country out of it is how
    the country-mismatch rule would start firing on strings nobody validated.
    Anything unrecognized is ``None``, which means abstain — never a mismatch.
    """
    if value is None:
        return None
    text = _norm_text(value)
    if text in _JUNK_COUNTRY:
        return None
    return _ALIAS_TO_COUNTRY.get(text)


def _stated(value: str | None) -> str | None:
    """``remote_scope`` as stated, or ``None`` when the field is a null in
    disguise (see ``_NULLISH``)."""
    if value is None:
        return None
    return None if _norm_text(value) in _NULLISH else value


# ----------------------------------------------------------- scope classifier

# Region names, as the country codes above that they contain. Membership is
# what the classifier actually asks ("is the resident inside this?"), so a
# region only ever needs the members this module can also name.
_NORTH_AMERICA = frozenset({"US", "CA", "MX"})
_LATAM = frozenset({"MX", "BR", "AR", "CO", "DO"})
_AMERICAS = _NORTH_AMERICA | _LATAM
_EUROPE = frozenset(
    {
        "GB",
        "IE",
        "DE",
        "NL",
        "FR",
        "ES",
        "PT",
        "IT",
        "CH",
        "AT",
        "BE",
        "PL",
        "CZ",
        "RO",
        "UA",
        "SE",
        "NO",
        "DK",
        "FI",
        "GR",
        "CY",
    }
)
_MIDDLE_EAST = frozenset({"AE", "SA", "IL", "TR", "CY"})
_AFRICA = frozenset({"ZA"})
_APAC = frozenset({"IN", "SG", "JP", "PH", "CN", "AU", "NZ"})

_REGION_ALIASES: dict[str, frozenset[str]] = {
    "north america": _NORTH_AMERICA,
    "americas": _AMERICAS,
    "south america": _LATAM,
    "latin america": _LATAM,
    "latam": _LATAM,
    "europe": _EUROPE,
    "european union": _EUROPE,
    "eu": _EUROPE,
    "eea": _EUROPE,
    "emea": _EUROPE | _MIDDLE_EAST | _AFRICA,
    "middle east": _MIDDLE_EAST,
    "africa": _AFRICA,
    "asia": _APAC,
    "asia pacific": _APAC,
    "apac": _APAC,
    "anz": frozenset({"AU", "NZ"}),
}

# Phrases meaning "no geographic restriction". Matching one is an immediate
# *include* for every residence — the generous direction, which costs at worst
# an abstain.
_WORLDWIDE = (
    "worldwide",
    "world wide",
    "global",
    "globally",
    "anywhere",
    "international",
    "any country",
    "any location",
)

# **The single guard that removes every measured scope false positive.** A
# scope built out of timezone vocabulary is a statement about overlapping
# working hours, not about where a worker may legally live: "EST and EU" does
# not exclude a US resident, it describes a meeting window. All five FPs in the
# 0.44% row of the table above were this shape. Anything containing one of
# these is ``unknown`` by construction, whatever else it also says — including
# scopes we could otherwise have read, which is coverage knowingly given up.
#
# Bare "et"/"ct"/"mt"/"pt" are here as whole tokens only; "Portugal" is a word,
# not "pt". "time" and "hours" catch the long tail ("US West Coast working
# hours") that no abbreviation list would.
#
# The list stops at the American and European abbreviations on purpose. A scope
# that is *only* a timezone ("9-6 India Standard Time") names no place the
# classifier recognizes, so it already lands on ``unknown`` with no guard at
# all; the guard earns its keep only where timezone vocabulary shares a string
# with a country name, which in this corpus is always a US or EU one.
_TIMEZONE_TOKENS = (
    "utc",
    "gmt",
    "time",
    "timezone",
    "timezones",
    "hours",
    "overlap",
    "est",
    "edt",
    "cst",
    "cdt",
    "mst",
    "mdt",
    "pst",
    "pdt",
    "cet",
    "cest",
    "eet",
    "bst",
    "et",
    "ct",
    "mt",
    "pt",
)

# Every recognizable place phrase → the countries it covers. Sorted longest
# first so the classifier consumes "south america" before "america" and
# "south africa" before "africa"; without that ordering "South America" would
# read as covering the United States.
_PLACE_PHRASES: list[tuple[str, frozenset[str]]] = sorted(
    [(alias, frozenset({code})) for alias, code in _ALIAS_TO_COUNTRY.items()]
    + list(_REGION_ALIASES.items()),
    key=lambda item: -len(item[0].split()),
)

ScopeClass = Literal["includes", "excludes", "unknown"]


def _contains(padded: str, phrase: str) -> bool:
    """Whole-token-run containment: ``phrase`` appears as complete words."""
    return f" {phrase} " in padded


def _classify_scope(scope: str, residence_country: str) -> ScopeClass:
    """Three-valued: does this scope cover ``residence_country``?

    ``unknown`` is a first-class answer and the most important one. A scope we
    half-understand is worth nothing: the whole value of the ``excludes``
    branch is that it is *certain*, so the classifier only returns it after
    recognizing at least one real place and finding the resident in none of
    them. Anything else — a bare list of US state codes, a timezone window, a
    country nobody aliased — is ``unknown``, and the caller abstains.
    """
    padded = f" {_norm_text(scope)} "
    if any(_contains(padded, token) for token in _TIMEZONE_TOKENS):
        return "unknown"
    if any(_contains(padded, phrase) for phrase in _WORLDWIDE):
        return "includes"

    covered: set[str] = set()
    recognized = False
    for phrase, members in _PLACE_PHRASES:
        if _contains(padded, phrase):
            recognized = True
            covered |= members
            # Consume it, so a longer phrase already matched cannot be
            # re-matched by the shorter one nested inside it.
            padded = padded.replace(f" {phrase} ", " ")
    if not recognized:
        return "unknown"
    return "includes" if residence_country in covered else "excludes"


# ------------------------------------------------------------------- the gate


def evaluate(parsed: ParsedJD, profile: MasterProfile) -> GeoDecision:
    """Can this candidate hold this job from where they live? First rule wins.

    Pure: no I/O, no model, no clock. The order below is the design — each
    rule is allowed to fire only after the cheaper, safer exceptions above it
    have had their say, which is why ``us_remote_ok`` sits at position 2
    (73.4% of the kept corpus lands there) rather than at the bottom as an
    afterthought the way it reads in Rule 6.
    """
    residence_country = (
        normalize_country(profile.residence.country) if profile.residence else None
    )
    job_country = normalize_country(parsed.job_country)

    # 1. No usable residence — there is nothing to compare against. Note this
    #    does NOT fall back to profile.location the way the prompt's
    #    _residence_str does; see the module docstring.
    if residence_country is None:
        return GeoDecision("abstain", "no_residence", None, job_country)

    # 2. The dominant safety valve: an explicit "US remote welcome" settles it
    #    for a US resident no matter where the company or the office sits.
    if parsed.us_remote_ok and residence_country == "US":
        return GeoDecision("eligible", "us_remote_ok", residence_country, job_country)

    scope = _stated(parsed.remote_scope)
    if scope is not None:
        scope_class = _classify_scope(scope, residence_country)
        # 3. A scope we can read that names places, none of them here.
        if scope_class == "excludes":
            return GeoDecision(
                "ineligible", "scope_excludes_country", residence_country, job_country
            )
        if scope_class == "unknown":
            return GeoDecision(
                "abstain", "scope_unparsed", residence_country, job_country
            )
        # An "includes" scope falls through rather than returning eligible: it
        # proves the resident is not excluded, which is not the same as
        # proving the role is a match, and no cheaper decision depends on it.
    # 4. A foreign office and no scope at all to widen it. The only clause of
    #    Rule 6 that replayed at 0.00% FP. It stays behind the scope check
    #    because a stated scope is the JD *correcting* the office address.
    elif job_country is not None and job_country != residence_country:
        return GeoDecision(
            "ineligible", "country_mismatch", residence_country, job_country
        )

    # 5. Nothing provable, but the three ways of getting here are not the same
    #    thing and must not share a label — Phase 3 writes these strings to
    #    Firestore, where a catch-all becomes permanently ambiguous.
    if job_country == residence_country:
        return GeoDecision("abstain", "same_country", residence_country, job_country)
    if job_country is not None:
        # Only reachable when a stated scope classified as ``includes``: the
        # office is abroad, but the JD explicitly named the resident's country
        # as in scope, so the address is not a barrier. Distinct from
        # ``country_unknown`` — here we read a country and decided it does not
        # matter, rather than failing to read one at all.
        return GeoDecision(
            "abstain", "scope_covers_foreign_office", residence_country, job_country
        )
    # job_country absent, junk, or unaliased — we simply could not tell.
    return GeoDecision("abstain", "country_unknown", residence_country, job_country)
