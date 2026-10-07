# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""A free, deterministic pre-score ("prerank") for jobs nobody has paid to score.

Pure: no I/O, no model, no clock. It reads a plain mapping of a job's fields,
so the same function scores a ``users/{uid}/jobs`` doc, a ``discarded_jobs``
tombstone and a ``Job.model_dump()``. Every term is a fixed constant chosen
before looking at data; ``cli.prerank_eval`` measures them against Pro's past
scores. Nothing in the scoring path uses this yet.

Each term abstains (contributes 0) when its input is absent or malformed, and
every profile-dependent term abstains when there are no preferences. A masked
term contributes 0 *and* is left out of ``features``.

The ``location`` term is a soft penalty, never a rejection: the freeform
location line is exactly what ``tools.matching.geo`` refuses to parse, so it
may lower a job's rank but must never remove it.

Bump ``PRERANK_VERSION`` on any change to a weight, a rule or a vocabulary.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache

from pydantic import ValidationError

from models.job import Job, ParsedJD
from models.profile import JobPreferences, MasterProfile, Residence
from tools import companies
from tools.discovery.title_filter import classify_title
from tools.matching import geo, pipeline

PRERANK_VERSION = 1

W_TITLE_EXACT = 40.0
W_TITLE_OVERLAP = 25.0
W_FAMILY_IN = 15.0
W_FAMILY_OUT = -40.0
W_SENIORITY_IN = 10.0
W_SENIORITY_OUT = -20.0
W_LOCATION_FOREIGN = -30.0
W_LOCATION_HOME = 5.0
W_CURATED = 5.0
W_PARSED_IN = 15.0
W_PARSED_REJECT = -100.0

FEATURES = (
    "title_exact",
    "title_overlap",
    "family",
    "seniority",
    "location",
    "curated",
    "parsed",
)


@dataclass(frozen=True)
class Prerank:
    """One job's prerank: the total, each unmasked term's contribution, and
    the rule version that produced them."""

    score: float
    features: dict[str, float] = field(default_factory=dict)
    version: int = PRERANK_VERSION


# ------------------------------------------------------------------ profile


def preferences_from(user_doc: Mapping | None) -> JobPreferences | None:
    """``users/{uid}.preferences`` as a model, or ``None`` when unusable."""
    raw = (user_doc or {}).get("preferences")
    if not isinstance(raw, Mapping):
        return None
    try:
        return JobPreferences.model_validate(raw)
    except ValidationError:
        return None


def residence_country(user_doc: Mapping | None) -> str | None:
    """The residence country code, resolved exactly as ``geo.evaluate`` does:
    ``residence.country`` only, never a parse of the freeform ``location``."""
    residence = (user_doc or {}).get("residence")
    if not isinstance(residence, Mapping):
        return None
    country = residence.get("country")
    return geo.normalize_country(country) if isinstance(country, str) else None


# -------------------------------------------------------------------- title

_TOKEN = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset(
    {"a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to", "with"}
)

# Title token -> the ``target_seniorities`` levels it can mean. Small and
# explicit on purpose. A token mapping to several levels is in target when any
# of them is, which keeps "lead" and "associate" generous.
_SENIORITY_TOKENS: dict[str, frozenset[str]] = {
    "intern": frozenset({"intern"}),
    "internship": frozenset({"intern"}),
    "junior": frozenset({"junior"}),
    "jr": frozenset({"junior"}),
    "associate": frozenset({"junior", "mid"}),
    "senior": frozenset({"senior"}),
    "sr": frozenset({"senior"}),
    "staff": frozenset({"staff"}),
    "principal": frozenset({"principal"}),
    "lead": frozenset({"senior", "staff"}),
    "manager": frozenset({"manager"}),
    "director": frozenset({"director"}),
    "vp": frozenset({"vp"}),
    "head": frozenset({"vp", "director"}),
}

# IC level words dropped from a target title's "core". Management words stay:
# in "Engineering Manager" the noun is the role, not a qualifier.
_LEVEL_WORDS = frozenset(
    {
        "intern",
        "junior",
        "jr",
        "associate",
        "senior",
        "sr",
        "staff",
        "principal",
        "lead",
    }
)


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.casefold())


def _title_exact(title: str, prefs: JobPreferences) -> float:
    """``prefilter_jobs``' rule: a target title is a substring of the title.
    A blank target is ignored rather than matching everything."""
    lowered = title.lower()
    wanted = [t.lower() for t in prefs.target_titles if t.strip()]
    return W_TITLE_EXACT if any(w in lowered for w in wanted) else 0.0


def _title_overlap(title: str, prefs: JobPreferences) -> float:
    """Best share of any target's core tokens present in the title."""
    words = set(_tokens(title))
    best = 0.0
    for target in prefs.target_titles:
        core = set(_tokens(target)) - _STOPWORDS - _LEVEL_WORDS
        if core:
            best = max(best, len(core & words) / len(core))
    return W_TITLE_OVERLAP * best


def _family(title: str, prefs: JobPreferences) -> float:
    targets = {f.lower() for f in prefs.target_role_families}
    if not targets:
        return 0.0
    family = classify_title(title)
    if family is None:
        return 0.0
    return W_FAMILY_IN if family in targets else W_FAMILY_OUT


def title_levels(title: str) -> frozenset[str]:
    """Every ``target_seniorities`` level a title's seniority words could mean.
    Empty when the title carries none."""
    words = _tokens(title)
    levels: set[str] = set()
    for word in words:
        levels |= _SENIORITY_TOKENS.get(word, frozenset())
    if "vice" in words and "president" in words:
        levels.add("vp")
    if "manager" in levels and levels & {"senior"}:
        levels.add("senior-manager")
    return frozenset(levels)


