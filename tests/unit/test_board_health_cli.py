# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.board_health`` and ``tools.companies.move_entries``: a report by
default, ``--write`` moves only ``moved`` boards, and the blocklist is never
written. Firestore is a :class:`FakeStoreDB`; the YAML lives in a temp
``DATA_DIR``."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
import yaml
from firestore_fakes import FakeStoreDB

import cli.board_health as cbh
import cli.eval_scoring as eval_scoring
import tools.companies as tc

KNOWN = """# header comment
greenhouse:
  - slug: acme
    name: "Acme Inc"  # display name
    added: "2026-06-01"
    # why we track it
    notes: "kept"
  # a comment between entries
  - slug: globex
    name: Globex
    added: "2026-06-01"

lever:
  - slug: initech
    name: Initech
    added: "2026-06-01"

google_jobs:
  - slug: software engineer
"""

UNVETTED = """greenhouse:
- slug: hooli
  name: Hooli
  added: '2026-06-20'
- slug: umbrella
  name: Umbrella
  added: '2026-06-20'
ashby:
- slug: soylent
  added: '2026-06-20'
"""

BLOCKLIST = """# keep this comment
blocked:
  - platform: ashby
    slug: spamco
    blocked_at: "2026-05-01"
    reason: "ghost jobs"
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(KNOWN)
    (d / "unvetted.yaml").write_text(UNVETTED)
    (d / "blocklist.yaml").write_text(BLOCKLIST)
    monkeypatch.setattr(tc, "DATA_DIR", d)
    return d


def _snapshot(d) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


# ------------------------------------------------------------ move_entries


def test_move_keeps_comments_name_and_order(data_dir):
    n = tc.move_entries("known.yaml", {("greenhouse", "acme"): ("lever", "acme-co")})
    assert n == 1
    text = (data_dir / "known.yaml").read_text()
    assert (
        text
        == """# header comment
greenhouse:
  # a comment between entries
  - slug: globex
    name: Globex
    added: "2026-06-01"

lever:
  - slug: initech
    name: Initech
    added: "2026-06-01"
  - slug: acme-co
    name: "Acme Inc"  # display name
    added: "2026-06-01"
    # why we track it
    notes: "kept"

google_jobs:
  - slug: software engineer
