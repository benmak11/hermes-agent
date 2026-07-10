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
"""

from __future__ import annotations

import re
from collections import Counter

from google.cloud import firestore
from pydantic import ValidationError

from models.job import Job
from models.profile import JobPreferences
from obs.logging import get_logger

log = get_logger("tools.discovery.title_filter")

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


def classify_title(title: str) -> str | None:
    """Best-effort role family for a job title; None when not confident."""
    t = title.lower()
    if _AMBIGUOUS.search(t):
        return None
    for family, pattern in _RULES:
        if pattern.search(t):
            return family
    return None


def prefilter_jobs(
    jobs: list[Job], preferences: JobPreferences | None
) -> tuple[list[Job], Counter[str]]:
    """Split fetched jobs into (kept, dropped-count-by-family).

    Keeps everything when there are no preferences to filter against, when the
    title matches one of the user's ``target_titles`` (explicit intent beats
    the keyword map), or when the title doesn't classify confidently.
    """
    if preferences is None or not preferences.target_role_families:
        return jobs, Counter()

    targets = {f.lower() for f in preferences.target_role_families}
    wanted_titles = [t.lower() for t in preferences.target_titles]

    kept: list[Job] = []
    dropped: Counter[str] = Counter()
    for job in jobs:
        title = job.title.lower()
        if any(w in title for w in wanted_titles):
            kept.append(job)
            continue
        family = classify_title(title)
        if family is None or family in targets:
            kept.append(job)
        else:
            dropped[family] += 1

    if dropped:
        log.info(
            "discovery.title_filtered",
            dropped=sum(dropped.values()),
            kept=len(kept),
            by_family=dict(dropped),
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
