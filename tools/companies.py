# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Centralized loader for the three company files.

This module is the only thing in the codebase that touches the company YAML
files. Everything else goes through it.

It is read-only apart from :func:`append_unvetted`, which the offline sweep
(``cli.discover_companies``) uses to grow the pool from a laptop, and
:func:`fill_missing_names`, which ``cli.board_names`` uses to backfill names. Nothing here
mutates the YAML on behalf of a request: an edit inside the container serving
the request is invisible to the crawl under ``QUEUE_MODE=1`` and is replaced
by the next deploy. Per-user exclusions are a Firestore overlay
(:mod:`tools.company_prefs`) subtracted at compose time by
:func:`all_active_companies`; promotion is a reviewed edit to ``known.yaml``.
"""

from __future__ import annotations

import re
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


def new_unvetted_slugs(platform: Platform, slugs: list[str]) -> list[str]:
    """The slugs :func:`append_unvetted` would add, in order, spelling kept.

    Case-insensitive, so a case variant of an existing unvetted, known or
    blocklisted slug (or of one earlier in ``slugs``) is skipped.
    """
    raw = _load(DATA_DIR / "unvetted.yaml")
    existing = {c["slug"].casefold() for c in raw.get(platform, [])}
    known = {c.slug.casefold() for c in load_known()[platform]}
    blocked = {slug.casefold() for plat, slug in load_blocklist() if plat == platform}

    skip = existing | known | blocked
    out: list[str] = []
    for s in slugs:
        if s.casefold() not in skip:
            skip.add(s.casefold())
            out.append(s)
    return out


def append_unvetted(platform: Platform, entries: list[tuple[str, str | None]]) -> int:
    """Append ``(slug, name)`` entries to unvetted.yaml. Returns count added.

    Deduped as :func:`new_unvetted_slugs` does; ``name`` is written only when
    it is known.

    One of the two writers here (with :func:`fill_missing_names`) and the
    only way the global pool grows:
    ``cli.discover_companies`` runs the sweep on a laptop and the diff is
    committed. It is a read-modify-write, survivable only because of where it
    runs — one process, one working copy, a human reviewing the result.
    Nothing on the request path may write here; per-user exclusions are an
    overlay in Firestore (:mod:`tools.company_prefs`).
    """
    names: dict[str, str | None] = {}
    for slug, name in entries:
        names.setdefault(slug, name)
    to_add = new_unvetted_slugs(platform, list(names))
    if not to_add:
        return 0

    today = date.today().isoformat()
    raw = _load(DATA_DIR / "unvetted.yaml")
    raw.setdefault(platform, []).extend(
        [
            {"slug": s, "name": names[s], "added": today}
            if names[s]
            else {"slug": s, "added": today}
            for s in to_add
        ]
    )
    (DATA_DIR / "unvetted.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    )
    return len(to_add)


_SECTION_RE = re.compile(r"^([A-Za-z_]+):\s*(?:#.*)?$")
_SLUG_LINE_RE = re.compile(r"^(\s*-\s+)slug:\s*(.*?)\s*$")


def _yaml_scalar(value: str) -> str:
    """``value`` as :func:`append_unvetted`'s ``safe_dump`` writes it, on one line."""
    dumped = yaml.safe_dump({"k": value}, allow_unicode=True, width=float("inf"))
    return dumped.removeprefix("k: ").rstrip("\n")


def fill_missing_names(
    filename: Literal["known.yaml", "unvetted.yaml"],
    names: dict[tuple[str, str], str],
) -> int:
    """Set ``name`` on entries of ``filename`` that have none. Returns count set.

    ``names`` is keyed by ``(platform, slug)``, slug spelled as in the file. An
    entry that already has a ``name`` key is never touched. The edit is a
    line insertion right after the entry's ``- slug:`` line, so comments,
    quoting and order survive; the result is re-parsed and nothing is written
    unless it equals the original with just those names added.
    """
    path = DATA_DIR / filename
    text = path.read_text()
    raw = yaml.safe_load(text) or {}
    wanted = {
        (plat, e["slug"]): names[(plat, e["slug"])]
        for plat, entries in raw.items()
        for e in entries or []
        if "name" not in e and (plat, e["slug"]) in names
    }
    if not wanted:
        return 0

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    section: str | None = None
    for line in lines:
        out.append(line)
        if m := _SECTION_RE.match(line):
            section = m.group(1)
            continue
        m = _SLUG_LINE_RE.match(line)
        if m is None or section is None:
            continue
        key = (section, str(yaml.safe_load(m.group(2))))
        if key in wanted:
            indent = " " * len(m.group(1))
            out.append(f"{indent}name: {_yaml_scalar(wanted[key])}\n")

    expected = {
        plat: [
            {"slug": e["slug"], "name": wanted[(plat, e["slug"])], **e}
            if (plat, e["slug"]) in wanted
            else e
            for e in entries or []
        ]
        if entries is not None
        else None
        for plat, entries in raw.items()
    }
    new_text = "".join(out)
    if yaml.safe_load(new_text) != expected:
        raise RuntimeError(
            f"{filename}: name insertion did not round-trip; not written"
        )
    path.write_text(new_text)
    return len(wanted)


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
