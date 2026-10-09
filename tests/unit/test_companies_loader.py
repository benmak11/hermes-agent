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

    assert tc.append_unvetted("ashby", [("Forward", None), ("newco", None)]) == 1
    assert _unvetted_slugs(data_dir, "ashby") == ["forward", "newco"]


def test_append_skips_a_case_variant_of_a_known_slug(data_dir) -> None:
    _write(data_dir, known={"lever": [{"slug": "spotify"}]})

    assert tc.append_unvetted("lever", [("Spotify", "Spotify")]) == 0


def test_append_skips_a_case_variant_of_a_blocklisted_slug(data_dir) -> None:
    _write(data_dir, blocked=[_block("ashby", "forward")])

    assert tc.append_unvetted("ashby", [("Forward", None), ("newco", None)]) == 1
    assert _unvetted_slugs(data_dir, "ashby") == ["newco"]


def test_append_adds_one_of_two_case_variants_in_a_batch(data_dir) -> None:
    _write(data_dir)

    assert tc.append_unvetted("lever", [("BestEgg", None), ("bestegg", None)]) == 1
    assert _unvetted_slugs(data_dir, "lever") == ["BestEgg"]


def test_append_writes_the_name_only_when_known(data_dir) -> None:
    _write(data_dir)

    assert tc.append_unvetted("ashby", [("acme", "Acme"), ("globex", None)]) == 2

    raw = yaml.safe_load((data_dir / "unvetted.yaml").read_text())
    acme, globex = raw["ashby"]
    assert list(acme) == ["slug", "name", "added"]
    assert acme["name"] == "Acme"
    assert list(globex) == ["slug", "added"]


def test_append_keeps_the_name_of_the_spelling_it_keeps(data_dir) -> None:
    _write(data_dir, known={"lever": [{"slug": "globex"}]})

    added = tc.append_unvetted(
        "lever", [("Acme", "Acme"), ("acme", "Other"), ("Globex", "Globex Corp")]
    )

    assert added == 1
    raw = yaml.safe_load((data_dir / "unvetted.yaml").read_text())
    assert [(c["slug"], c["name"]) for c in raw["lever"]] == [("Acme", "Acme")]


def test_new_unvetted_slugs_matches_what_append_adds(data_dir) -> None:
    _write(
        data_dir,
        known={"lever": [{"slug": "spotify"}]},
        unvetted={"lever": [{"slug": "forward"}]},
        blocked=[_block("lever", "deadco")],
    )
    batch = ["Spotify", "FORWARD", "DeadCo", "acme", "Acme", "globex"]

    assert tc.new_unvetted_slugs("lever", batch) == ["acme", "globex"]
    assert tc.append_unvetted("lever", [(s, None) for s in batch]) == 2


# ---------------------------------------------------------------------------
# fill_missing_names
# ---------------------------------------------------------------------------
_KNOWN_TEXT = """# Hand-written header comment.
greenhouse:
  - slug: acme
    added: "2026-06-01"
    notes: "keeps its quotes"
  - slug: globex
    name: "Globex Corp"
    added: "2026-06-01"

# A comment between sections.
ashby:
  - slug: Initech  # trailing comment
    paused: true
"""


def test_fill_inserts_names_and_keeps_comments_quotes_and_order(data_dir) -> None:
    (data_dir / "known.yaml").write_text(_KNOWN_TEXT)

    n = tc.fill_missing_names(
        "known.yaml",
        {("greenhouse", "acme"): "Acme & Co", ("ashby", "Initech"): "Initech"},
    )

    assert n == 2
    assert (data_dir / "known.yaml").read_text() == _KNOWN_TEXT.replace(
        "  - slug: acme\n", "  - slug: acme\n    name: Acme & Co\n"
    ).replace(
        "  - slug: Initech  # trailing comment\n",
        "  - slug: Initech  # trailing comment\n    name: Initech\n",
    )


def test_fill_never_overwrites_an_existing_name(data_dir) -> None:
    (data_dir / "known.yaml").write_text(_KNOWN_TEXT)

    n = tc.fill_missing_names(
        "known.yaml", {("greenhouse", "globex"): "Something Else"}
    )

    assert n == 0
    assert (data_dir / "known.yaml").read_text() == _KNOWN_TEXT


def test_fill_matches_the_slug_exactly_on_its_platform(data_dir) -> None:
    (data_dir / "known.yaml").write_text(_KNOWN_TEXT)

    n = tc.fill_missing_names(
        "known.yaml",
        {("greenhouse", "Acme"): "Wrong case", ("ashby", "acme"): "Wrong platform"},
    )

    assert n == 0
    assert (data_dir / "known.yaml").read_text() == _KNOWN_TEXT


def test_fill_on_a_dumped_file_matches_a_safe_dump_round_trip(data_dir) -> None:
    """unvetted.yaml is machine-written; the insertion must equal re-dumping it."""
    _write(data_dir)
    tc.append_unvetted("lever", [("acme", None), ("globex", "Globex")])
    tc.append_unvetted("ashby", [("initech", None)])

    tc.fill_missing_names(
        "unvetted.yaml",
        {("lever", "acme"): "Acme: The Company", ("ashby", "initech"): "Ïnitech"},
    )

    text = (data_dir / "unvetted.yaml").read_text()
    raw = yaml.safe_load(text)
    assert raw["lever"][0]["name"] == "Acme: The Company"
    assert raw["ashby"][0]["name"] == "Ïnitech"
    assert yaml.safe_dump(raw, sort_keys=False, allow_unicode=True) == text


def test_fill_refuses_a_layout_it_cannot_edit_and_writes_nothing(data_dir) -> None:
    text = "greenhouse:\n  - {slug: acme, added: '2026-06-01'}\n"
    (data_dir / "known.yaml").write_text(text)

    with pytest.raises(RuntimeError):
        tc.fill_missing_names("known.yaml", {("greenhouse", "acme"): "Acme"})

    assert (data_dir / "known.yaml").read_text() == text


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
