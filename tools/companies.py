# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Centralized loader for the three company files.

This module is the only thing in the codebase that touches the company YAML
files. Everything else goes through it.

It is read-only apart from :func:`append_unvetted`, which the offline sweep
(``cli.discover_companies``) uses to grow the pool from a laptop. Nothing here
mutates the YAML on behalf of a request: an edit inside the container serving
the request is invisible to the crawl under ``QUEUE_MODE=1`` and is replaced
by the next deploy. Per-user exclusions are a Firestore overlay
(:mod:`tools.company_prefs`) subtracted at compose time by
:func:`all_active_companies`; promotion is a reviewed edit to ``known.yaml``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

# Multi-tenant ATS platforms use a company slug; the single-company career
# sites (google_jobs, meta_jobs) reuse the slot as a *search query* instead.
Platform = Literal["greenhouse", "lever", "ashby", "google_jobs", "meta_jobs"]
PLATFORMS: list[Platform] = ["greenhouse", "lever", "ashby", "google_jobs", "meta_jobs"]

DATA_DIR = Path("data/companies")


class CompanyEntry(BaseModel):
    slug: str
    name: str | None = None  # display name; the company's identity across platforms
    added: date | None = None
    notes: str | None = None
    paused: bool = False  # temporarily excluded from the daily fetch


class BlockEntry(BaseModel):
    platform: Platform
    slug: str
    blocked_at: date
    reason: str


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_known() -> dict[Platform, list[CompanyEntry]]:
    raw = _load(DATA_DIR / "known.yaml")
    return {p: [CompanyEntry(**c) for c in raw.get(p, [])] for p in PLATFORMS}


def load_unvetted() -> dict[Platform, list[CompanyEntry]]:
    raw = _load(DATA_DIR / "unvetted.yaml")
    return {p: [CompanyEntry(**c) for c in raw.get(p, [])] for p in PLATFORMS}


def load_blocklist() -> set[tuple[Platform, str]]:
    """Return as a set for O(1) membership checks. Slugs keep their spelling."""
    raw = _load(DATA_DIR / "blocklist.yaml")
    return {(e["platform"], e["slug"]) for e in raw.get("blocked", [])}


def load_blocklist_detailed() -> list[dict]:
    """Full blocklist entries (for the company-management API)."""
    raw = _load(DATA_DIR / "blocklist.yaml")
    return raw.get("blocked", []) or []


def append_unvetted(platform: Platform, new_slugs: list[str]) -> int:
    """Append new slugs to unvetted.yaml. Returns count actually added (after dedup).

    The only writer in this module and the only way the global pool grows:
    ``cli.discover_companies`` runs the sweep on a laptop and the diff is
    committed. It is a read-modify-write, survivable only because of where it
    runs — one process, one working copy, a human reviewing the result.
    Nothing on the request path may write here; per-user exclusions are an
    overlay in Firestore (:mod:`tools.company_prefs`).
    """
    raw = _load(DATA_DIR / "unvetted.yaml")
    existing = {c["slug"].casefold() for c in raw.get(platform, [])}
    known = {c.slug.casefold() for c in load_known()[platform]}
    blocked = {slug.casefold() for plat, slug in load_blocklist() if plat == platform}

    # Case-insensitive, so a case variant of an existing, known or blocklisted
    # slug (or of one earlier in this batch) is skipped; the spelling is kept.
    skip = existing | known | blocked
    to_add: list[str] = []
    for s in new_slugs:
        if s.casefold() not in skip:
            skip.add(s.casefold())
            to_add.append(s)
    if not to_add:
        return 0

    raw.setdefault(platform, []).extend(
        [{"slug": s, "added": date.today().isoformat()} for s in to_add]
    )
    (DATA_DIR / "unvetted.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    return len(to_add)


def all_active_companies(
    exclusions: frozenset[tuple[Platform, str]] = frozenset(),
) -> list[tuple[Platform, str, Literal["known", "unvetted"]]]:
    """Flat list of (platform, slug, source) tuples to fetch on a daily run.

    Blocklisted boards are dropped (platform exact, slug ignoring case). A slug
    listed twice on one platform, in any case, composes once with the first
    spelling seen, ``known`` before ``unvetted``. Slugs are never lowercased:
    Lever's API is case-sensitive.

    ``exclusions`` is the per-user overlay read by
    :func:`tools.company_prefs.load_exclusions`. The pool stays global in
    YAML and one user's exclusions are subtracted here, at compose time, so
    nothing about the shared pool has to know a user exists. It defaults to
    empty, which composes the whole pool.
    """
    seen = {(plat, slug.casefold()) for plat, slug in load_blocklist()}
    out: list[tuple[Platform, str, Literal["known", "unvetted"]]] = []
    sources: list[
        tuple[Literal["known", "unvetted"], dict[Platform, list[CompanyEntry]]]
    ] = [
        ("known", load_known()),
        ("unvetted", load_unvetted()),
    ]
    for source, groups in sources:
        for plat, entries in groups.items():
            for e in entries:
                key = (plat, e.slug.casefold())
                if key in seen or (plat, e.slug) in exclusions:
                    continue
                if source == "known" and e.paused:
                    continue
                seen.add(key)
                out.append((plat, e.slug, source))
    return out
