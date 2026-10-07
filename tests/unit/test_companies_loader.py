# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The company loader: blocklist enforcement and case-insensitive slug dedupe.

Slugs are compared with ``casefold()`` but never rewritten — Lever's API is
case-sensitive, so the spelling that composes is the spelling that is fetched.
"""

from __future__ import annotations

import pytest
import yaml

import tools.companies as tc


def _write(d, known=None, unvetted=None, blocked=None) -> None:
    (d / "known.yaml").write_text(yaml.safe_dump(known or {}))
    (d / "unvetted.yaml").write_text(yaml.safe_dump(unvetted or {}))
    (d / "blocklist.yaml").write_text(yaml.safe_dump({"blocked": blocked or []}))


def _block(platform: str, slug: str) -> dict:
    return {
        "platform": platform,
        "slug": slug,
        "blocked_at": "2026-10-06",
        "reason": "test",
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "companies"
    d.mkdir()
    monkeypatch.setattr(tc, "DATA_DIR", d)
    return d


# ---------------------------------------------------------------------------
# all_active_companies: blocklist
# ---------------------------------------------------------------------------
def test_a_blocklisted_board_is_not_composed(data_dir) -> None:
    _write(
        data_dir,
        known={"greenhouse": [{"slug": "stripe"}]},
        unvetted={"ashby": [{"slug": "deadco"}, {"slug": "liveco"}]},
        blocked=[_block("ashby", "deadco")],
    )

    assert tc.all_active_companies() == [
        ("greenhouse", "stripe", "known"),
        ("ashby", "liveco", "unvetted"),
    ]


def test_the_blocklist_matches_slugs_ignoring_case(data_dir) -> None:
    _write(
        data_dir,
        unvetted={"ashby": [{"slug": "Forward"}, {"slug": "forward"}]},
        blocked=[_block("ashby", "FORWARD")],
    )

    assert tc.all_active_companies() == []


def test_the_blocklist_matches_the_platform_exactly(data_dir) -> None:
    _write(
        data_dir,
        unvetted={"lever": [{"slug": "acme"}], "ashby": [{"slug": "acme"}]},
        blocked=[_block("ashby", "acme")],
    )

    assert tc.all_active_companies() == [("lever", "acme", "unvetted")]


def test_a_blocklisted_known_board_is_not_composed(data_dir) -> None:
    _write(
        data_dir,
        known={"greenhouse": [{"slug": "Stripe"}]},
        blocked=[_block("greenhouse", "stripe")],
    )

    assert tc.all_active_companies() == []


# ---------------------------------------------------------------------------
# all_active_companies: dedupe
# ---------------------------------------------------------------------------
def test_case_variants_on_one_platform_compose_once_keeping_the_first(
    data_dir,
) -> None:
    _write(data_dir, unvetted={"ashby": [{"slug": "Forward"}, {"slug": "forward"}]})

    assert tc.all_active_companies() == [("ashby", "Forward", "unvetted")]


def test_a_board_in_known_and_unvetted_composes_once_from_known(data_dir) -> None:
    _write(
        data_dir,
        known={"greenhouse": [{"slug": "stripe"}]},
        unvetted={"greenhouse": [{"slug": "STRIPE"}, {"slug": "newco"}]},
    )

    assert tc.all_active_companies() == [
        ("greenhouse", "stripe", "known"),
        ("greenhouse", "newco", "unvetted"),
    ]


def test_the_same_slug_on_two_platforms_is_two_boards(data_dir) -> None:
    _write(
        data_dir,
        known={"greenhouse": [{"slug": "stripe"}], "lever": [{"slug": "stripe"}]},
    )

    assert tc.all_active_companies() == [
        ("greenhouse", "stripe", "known"),
        ("lever", "stripe", "known"),
    ]


def test_a_lever_slug_keeps_its_case(data_dir) -> None:
    _write(data_dir, unvetted={"lever": [{"slug": "BestEgg"}]})

    assert tc.all_active_companies() == [("lever", "BestEgg", "unvetted")]


# ---------------------------------------------------------------------------
# append_unvetted
# ---------------------------------------------------------------------------
def _unvetted_slugs(d, platform: str) -> list[str]:
    raw = yaml.safe_load((d / "unvetted.yaml").read_text()) or {}
    return [c["slug"] for c in raw.get(platform, [])]


def test_append_skips_a_case_variant_of_an_existing_unvetted_slug(data_dir) -> None:
    _write(data_dir, unvetted={"ashby": [{"slug": "forward"}]})

    assert tc.append_unvetted("ashby", ["Forward", "newco"]) == 1
    assert _unvetted_slugs(data_dir, "ashby") == ["forward", "newco"]


def test_append_skips_a_case_variant_of_a_known_slug(data_dir) -> None:
    _write(data_dir, known={"lever": [{"slug": "spotify"}]})

    assert tc.append_unvetted("lever", ["Spotify"]) == 0


def test_append_skips_a_case_variant_of_a_blocklisted_slug(data_dir) -> None:
    _write(data_dir, blocked=[_block("ashby", "forward")])

    assert tc.append_unvetted("ashby", ["Forward", "newco"]) == 1
    assert _unvetted_slugs(data_dir, "ashby") == ["newco"]


def test_append_adds_one_of_two_case_variants_in_a_batch(data_dir) -> None:
    _write(data_dir)

    assert tc.append_unvetted("lever", ["BestEgg", "bestegg"]) == 1
    assert _unvetted_slugs(data_dir, "lever") == ["BestEgg"]


# ---------------------------------------------------------------------------
# CompanyEntry.name
# ---------------------------------------------------------------------------
def test_name_is_optional() -> None:
    assert tc.CompanyEntry(slug="ramp").name is None


def test_name_round_trips_through_the_loader(data_dir) -> None:
    _write(
        data_dir,
        known={
            "ashby": [
                {"slug": "notion", "name": "Notion", "added": "2026-06-01"},
                {"slug": "ramp"},
            ]
        },
    )

    entries = tc.load_known()["ashby"]

    assert [(e.slug, e.name) for e in entries] == [("notion", "Notion"), ("ramp", None)]
    assert entries[0].model_dump(mode="json")["name"] == "Notion"
