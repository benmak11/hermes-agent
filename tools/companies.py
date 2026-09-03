# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Centralized loader for the three company files.

This module is the only thing in the codebase that touches the company YAML
files. Everything else goes through it.

It is read-only apart from :func:`append_unvetted`, which the offline sweep
(``cli.discover_companies``) uses to grow the pool from a laptop. The
promote/block/dismiss/pause mutators that used to live here are gone: they
edited the YAML inside whichever container served the API request, so under
``QUEUE_MODE=1`` the crawl — running in a *different* container — never saw
the edit, and a deploy replaced the filesystem anyway. Per-user exclusions are
now a Firestore overlay (:mod:`tools.company_prefs`) subtracted at compose time
by :func:`all_active_companies`; promotion is a reviewed edit to
``known.yaml``.
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
    """Return as a set for O(1) membership checks."""
    raw = _load(DATA_DIR / "blocklist.yaml")
    return {(e["platform"], e["slug"]) for e in raw.get("blocked", [])}


def load_blocklist_detailed() -> list[dict]:
    """Full blocklist entries (for the company-management API)."""
    raw = _load(DATA_DIR / "blocklist.yaml")
    return raw.get("blocked", []) or []


def append_unvetted(platform: Platform, new_slugs: list[str]) -> int:
    """Append new slugs to unvetted.yaml. Returns count actually added (after dedup).

    The only writer left in this module, and the only way the global pool grows:
    ``cli.discover_companies`` runs the sweep on a laptop and the diff is
    committed. It is a read-modify-write and that is survivable *only* because
    of where it runs — one process, one working copy, a human reviewing the
    result. Nothing on the request path may write here; per-user exclusions are
    an overlay in Firestore (:mod:`tools.company_prefs`).
    """
    raw = _load(DATA_DIR / "unvetted.yaml")
    existing = {c["slug"] for c in raw.get(platform, [])}
    known = {c.slug for c in load_known()[platform]}
    blocked = {slug for plat, slug in load_blocklist() if plat == platform}

    skip = existing | known | blocked
    to_add = [s for s in new_slugs if s not in skip]
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

    ``exclusions`` is the per-user overlay read by
    :func:`tools.company_prefs.load_exclusions` — the pool above is global and
    stays in YAML; what one user has excluded is subtracted here, at compose
    time, so nothing about the shared pool has to know a user exists.

    It defaults to empty, which composes exactly the set this has always
    composed. That default is what makes every existing caller unchanged.
    """
    out: list[tuple[Platform, str, Literal["known", "unvetted"]]] = []
    for plat, entries in load_known().items():
        out.extend(
            (plat, e.slug, "known")
            for e in entries
            if not e.paused and (plat, e.slug) not in exclusions
        )
    for plat, entries in load_unvetted().items():
        out.extend(
            (plat, e.slug, "unvetted")
            for e in entries
            if (plat, e.slug) not in exclusions
        )
    return out
