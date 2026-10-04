# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Deterministic geographic eligibility — the part of scoring Rule 6 that can
be decided in Python, for free, instead of inside a paid Pro call.

Rule 6 of ``pipeline.MATCH_CONTEXT_TEMPLATE`` caps an ineligible job at exactly
20, which is ``score.DISCARD_AT_OR_BELOW``, so it is tombstoned on arrival; on
the main user that was 69.7% of every Pro call ever made (2,534 of 3,634 scores
landed on exactly 20).

Measured, by replaying candidate clauses over 1,127 historical jobs Pro judged
eligible (so any ineligible verdict there is a false positive): the bar is
≤0.5% FP, and only the foreign-country clause (0.00%) and a scope clause
guarded against timezone vocabulary (0.44%, all five failures timezone-shaped)
clear it. Deliberately absent, not missing: state or city comparison (2.48%
FP), "unstated scope means ineligible" (3.19% FP), any ``remote_policy`` clause
(that is a preference filter, not geography, and its default ``["remote"]``
would silently skip every onsite role), and any parsing of the freeform
``Job.location`` / ``profile.location``.

Every rule abstains unless the parsed fields alone settle it, because the parse
is thinner than the JD — 40.1% of engineering parses leave ``job_country`` null
while the location line states it plainly — and ``abstain`` means "let Pro
decide", the status quo. A wrong ``ineligible`` costs the user a job they will
never see; a wrong ``eligible`` or ``abstain`` only costs a Pro call, so the
classifier is generous about including and stingy about excluding.

Bump ``GATE_VERSION`` on any rule change so a recorded decision can be traced
to the logic that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from models.job import ParsedJD
from models.profile import MasterProfile

#: Bump on any change to a rule, an alias, or the scope classifier; recorded
#: alongside decisions so a replay can tell which logic produced them.
GATE_VERSION = 1

#: ``ineligible`` — provably out of reach, safe to skip the Pro call.
#: ``eligible`` — provably reachable (only the US-remote exception gets here).
#: ``abstain`` — not settled by the parsed fields; Pro decides, as today.
Verdict = Literal["ineligible", "eligible", "abstain"]


@dataclass(frozen=True)
class GeoDecision:
    """One gate outcome, plus enough to explain it in a log line or a replay.

    ``rule`` is a fixed clause label rather than free text, so a replay can
    group by it; the two normalized countries are carried because an abstain is
    almost always explained by one of them being ``None``.
    """

    verdict: Verdict
    rule: str
    residence_country: str | None
    job_country: str | None


# --------------------------------------------------------------- normalization

# Strings that appear where a country name should be; ``"null"`` as four
# literal characters is the model writing the JSON word out.
#
# ``_NULLISH`` alone decides whether ``remote_scope`` was *stated at all*: an
# unreadable scope like "Remote" or "Various" must reach :func:`_classify_scope`
# and abstain there rather than be treated as absent, because an absent scope
# lets the country-mismatch rule fire.
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
# Listed explicitly, though an unrecognized string already normalizes to
# ``None``, so a reader extending ``_COUNTRY_ALIASES`` can see they were
# considered and rejected rather than merely missed.
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

# Countries observed in the corpus, those named inside observed
# ``remote_scope`` strings, and unambiguous remote-hiring countries.
# Deliberately absent: country names that collide with a US state ("Georgia"),
# since a US posting must never normalize to a foreign country.
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    # Live data disagrees with itself: profiles store both "US" and "United
    # States", and ``job_country`` carries "US", "USA" and "United States" in
    # the same collection.
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

    Punctuation becomes a separator rather than being stripped, so "U.S." reads
    as the two tokens ``u s`` and "Canada/USA" does not become one unmatchable
    word. Callers must then match whole token runs, never substrings, or
    "Cyprus" and "Australia" both look like the United States.
    """
    return _NON_ALNUM.sub(" ", value.casefold()).strip()


def normalize_country(value: str | None) -> str | None:
    """A country string to its code, or ``None`` when we cannot be sure.

    An exact alias match on the normalized string, never a search within it:
    "Remote - USA" in ``job_country`` is a parse that answered the wrong
    question. Anything unrecognized is ``None``, which means abstain, never a
    mismatch.
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

# Region names, as the country codes they contain. The classifier only asks
# whether the resident is inside, so a region needs only the members this
# module can also name.
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

# Phrases meaning "no geographic restriction"; matching one includes every
# residence, the generous direction, which costs at worst an abstain.
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

# The guard that removes every measured scope false positive: timezone
# vocabulary describes working hours, not where a worker may live ("EST and EU"
# is a meeting window, not an exclusion). A scope containing any of these is
# ``unknown`` whatever else it says, including scopes we could otherwise have
# read — coverage knowingly given up.
#
# Bare "et"/"ct"/"mt"/"pt" match as whole tokens only ("Portugal" is a word,
# not "pt"); "time" and "hours" catch the long tail. Only American and European
# abbreviations are listed: a scope that is *only* a timezone names no place the
# classifier recognizes and already lands on ``unknown``.
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
# first so the classifier consumes "south america" before "america"; without
# that ordering "South America" would read as covering the United States.
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

    ``excludes`` is only returned after recognizing at least one real place and
    finding the resident in none of them. Anything half-understood — a bare list
    of US state codes, a timezone window, an unaliased country — is ``unknown``,
    and the caller abstains.
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

    Pure: no I/O, no model, no clock. Rule order is load-bearing — every
    exclusion rule must come after the cheaper ``us_remote_ok`` exception, where
    73.4% of the kept corpus lands.
    """
    residence_country = (
        normalize_country(profile.residence.country) if profile.residence else None
    )
    job_country = normalize_country(parsed.job_country)

    # 1. No usable residence, nothing to compare against. This deliberately
    #    does NOT fall back to profile.location the way the prompt's
    #    _residence_str does; see the module docstring.
    if residence_country is None:
        return GeoDecision("abstain", "no_residence", None, job_country)

    # 2. An explicit "US remote welcome" settles it for a US resident, wherever
    #    the office sits.
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
        # An "includes" scope falls through rather than returning eligible:
        # not excluded is not the same as a match.
    # 4. A foreign office and no scope at all to widen it — the only Rule 6
    #    clause that replayed at 0.00% FP. It stays behind the scope check
    #    because a stated scope is the JD correcting the office address.
    elif job_country is not None and job_country != residence_country:
        return GeoDecision(
            "ineligible", "country_mismatch", residence_country, job_country
        )

    # 5. Nothing provable. The three ways of getting here must not share a
    #    label: these strings are written to Firestore, where a catch-all
    #    becomes permanently ambiguous.
    if job_country == residence_country:
        return GeoDecision("abstain", "same_country", residence_country, job_country)
    if job_country is not None:
        # Only reachable when a stated scope classified as ``includes``: the
        # office is abroad but the JD named the resident's country as in scope.
        # Distinct from ``country_unknown``, where no country was read at all.
        return GeoDecision(
            "abstain", "scope_covers_foreign_office", residence_country, job_country
        )
    # job_country absent, junk, or unaliased — we simply could not tell.
    return GeoDecision("abstain", "country_unknown", residence_country, job_country)
