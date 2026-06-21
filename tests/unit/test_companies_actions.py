# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for company-management mutations (isolated tmp data dir)."""

import pytest
import yaml

import tools.companies as tc


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(
        yaml.safe_dump(
            {
                "greenhouse": [{"slug": "stripe", "added": "2026-06-01"}],
                "lever": [],
                "ashby": [],
            }
        )
    )
    (d / "unvetted.yaml").write_text(
        yaml.safe_dump(
            {
                "greenhouse": [{"slug": "newco", "added": "2026-06-20"}],
                "lever": [],
                "ashby": [],
            }
        )
    )
    (d / "blocklist.yaml").write_text(yaml.safe_dump({"blocked": []}))
    monkeypatch.setattr(tc, "DATA_DIR", d)
    return d


def test_promote_moves_unvetted_to_known(data_dir) -> None:
    tc.promote_to_known("greenhouse", "newco")
    known = {c.slug for c in tc.load_known()["greenhouse"]}
    unvetted = {c.slug for c in tc.load_unvetted()["greenhouse"]}
    assert "newco" in known
    assert "newco" not in unvetted


def test_block_adds_to_blocklist_and_removes(data_dir) -> None:
    tc.block_company("greenhouse", "stripe", "onsite only")
    assert ("greenhouse", "stripe") in tc.load_blocklist()
    assert "stripe" not in {c.slug for c in tc.load_known()["greenhouse"]}


def test_dismiss_removes_without_blocking(data_dir) -> None:
    tc.dismiss_unvetted("greenhouse", "newco")
    assert "newco" not in {c.slug for c in tc.load_unvetted()["greenhouse"]}
    assert ("greenhouse", "newco") not in tc.load_blocklist()


def test_pause_excludes_from_active(data_dir) -> None:
    tc.set_paused("greenhouse", "stripe", True)
    active = {slug for _, slug, _ in tc.all_active_companies()}
    assert "stripe" not in active  # paused known company skipped
    assert "newco" in active  # unvetted still fetched


def test_apply_company_action_dispatch(data_dir) -> None:
    tc.apply_company_action("greenhouse", "newco", "promote")
    assert "newco" in {c.slug for c in tc.load_known()["greenhouse"]}
    with pytest.raises(ValueError):
        tc.apply_company_action("greenhouse", "x", "bogus")
