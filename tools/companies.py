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

Platform = Literal[
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"
]
PLATFORMS: list[Platform] = [
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "smartrecruiters",
    "recruitee",
]

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


def _save(filename: str, raw: dict) -> None:
    (DATA_DIR / filename).write_text(yaml.safe_dump(raw, sort_keys=False))


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
        out.extend((plat, e.slug, "known") for e in entries if not e.paused)
    for plat, entries in load_unvetted().items():
        out.extend((plat, e.slug, "unvetted") for e in entries)
    return out


# ---------------------------------------------------------------------------
# Mutations (used by the company-management API in Phase 5)
# ---------------------------------------------------------------------------
def _remove_slug(filename: str, platform: Platform, slug: str) -> None:
    raw = _load(DATA_DIR / filename)
    if raw.get(platform):
        raw[platform] = [c for c in raw[platform] if c.get("slug") != slug]
        _save(filename, raw)


def promote_to_known(platform: Platform, slug: str) -> None:
    """Move a discovered company from unvetted.yaml to known.yaml."""
    _remove_slug("unvetted.yaml", platform, slug)
    known = _load(DATA_DIR / "known.yaml")
    if not any(c.get("slug") == slug for c in known.get(platform, [])):
        known.setdefault(platform, []).append(
            {"slug": slug, "added": date.today().isoformat()}
        )
        _save("known.yaml", known)


def block_company(platform: Platform, slug: str, reason: str | None = None) -> None:
    """Add a company to blocklist.yaml and remove it from known + unvetted."""
    bl = _load(DATA_DIR / "blocklist.yaml")
    blocked = bl.get("blocked") or []
    if not any(
        e.get("platform") == platform and e.get("slug") == slug for e in blocked
    ):
        blocked.append(
            {
                "platform": platform,
                "slug": slug,
                "blocked_at": date.today().isoformat(),
                "reason": reason or "",
            }
        )
        bl["blocked"] = blocked
        _save("blocklist.yaml", bl)
    _remove_slug("known.yaml", platform, slug)
    _remove_slug("unvetted.yaml", platform, slug)


def dismiss_unvetted(platform: Platform, slug: str) -> None:
    """Remove a company from unvetted.yaml without blocking it."""
    _remove_slug("unvetted.yaml", platform, slug)


def set_paused(platform: Platform, slug: str, paused: bool = True) -> None:
    """Toggle the paused flag on a known company (temporarily skip its fetch)."""
    known = _load(DATA_DIR / "known.yaml")
    for c in known.get(platform, []):
        if c.get("slug") == slug:
            c["paused"] = paused
    _save("known.yaml", known)


def apply_company_action(
    platform: Platform, slug: str, action: str, reason: str | None = None
) -> None:
    """Dispatch a company-management action from the API."""
    if action == "promote":
        promote_to_known(platform, slug)
    elif action == "block":
        block_company(platform, slug, reason)
    elif action == "dismiss":
        dismiss_unvetted(platform, slug)
    elif action == "pause":
        set_paused(platform, slug, True)
    else:
        raise ValueError(f"unknown company action: {action!r}")
