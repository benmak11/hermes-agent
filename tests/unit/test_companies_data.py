# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Lint over the real ``data/companies`` YAML, so board-list drift fails CI.

Reads the checked-in files, not a fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import tools.companies as tc

REAL_DIR = Path(__file__).resolve().parents[2] / "data" / "companies"


def _raw(name: str) -> dict:
    return yaml.safe_load((REAL_DIR / name).read_text()) or {}


def _pool() -> list[tuple[str, str, dict]]:
    """Every (file, platform, raw entry) in known + unvetted."""
    out = []
    for name in ("known.yaml", "unvetted.yaml"):
        for platform, entries in _raw(name).items():
            out.extend((name, platform, e) for e in entries or [])
    return out


def test_the_lint_reads_the_real_files() -> None:
    assert (REAL_DIR / "known.yaml").is_file()
    assert (REAL_DIR / "unvetted.yaml").is_file()
    assert (REAL_DIR / "blocklist.yaml").is_file()
    assert len(_pool()) > 50


@pytest.mark.parametrize("name", ["known.yaml", "unvetted.yaml"])
def test_every_section_is_a_platform(name: str) -> None:
    assert set(_raw(name)) <= set(tc.PLATFORMS)


def test_every_entry_parses_with_no_unknown_fields() -> None:
    fields = set(tc.CompanyEntry.model_fields)
    for name, platform, entry in _pool():
        tc.CompanyEntry.model_validate(entry)
        assert set(entry) <= fields, (name, platform, entry)


def test_no_two_slugs_on_a_platform_collide_ignoring_case() -> None:
    seen: dict[tuple[str, str], str] = {}
    for name, platform, entry in _pool():
        key = (platform, entry["slug"].casefold())
        assert key not in seen, (
            f"{platform}/{entry['slug']} in {name} collides with {seen.get(key)}"
        )
        seen[key] = f"{platform}/{entry['slug']} in {name}"


def test_every_blocklist_entry_parses() -> None:
    blocked = _raw("blocklist.yaml")["blocked"]
    assert isinstance(blocked, list)
    for entry in blocked:
        tc.BlockEntry.model_validate(entry)


def test_no_active_slug_is_blocklisted() -> None:
    blocked = {
        (e["platform"], e["slug"].casefold()) for e in _raw("blocklist.yaml")["blocked"]
    }
    active = [
        (name, platform, entry["slug"])
        for name, platform, entry in _pool()
        if not entry.get("paused")
    ]
    hits = [a for a in active if (a[1], a[2].casefold()) in blocked]
    assert hits == []


def test_every_name_is_a_non_empty_trimmed_string() -> None:
    """``name`` is the company's identity across platforms; a blank one is no name."""
    for name, platform, entry in _pool():
        if "name" not in entry:
            continue
        value = entry["name"]
        assert isinstance(value, str), (name, platform, entry)
        assert value.strip() == value != "", (name, platform, entry)
