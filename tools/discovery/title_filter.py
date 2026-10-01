# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Free title -> role-family pre-filter, applied between fetch and persist.

Company boards return every open role, but 71% of the 12K-job backlog scored
out-of-family — each one a Flash parse (plus Firestore churn) spent only to
learn the title said "Account Executive" all along. This filter drops those
jobs before they are ever persisted or parsed.

Contract: **precision over recall**. A job is dropped only when its title
classifies *confidently* into a family outside the user's
``target_role_families``. Anything ambiguous ("Data Engineer", "Solutions
Architect", quirky startup titles) passes through — the Flash parse and its
``role_family`` gate in ``tools.matching`` remain the arbiter, exactly as
before. A wrong drop here is silent and unrecoverable within the run, so every
rule below should be obvious for a human screener too.

Dropped jobs get no tombstone: they are re-fetched and re-filtered next run,
which is free.

**The one-sided map.** The rules below were written for the families the user
*wants*, so a replay of the live 8,882-job backlog drops nothing at all: 63%
of it classifies as ``None`` and is kept. Precision-over-recall is the right
contract, but a keyword map with holes in it degrades that contract into "keep
everything". :data:`_WIDE_RULES` closes the measured holes, and ships behind
``TITLE_FILTER_WIDE`` (default **off**, the ``GEO_GATE_ENFORCE`` precedent) so
the rules can be measured by ``cli.title_replay`` before they act on anyone.
"""

from __future__ import annotations

import os
import re
from collections import Counter

from google.cloud import firestore
from pydantic import ValidationError

from models.job import Job
from models.profile import JobPreferences
from obs.logging import get_logger

log = get_logger("tools.discovery.title_filter")

# Bumped whenever a rule changes, mirroring ``geo.GATE_VERSION``: a drop made
# under one version of the map has to be resolvable against a later one.
# Nothing stamps it yet — the filter writes no tombstones.
TITLE_FILTER_VERSION = 1

# Titles that straddle families (or read differently to different screeners).
# These short-circuit to "unclassified" so Flash decides, never a keyword.
_AMBIGUOUS = re.compile(
    r"""
    \bdata\s+engineer |            # data vs engineering
    \bmachine\s+learning | \bml\b | \bai\b | artificial\s+intelligence |
    \banalytics\s+engineer |
    \bresearch |                   # UX research=design, research scientist=data/eng
    \bsolutions?\s+(architect|engineer|consultant) |  # pre-sales vs engineering
    \bsales\s+engineer | \bpre-?sales |
    \bsupport\s+engineer |         # customer-success vs engineering

    \barchitect\b |                # data/enterprise/solutions architect
    \b(technical\s+)?program\s+manager |  # eng/product/operations
    \bdeveloper\s+(advocate|relations) | \bdevrel\b |
    \bcommunity\b | \bgrowth\b     # marketing vs engineering vs product
    """,
    re.VERBOSE,
)

# First match wins, so compound titles must resolve before their generic
# parts: "Product Designer" is design (not product), "Product Marketing
# Manager" is marketing, "People Ops" is people (not operations), "Salesforce
# Developer" is engineering (word boundary keeps \bsales\b off "salesforce").
# Families mirror the ParsedJD.role_family taxonomy in models/job.py.
_RULES: list[tuple[str, re.Pattern]] = [
    ("design", re.compile(r"\bdesigner\b|\bux\b|\bui\s+design|\buser\s+experience")),
    (
        "engineering",
        re.compile(
            r"\bengineer(ing)?\b|\bdeveloper\b|\bprogrammer\b|\bsoftware\b"
            r"|\bsre\b|\bdevops\b|\bqa\b"
        ),
    ),
    (
        "data",
        re.compile(
            r"\bdata\s+(scientist|analyst|science)\b|\banalytics\b"
            r"|\bbusiness\s+intelligence\b"
        ),
    ),
    (
        "product",
        re.compile(
            r"\bproduct\s+(manager|owner|management|lead|director)\b"
            r"|\b(head|director|vp)\s+of\s+product\b"
        ),
    ),
    (
        "people",
        re.compile(
            r"\brecruit|\btalent\b|\bpeople\b"
            r"|\bhr\b|\bhuman\s+resources\b"
        ),
    ),
    (
        "customer-success",
        re.compile(
            r"\bcustomer\s+(success|support|experience|service)\b"
            r"|\bsupport\s+(specialist|representative|agent|manager)\b|\bcsm\b"
        ),
    ),
    (
        "sales",
        re.compile(
            r"\bsales\b|\baccount\s+(executive|manager)\b"
            r"|\bbusiness\s+development\b|\bsdr\b|\bbdr\b|\bpartnerships?\b"
        ),
    ),
    (
        "finance",
        re.compile(
            r"\bfinanc(e|ial)\b|\baccount(ant|ing)\b|\bcontroller\b|\bfp&a\b"
            r"|\btax\b|\btreasury\b|\bpayroll\b|\bbilling\b|\bauditor\b"
        ),
    ),
    (
        "legal",
        re.compile(r"\blegal\b|\bcounsel\b|\bparalegal\b|\battorney\b|\bcompliance\b"),
    ),
    (
        "marketing",
        re.compile(
            r"\bmarketing\b|\bbrand\b|\bcontent\b|\bseo\b|\bcopywriter\b"
            r"|\bcommunications\b|\bsocial\s+media\b|\bdemand\s+generation\b"
        ),
    ),
    (
        "operations",
        re.compile(
            r"\boperations\b|\bops\b|\bsupply\s+chain\b|\blogistics\b"
            r"|\bprocurement\b|\bworkplace\b|\bfacilities\b"
            r"|\bexecutive\s+assistant\b|\bchief\s+of\s+staff\b|\boffice\s+manager\b"
            r"|\badministrative\b"
        ),
    ),
]


# The widened map, off by default. Every rule here closes a hole measured in
# the live backlog.
#
# **What makes these rules safe is the ordering, not the wording.** They run
# last, and only on titles _AMBIGUOUS and _RULES both had nothing to say
# about, so widening can turn "kept because we don't know" into a drop but
# can never overrule either. Do not read the phrases below as safe in
# themselves: "user acquisition", "ad operations", "paid media", "customer
# onboarding", "growth marketing" and "project management" all occur in real
# engineering titles, and every one of them is neutralised by an earlier rule
# matching first, not by the noun being unambiguous.
#
# So the test a new rule has to pass is not "does this phrase sound
# out-of-family" — it is "is every title containing it already caught by
# _AMBIGUOUS or _RULES, and is it still out-of-family when it isn't". Both
# times that test was skipped the replay caught it: \bwarehouse\b against
# *Data Warehouse Engineer*, and \bunderwriting\b against a *Machine Learning
# Engineer, Capital Underwriting* that Pro scored 53. \bdriver\b (device
# driver engineer) and \bscheduler\b were rejected the same way.
#
# The ordering was learned the same way. The first cut ran these rules first
# and the replay caught it dropping eleven jobs the narrow map calls
# engineering, among them *Staff Engineer - Customer Onboarding*.
#
# Left deliberately unclassified, and therefore kept: QA, security, web
# developer, integration developer, solutions engineer, sales engineer, data
# annotation, program manager, business analyst. They are engineering-adjacent
# or genuinely ambiguous, and an unclassified title is kept while a
# misclassified one is dropped and never seen again.
_WIDE_RULES: list[tuple[str, re.Pattern]] = [
    (
        "marketing",
        re.compile(
            r"""
            \bmarketing\s+(and\s+|&\s+)?
                (sales|specialist|manager|associate|coordinator|assistant|
                 executive|intern)\b |
            \bppc\b |                                  # pay-per-click
            \buser\s+acqui\w* |                        # …acquisition, and the
                                                       # backlog's misspelling
                                                       # of it
            \b(paid|performance)\s+(media|search|social|acquisition)\b |
            \bmedia\s+buyer\b |
            \bplayable\s+ads?\b |
            \b(ad|ads|advertising)\s+
                (editor|creative|operations|ops|specialist|manager|buyer|
                 trafficker)\b |
            \bgrowth\s+(marketer|marketing|specialist|associate|coordinator)\b
            """,
            re.VERBOSE,
        ),
    ),
    (
        "sales",
        re.compile(
            r"""
            \b(client|customer|revenue)\s+growth\s+(manager|lead|director)\b |
            \baccount\s+(director|coordinator|specialist|associate|supervisor)\b |
            \bterritory\s+(manager|representative)\b
            """,
            re.VERBOSE,
        ),
    ),
    (
        "customer-success",
        re.compile(
            r"""
            \b(client|customer)\s+
                (care|relations|advocate|onboarding|engagement)\b |
            \bclient\s+success\b |
            \b(call|contact)\s+cent(er|re)\b
            """,
            re.VERBOSE,
        ),
    ),
    (
        # No "program/project" family exists in the ParsedJD taxonomy, and
        # delivery management sits closer to product than to operations.
        # _AMBIGUOUS keeps *program* manager unclassified (it is often an
        # engineering title); *project* manager is the one the backlog leaks.
        "product",
        re.compile(
            r"""
            # No "lead": *Engineering Project Lead* is an IC engineering
            # title, while *Project Manager* is a delivery role in any
            # industry.
            \bproject\s+(manager|management|coordinator)\b |
            \bscrum\s+master\b |
            \bproduct\s+(analyst|specialist|operations)\b
            """,
            re.VERBOSE,
        ),
    ),
    ("design", re.compile(r"\bart\s+director\b")),
    (
        "finance",
        re.compile(
            r"""
            \baccounts\s+(payable|receivable)\b | \bbookkeep\w* |
            # \bunderwriter\b only: "…- Underwriting" is a *domain*, and
            # engineers are hired into it.
            \bunderwriter\b | \bactuar(y|ial)\b |
            \bcredit\s+analyst\b | \bcollections\s+specialist\b
            """,
            re.VERBOSE,
        ),
    ),
    (
        "people",
        re.compile(
            r"""
            \bsourcer\b | \bstaffing\b | \bhris\b |
            \bbenefits\s+(specialist|administrator|manager)\b |
            \bcompensation\s+(analyst|manager|partner)\b
            """,
            re.VERBOSE,
        ),
    ),
    (
        "legal",
        re.compile(
            r"""
            \bcontracts?\s+(manager|specialist|administrator)\b |
            \blitigation\b | \bregulatory\s+affairs\b
            """,
            re.VERBOSE,
        ),
    ),
    (
        "operations",
        re.compile(
            r"""
            \bdata\s+entry\b | \breceptionist\b | \bdispatcher?\b |
            # Qualified, because *Data Warehouse Engineer* is engineering.
            \bwarehouse\s+(associate|worker|clerk|operative|supervisor)\b |
            \bcustodian\b | \bjanitor\w* | \bcourier\b |
            \bvirtual\s+assistant\b |
            \binventory\s+(clerk|specialist|manager|analyst)\b
            """,
            re.VERBOSE,
        ),
    ),
]

# Every outcome :func:`evaluate_title` can return. Only "drop" loses a job.
OUTCOMES = ("no-filter", "target-title", "in-family", "unclassified", "drop")


def wide_enabled() -> bool:
    """Whether the widened out-of-family rules are switched on.

    Off by default and read per call, exactly like
    ``tools.matching.pipeline.geo_enforce_enabled``: the flag is hand-set
    Cloud Run env, and a module-level constant would pin whatever the process
    started with.
    """
    return os.getenv("TITLE_FILTER_WIDE", "").strip().lower() in {"1", "true", "on"}


def classify_title(title: str, *, wide: bool | None = None) -> str | None:
    """Best-effort role family for a job title; None when not confident.

    ``wide`` overrides the ``TITLE_FILTER_WIDE`` env flag, which is what lets
    ``cli.title_replay`` measure the candidate map without switching it on for
    anybody. Leave it ``None`` in production code.
    """
    t = title.lower()
    if _AMBIGUOUS.search(t):
        # Not confident, so: kept. The wide rules do not get a second opinion
        # here — _AMBIGUOUS is built out of the user's own target nouns, and
        # letting the wide map resolve one of them is how *Machine Learning
        # Engineer, Capital Underwriting* got dropped.
        return None
    for family, pattern in _RULES:
        if pattern.search(t):
            return family
    if wide_enabled() if wide is None else wide:
        # Reached only when neither the ambiguity guard nor the narrow map
        # has anything to say, so the widening can add drops and can never
        # move a job either of them already placed.
        for family, pattern in _WIDE_RULES:
            if pattern.search(t):
                return family
    return None


def evaluate_title(
    title: str, preferences: JobPreferences | None, *, wide: bool | None = None
) -> tuple[str, str | None]:
    """``(outcome, family)`` for one title — the whole pre-filter decision.

    Split out of :func:`prefilter_jobs` so the replay CLI measures the
    function discovery actually runs, rather than a re-implementation of it
    that can drift into flattering the map.
    """
    if preferences is None or not preferences.target_role_families:
        return "no-filter", None
    t = title.lower()
    if any(w.lower() in t for w in preferences.target_titles):
        return "target-title", None
    family = classify_title(t, wide=wide)
    if family is None:
        return "unclassified", None
    if family in {f.lower() for f in preferences.target_role_families}:
        return "in-family", family
    return "drop", family


def prefilter_jobs(
    jobs: list[Job], preferences: JobPreferences | None, *, wide: bool | None = None
) -> tuple[list[Job], Counter[str]]:
    """Split fetched jobs into (kept, dropped-count-by-family).

    Keeps everything when there are no preferences to filter against, when the
    title matches one of the user's ``target_titles`` (explicit intent beats
    the keyword map), or when the title doesn't classify confidently.
    """
    if preferences is None or not preferences.target_role_families:
        return jobs, Counter()

    kept: list[Job] = []
    dropped: Counter[str] = Counter()
    for job in jobs:
        outcome, family = evaluate_title(job.title, preferences, wide=wide)
        if outcome == "drop" and family is not None:
            dropped[family] += 1
        else:
            kept.append(job)

    if dropped:
        # ``wide`` and ``version`` are stamped because the moment the flag
        # flips this line is the only production evidence of what the
        # widening did, and a wide drop is otherwise indistinguishable from a
        # narrow one. ``geo`` stamps GATE_VERSION for the same reason.
        log.info(
            "discovery.title_filtered",
            dropped=sum(dropped.values()),
            kept=len(kept),
            by_family=dict(dropped),
            wide=wide_enabled() if wide is None else wide,
            version=TITLE_FILTER_VERSION,
        )
    return kept, dropped


async def load_job_preferences(user_id: str) -> JobPreferences | None:
    """The user's job preferences, or None when absent/incomplete.

    None just means "don't pre-filter" — discovery must keep working for a
    user who hasn't finished onboarding.
    """
    db = firestore.AsyncClient()
    doc = await db.collection("users").document(user_id).get()
    prefs = (doc.to_dict() or {}).get("preferences")
    if not prefs:
        return None
    try:
        return JobPreferences.model_validate(prefs)
    except ValidationError:
        log.warning("discovery.preferences_invalid", user_id=user_id)
        return None