def _norm_level(level: str) -> str:
    return "-".join(_tokens(level.replace("_", " ")))


def _seniority(title: str, prefs: JobPreferences) -> float:
    targets = {_norm_level(s) for s in prefs.target_seniorities if s.strip()}
    levels = title_levels(title)
    if not targets or not levels:
        return 0.0
    return W_SENIORITY_IN if levels & targets else W_SENIORITY_OUT


# ----------------------------------------------------------------- location

# Separators between places in a location line. "." is deliberately absent so
# "U.S." survives as one segment; normalize_country already reads it.
_LOCATION_SPLIT = re.compile(
    r"[,;/|()\[\]\n\u00b7\u2022\u2013\u2014-]+|\s+(?:or|and|&)\s+", re.IGNORECASE
)


def location_countries(location: str) -> frozenset[str]:
    """Country codes named by whole segments of a freeform location line."""
    found = set()
    for segment in _LOCATION_SPLIT.split(location):
        code = geo.normalize_country(segment)
        if code is not None:
            found.add(code)
    return frozenset(found)


def _location(location: str, residence: str) -> float:
    countries = location_countries(location)
    if residence in countries:
        return W_LOCATION_HOME
    if countries and "remote" not in location.casefold():
        return W_LOCATION_FOREIGN
    return 0.0


# ------------------------------------------------------------------ curated


@cache
def curated_slugs() -> frozenset[str]:
    """Casefolded ``known.yaml`` slugs on the multi-tenant ATS platforms.

    Cached per process. The single-company career sites are left out: their
    "slug" is a search query, not a company.
    """
    known = companies.load_known()
    return frozenset(
        entry.slug.casefold()
        for platform in ("greenhouse", "lever", "ashby")
        for entry in known.get(platform, [])
    )


# ------------------------------------------------------------------- parsed


def _parsed(
    raw: Mapping,
    job_id: str,
    prefs: JobPreferences,
    residence: str | None,
    enforce_geo: bool,
) -> float | None:
    """The real ``pipeline.prefilter`` verdict on a stored parse: rejected,
    in-family, or ``None`` when the parse does not validate.

    ``prefilter`` takes a ``Job`` and a ``MasterProfile`` and reads only the
    parse, the id, the target families and the residence, so it is driven
    with unvalidated stand-ins carrying just those. With ``enforce_geo`` it can
    log, as it does in production.
    """
    try:
        parsed = ParsedJD.model_validate(raw)
    except ValidationError:
        return None
    job = Job.model_construct(id=job_id, jd_parsed=parsed)
    profile = MasterProfile.model_construct(
        preferences=prefs,
        residence=Residence(country=residence) if residence else None,
    )
    skipped, _ = pipeline.prefilter(job, profile, enforce=enforce_geo)
    return W_PARSED_REJECT if skipped is not None else W_PARSED_IN


# --------------------------------------------------------------------- main


def prerank(
    job: Mapping,
    preferences: JobPreferences | None,
    residence: str | None,
    *,
    mask: frozenset[str] = frozenset(),
    enforce_geo: bool = False,
) -> Prerank:
    """Score one job from its fields. Never raises on a malformed field.

    ``residence`` is a country code (:func:`residence_country`). ``mask`` names
    terms to leave out. When ``jd_parsed`` is present and the prefilter keeps
    the job, ``parsed`` replaces ``family`` (which then contributes 0).
    ``enforce_geo`` is passed to ``pipeline.prefilter`` as ``enforce``.
    """
    features: dict[str, float] = {}
    title = job.get("title")
    title = title if isinstance(title, str) else ""

    def put(name: str, value: float) -> None:
        if name not in mask:
            features[name] = value

    if preferences is not None:
        put("title_exact", _title_exact(title, preferences))
        put("title_overlap", _title_overlap(title, preferences))
        put("family", _family(title, preferences))
        put("seniority", _seniority(title, preferences))
    else:
        for name in ("title_exact", "title_overlap", "family", "seniority"):
            put(name, 0.0)

    location = job.get("location")
    put(
        "location",
        _location(location, residence)
        if isinstance(location, str) and residence
        else 0.0,
    )

    company = job.get("company")
    put(
        "curated",
        W_CURATED
        if isinstance(company, str) and company.casefold() in curated_slugs()
        else 0.0,
    )

    if "parsed" not in mask:
        raw = job.get("jd_parsed")
        value = None
        if preferences is not None and isinstance(raw, Mapping):
            job_id = job.get("id") or job.get("job_id")
            value = _parsed(
                raw,
                job_id if isinstance(job_id, str) else "",
                preferences,
                residence,
                enforce_geo,
            )
        features["parsed"] = value or 0.0
        if value == W_PARSED_IN and "family" in features:
            features["family"] = 0.0

    return Prerank(score=sum(features.values()), features=features)


def _timestamp(value) -> float | None:
    """Seconds since the epoch for a datetime or ISO string; naive is UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def sort_key(score: float, discovered_at, job_id: str) -> tuple[float, float, str]:
    """Ascending-sort key for a ranking: score descending, then newest
    ``discovered_at`` first (missing or unreadable sorts as oldest), then job
    id ascending."""
    ts = _timestamp(discovered_at)
    return (-score, -ts if ts is not None else math.inf, job_id)