"""
    )
    lever = {e.slug: e for e in tc.load_known()["lever"]}
    assert lever["acme-co"].name == "Acme Inc"


def test_move_reindents_into_a_section_of_another_style_and_creates_one(data_dir):
    n = tc.move_entries(
        "unvetted.yaml",
        {
            ("greenhouse", "HOOLI"): ("ashby", "hooli"),
            ("greenhouse", "umbrella"): ("lever", "Umbrella"),
        },
    )
    assert n == 2
    raw = yaml.safe_load((data_dir / "unvetted.yaml").read_text())
    assert raw["greenhouse"] == []
    assert raw["ashby"] == [
        {"slug": "soylent", "added": "2026-06-20"},
        {"slug": "hooli", "name": "Hooli", "added": "2026-06-20"},
    ]
    assert raw["lever"] == [
        {"slug": "Umbrella", "name": "Umbrella", "added": "2026-06-20"}
    ]
    assert tc.load_unvetted()["greenhouse"] == [], "an emptied section must still load"


def test_move_into_an_emptied_section(data_dir):
    tc.move_entries("unvetted.yaml", {("greenhouse", "hooli"): ("ashby", "hooli")})
    tc.move_entries(
        "unvetted.yaml", {("greenhouse", "umbrella"): ("ashby", "umbrella")}
    )
    tc.move_entries(
        "unvetted.yaml", {("ashby", "umbrella"): ("greenhouse", "umbrella")}
    )
    raw = yaml.safe_load((data_dir / "unvetted.yaml").read_text())
    assert [e["slug"] for e in raw["greenhouse"]] == ["umbrella"]


def test_move_of_nothing_writes_nothing(data_dir):
    before = _snapshot(data_dir)
    assert tc.move_entries("known.yaml", {("ashby", "nope"): ("lever", "nope")}) == 0
    assert _snapshot(data_dir) == before


def test_a_move_that_does_not_round_trip_is_not_written(data_dir):
    (data_dir / "known.yaml").write_text(
        "greenhouse:\n  - slug: acme\nlever: [{slug: x}]\n"
    )
    before = _snapshot(data_dir)
    with pytest.raises(RuntimeError, match="did not round-trip"):
        tc.move_entries("known.yaml", {("greenhouse", "acme"): ("lever", "acme")})
    assert _snapshot(data_dir) == before


# ------------------------------------------------------------------- CLI


def _rec(platform, slug, state, **over) -> dict:
    rec = {
        "platform": platform,
        "slug": slug,
        "state": state,
        "last_outcome": "not_found",
        "last_status": 404,
        "last_ok_at": None,
        "failing_since": "2026-09-01T00:00:00+00:00",
        "not_found_days": 3,
        "last_not_found_day": "2026-09-20",
        "first_not_found_day": "2026-09-01",
    }
    rec.update(over)
    return rec


def _candidate(platform, slug, name, jobs=4) -> dict:
    return {
        "platform": platform,
        "slug": slug,
        "name": name,
        "job_count": jobs,
        "probed_at": "2026-10-01T00:00:00+00:00",
    }


RECORDS = {
    "greenhouse:acme": _rec(
        "greenhouse",
        "acme",
        "moved",
        resolves_to={"platform": "lever", "slug": "acme-co"},
        candidates=[_candidate("lever", "acme-co", "Acme, Inc.")],
    ),
    "greenhouse:umbrella": _rec(
        "greenhouse",
        "umbrella",
        "moved",
        resolves_to={"platform": "ashby", "slug": "spamco"},
        candidates=[_candidate("ashby", "spamco", "Umbrella")],
    ),
    "greenhouse:hooli": _rec(
        "greenhouse",
        "hooli",
        "moved",
        resolves_to={"platform": "lever", "slug": "initech"},
        candidates=[_candidate("lever", "initech", "Hooli")],
    ),
    "greenhouse:globex": _rec(
        "greenhouse",
        "globex",
        "quarantined",
        candidates=[
            _candidate("lever", "globex", "Globex Labs", 7),
            _candidate("ashby", "globex", None, 1),
        ],
    ),
    "lever:initech": _rec(
        "lever", "initech", "dead", next_check_at="2026-10-15T00:00:00+00:00"
    ),
    "ashby:soylent": _rec(
        "ashby",
        "soylent",
        "failing",
        not_found_days=1,
        last_outcome="timeout",
        last_status=None,
    ),
    "ashby:fine": _rec("ashby", "fine", "ok", not_found_days=0),
}


def _run(data_dir, *, write):
    db = FakeStoreDB({"board_health": {k: dict(v) for k, v in RECORDS.items()}})
    report = asyncio.run(cbh.run(write=write, db=db, today=date(2026, 10, 8)))
    return report, db


def test_the_report_prints_every_problem_and_writes_nothing(data_dir, capsys):
    before = _snapshot(data_dir)
    report, db = _run(data_dir, write=False)
    out = capsys.readouterr().out
    assert _snapshot(data_dir) == before
    assert db.writes == [] and db.deletes == []
    assert [m.slug for m in report.moved] == ["acme", "hooli", "umbrella"]
    assert "greenhouse/acme → lever/acme-co  ('Acme Inc' = 'Acme, Inc.')" in out
    assert "? greenhouse/globex" in out
    assert "lever/globex: 'Globex Labs', 7 jobs" in out
    assert "ashby/globex: None, 1 jobs" in out
    assert "✗ lever/initech  (404 on 3 days, 2026-09-01 to 2026-09-20" in out
    assert "! ashby/soylent: timeout, 1 404 days" in out
    assert "ashby/fine" not in out
    assert "Report only — nothing written" in out


def test_dead_boards_are_printed_blocklist_suggestions_only(data_dir, capsys):
    _run(data_dir, write=True)
    out = capsys.readouterr().out
    suggestion = (
        "  - platform: lever\n"
        "    slug: initech\n"
        '    blocked_at: "2026-10-08"\n'
        '    reason: "404 on 3 days, 2026-09-01 to 2026-09-20; no board on another platform"'
    )
    assert suggestion in out
    assert (data_dir / "blocklist.yaml").read_text() == BLOCKLIST
    assert yaml.safe_load(suggestion) == [
        {
            "platform": "lever",
            "slug": "initech",
            "blocked_at": "2026-10-08",
            "reason": "404 on 3 days, 2026-09-01 to 2026-09-20; no board on another platform",
        }
    ]


def test_write_moves_only_moved_boards(data_dir, capsys):
    report, db = _run(data_dir, write=True)
    out = capsys.readouterr().out
    assert db.writes == []
    assert report.written == {"known.yaml": 1, "unvetted.yaml": 0}
    known = tc.load_known()
    assert [e.slug for e in known["greenhouse"]] == ["globex"], "quarantined stays"
    assert [(e.slug, e.name) for e in known["lever"]] == [
        ("initech", "Initech"),
        ("acme-co", "Acme Inc"),
    ], "the dead board stays and the moved one keeps its name"
    text = (data_dir / "known.yaml").read_text()
    assert "# why we track it" in text and "# a comment between entries" in text
    assert "# header comment" in text
    # A blocklisted target and an already-listed one are skipped, not moved.
    unvetted = tc.load_unvetted()
    assert [e.slug for e in unvetted["greenhouse"]] == ["hooli", "umbrella"]
    assert "greenhouse/umbrella → ashby/spamco: target is blocklisted" in out
    assert "greenhouse/hooli → lever/initech: target is already listed" in out
    assert (data_dir / "blocklist.yaml").read_text() == BLOCKLIST
    assert "Moved 1 entries in known.yaml." in out


def test_the_client_is_pinned_to_the_project(monkeypatch):
    built = {}

    def fake_client(*, project):
        built["project"] = project
        return FakeStoreDB()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-test")
    monkeypatch.setattr(eval_scoring.firestore, "AsyncClient", fake_client)
    cbh.firestore_client()
    assert built == {"project": "proj-test"}
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", " ")
    with pytest.raises(AssertionError):
        cbh.firestore_client()
