# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Centralized loader/writer for the three company files.

This module is the only thing in the codebase that touches the company YAML
files. Everything else goes through it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

Platform = Literal["greenhouse", "lever", "ashby"]
PLATFORMS: list[Platform] = ["greenhouse", "lever", "ashby"]

DATA_DIR = Path("data/companies")


class CompanyEntry(BaseModel):
    slug: str
    added: date | None = None
    notes: str | None = None


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


def append_unvetted(platform: Platform, new_slugs: list[str]) -> int:
    """Append new slugs to unvetted.yaml. Returns count actually added (after dedup)."""
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


def all_active_companies() -> list[tuple[Platform, str, Literal["known", "unvetted"]]]:
    """Flat list of (platform, slug, source) tuples to fetch on a daily run."""
    out: list[tuple[Platform, str, Literal["known", "unvetted"]]] = []
    for plat, entries in load_known().items():
        out.extend((plat, e.slug, "known") for e in entries)
    for plat, entries in load_unvetted().items():
        out.extend((plat, e.slug, "unvetted") for e in entries)
    return out
