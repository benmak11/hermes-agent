# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.board_names`` — dry run by default; ``--write`` fills only missing names.

The probe is faked at ``cli.board_names.probe_board``, so no HTTP happens, and
the YAML lives in a temp ``DATA_DIR``.
"""

from __future__ import annotations

import pytest
import yaml

import cli.board_names as bn
import tools.companies as tc
from tools.ats.probe import BoardProbe

KNOWN = """# header comment
greenhouse:
  - slug: acme
    added: "2026-06-01"
  - slug: globex
    name: "Globex Corp"
    added: "2026-06-01"

lever:
  - slug: deadco
    added: "2026-06-01"

google_jobs:
  - slug: software engineer
"""

UNVETTED = {
    "ashby": [
        {"slug": "initech", "added": "2026-06-20"},
        {"slug": "nameless", "added": "2026-06-20"},
        {"slug": "blockedco", "added": "2026-06-20"},
    ]
}

BLOCKLIST = {
    "blocked": [
        {
            "platform": "ashby",
            "slug": "BlockedCo",
            "blocked_at": "2026-10-01",
            "reason": "test",
        }
    ]
}

ANSWERS = {
    ("greenhouse", "acme"): BoardProbe("ok", 200, 3, "Acme"),
    ("greenhouse", "globex"): BoardProbe("ok", 200, 1, "Globex Corporation"),
    ("lever", "deadco"): BoardProbe("not_found", 404, None, None),
    ("ashby", "initech"): BoardProbe("ok", 200, 0, "Initech"),
    ("ashby", "nameless"): BoardProbe("ok", 200, 5, None),
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(KNOWN)
    (d / "unvetted.yaml").write_text(yaml.safe_dump(UNVETTED, sort_keys=False))
    (d / "blocklist.yaml").write_text(yaml.safe_dump(BLOCKLIST, sort_keys=False))
    monkeypatch.setattr(tc, "DATA_DIR", d)

    probed: list[tuple[str, str]] = []

    async def fake_probe(platform: str, slug: str) -> BoardProbe:
        probed.append((platform, slug))
        return ANSWERS[(platform, slug)]

    monkeypatch.setattr(bn, "probe_board", fake_probe)
    return d, probed


def _snapshot(d) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(d.iterdir())}


def test_dry_run_writes_nothing_and_prints_proposals(env, capsys) -> None:
    d, _ = env
    before = _snapshot(d)

    bn.main([])

    assert _snapshot(d) == before
    out = capsys.readouterr().out
    assert "+ greenhouse/acme (known): Acme" in out
    assert "+ ashby/initech (unvetted): Initech" in out
    assert "= greenhouse/globex (known): Globex Corp kept" in out
    assert "board says 'Globex Corporation'" in out
    assert "? ashby/nameless (unvetted)" in out
    assert "Dry run" in out


def test_write_fills_only_missing_names(env) -> None:
    d, _ = env
    blocklist_before = (d / "blocklist.yaml").read_text()

    bn.main(["--write"])

    known = yaml.safe_load((d / "known.yaml").read_text())
    assert [(e["slug"], e.get("name")) for e in known["greenhouse"]] == [
        ("acme", "Acme"),
        ("globex", "Globex Corp"),  # never overwritten
    ]
    assert "name" not in known["lever"][0]  # 404: no name to give
    assert "name" not in known["google_jobs"][0]  # not a board
    assert (d / "known.yaml").read_text().startswith("# header comment\n")

    unvetted = yaml.safe_load((d / "unvetted.yaml").read_text())
    assert [(e["slug"], e.get("name")) for e in unvetted["ashby"]] == [
        ("initech", "Initech"),
        ("nameless", None),
        ("blockedco", None),
    ]
    assert (d / "blocklist.yaml").read_text() == blocklist_before


def test_write_twice_changes_nothing_the_second_time(env) -> None:
    d, _ = env
    bn.main(["--write"])
    after_first = _snapshot(d)

    bn.main(["--write"])

    assert _snapshot(d) == after_first


def test_non_ok_boards_are_reported_not_removed(env, capsys) -> None:
    d, _ = env

    bn.main(["--write"])

    out = capsys.readouterr().out
    assert "✗ lever/deadco (known): not_found 404" in out
    known = yaml.safe_load((d / "known.yaml").read_text())
    assert [e["slug"] for e in known["lever"]] == ["deadco"]


def test_blocklisted_and_search_query_entries_are_never_probed(env) -> None:
    _, probed = env

    bn.main([])

    assert ("ashby", "blockedco") not in probed
    assert all(p in {"greenhouse", "lever", "ashby"} for p, _ in probed)
    assert len(probed) == 5


def test_platform_and_limit_narrow_the_probe(env) -> None:
    _, probed = env

    bn.main(["--platform", "greenhouse", "--limit", "1"])

    assert probed == [("greenhouse", "acme")]


def test_write_reports_counts_per_file(env, capsys) -> None:
    bn.main(["--write"])

    out = capsys.readouterr().out
    assert "Wrote 1 names to known.yaml." in out
    assert "Wrote 1 names to unvetted.yaml." in out
