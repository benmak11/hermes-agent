# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""HN "Who is hiring?" link mining and ``cli.hn_boards``.

The Algolia API is served by an ``httpx.MockTransport`` and the board probe is
faked at ``tools.discovery.dork.probe_board``, so no HTTP leaves the process.
Post text is escaped exactly as Algolia returns it (``/`` as ``&#x2F;``).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import yaml

import cli.hn_boards as hb
import tools.companies as tc
from tools.ats.probe import BoardProbe
from tools.discovery import dork, hn
from tools.discovery.hn import extract_board_slugs


def _esc(text: str) -> str:
    """Escape like Algolia's ``text`` field."""
    return text.replace("/", "&#x2F;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# extract_board_slugs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://jobs.ashbyhq.com/acme", [("ashby", "acme")]),
        (
            "https://jobs.ashbyhq.com/acme/0b6c1a2e-1111-2222-3333-444455556666",
            [("ashby", "acme")],
        ),
        ("https://jobs.ashbyhq.com/Acme%20Labs", [("ashby", "Acme Labs")]),
        ("https://boards.greenhouse.io/globex", [("greenhouse", "globex")]),
        ("https://job-boards.greenhouse.io/globex", [("greenhouse", "globex")]),
        (
            "https://boards.greenhouse.io/globex/jobs/4012345",
            [("greenhouse", "globex")],
        ),
        (
            "https://boards.greenhouse.io/embed/job_board?for=globex",
            [("greenhouse", "globex")],
        ),
        (
            "https://job-boards.greenhouse.io/embed/job_board?b=1&for=globex",
            [("greenhouse", "globex")],
        ),
        ("https://jobs.lever.co/initech", [("lever", "initech")]),
        (
            "https://jobs.lever.co/initech/9f0e8d7c-aaaa-bbbb-cccc-ddddeeeeffff",
            [("lever", "initech")],
        ),
        ("Apply at https://jobs.lever.co/initech.", [("lever", "initech")]),
        ("(https://jobs.ashbyhq.com/acme)", [("ashby", "acme")]),
        ("https://jobs.ashbyhq.com/acme, or email", [("ashby", "acme")]),
        ('"https://jobs.ashbyhq.com/acme"', [("ashby", "acme")]),
        ("https://jobs.lever.co/InitechCo", [("lever", "InitechCo")]),
        ("https://jobs.ashbyhq.com/cal.co", [("ashby", "cal.co")]),
        ("https://apply.workable.com/umbrella/", []),
        ("https://ats.rippling.com/hooli/jobs", []),
        ("https://boards.greenhouse.io/jobs", []),
        ("", []),
    ],
)
def test_extract_board_slugs_table(text, expected) -> None:
    assert extract_board_slugs(_esc(text)) == expected


def test_extract_unescapes_entities_before_matching() -> None:
    text = "Acme (Remote) | Backend Engineer | https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;acme"
    assert extract_board_slugs(text) == [("ashby", "acme")]


def test_extract_collapses_duplicates_in_order_and_reads_anchor_html() -> None:
    text = _esc(
        "Acme | Remote<p>Roles: "
        '<a href="https://jobs.ashbyhq.com/acme/1" rel="nofollow">'
        "https://jobs.ashbyhq.com/acme/1</a> and "
        '<a href="https://boards.greenhouse.io/globex">x</a> '
        "https://jobs.ashbyhq.com/acme"
    )
    assert extract_board_slugs(text) == [("ashby", "acme"), ("greenhouse", "globex")]


# ---------------------------------------------------------------------------
# harvest against a mocked Algolia API
# ---------------------------------------------------------------------------
def _hit(oid: int, title: str, created: str) -> dict:
    return {"objectID": str(oid), "title": title, "created_at": created}


SEARCH = {
    "hits": [
        _hit(103, "Ask HN: Who wants to be hired? (October 2026)", "2026-10-01T15:01Z"),
        _hit(
            102,
            "Ask HN: Freelancer? Seeking freelancer? (October 2026)",
            "2026-10-01T15:01Z",
        ),
        _hit(101, "Ask HN: Who is hiring? (October 2026)", "2026-10-01T15:00Z"),
        _hit(
            93, "Ask HN: Who wants to be hired? (September 2026)", "2026-09-01T15:01Z"
        ),
        _hit(91, "Ask HN: Who is hiring? (September 2026)", "2026-09-01T15:00Z"),
        _hit(81, "Ask HN: Who is hiring? (August 2026)", "2026-08-01T15:00Z"),
    ]
}


def _post(pid: int, text: str, children: list | None = None) -> dict:
    return {
        "author": f"poster{pid}",
        "children": children or [],
        "created_at": "2026-10-01T16:00:00.000Z",
        "created_at_i": 1790870400,
        "id": pid,
        "options": [],
        "parent_id": 101,
        "points": None,
        "story_id": 101,
        "text": _esc(text),
        "title": None,
        "type": "comment",
        "url": None,
    }


def _thread(tid: int, posts: list[dict]) -> dict:
    return {"id": tid, "title": "Ask HN: Who is hiring?", "children": posts}


THREADS = {
    "101": _thread(
        101,
        [
            _post(
                1,
                "Acme (Remote) | Backend Engineer | https://jobs.ashbyhq.com/acme",
                children=[
                    # A nested reply is not a job post: never harvested.
                    _post(11, "Also look at https://jobs.lever.co/replyco"),
                ],
            ),
            _post(2, "Globex | NYC | https://boards.greenhouse.io/globex/jobs/1"),
            _post(3, "Acme again | https://jobs.ashbyhq.com/acme/abc"),
            _post(4, "Known Co | https://jobs.lever.co/known-co"),
            _post(5, "Dead Co | https://jobs.ashbyhq.com/deadco"),
            _post(6, "Umbrella | https://apply.workable.com/umbrella"),
        ],
    ),
    "91": _thread(91, [_post(7, "Initech | https://jobs.lever.co/Initech")]),
    "81": _thread(81, [_post(8, "Hooli | https://jobs.ashbyhq.com/hooli")]),
}


def _client(fail: set[str] | None = None, calls: list | None = None):
    """An AsyncClient answering the Algolia endpoints; ``fail`` paths 500."""
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url)
        assert request.url.host == "hn.algolia.com"
        path = request.url.path
        if path in fail:
            return httpx.Response(500, text="boom")
        if path == "/api/v1/search_by_date":
            return httpx.Response(200, json=SEARCH)
        tid = path.removeprefix("/api/v1/items/")
        if tid in THREADS:
            return httpx.Response(200, json=THREADS[tid])
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _harvest(months: int = 1, **kw) -> hn.Harvest:
    async def go() -> hn.Harvest:
        async with _client(**kw) as client:
            return await hn.harvest(months, client)

    return asyncio.run(go())


def test_latest_thread_only_by_default_and_never_who_wants_to_be_hired() -> None:
    calls: list = []
    result = _harvest(1, calls=calls)

    assert [t.id for t in result.threads] == ["101"]
    assert result.threads[0].title == "Ask HN: Who is hiring? (October 2026)"
    assert result.errors == []
    item_paths = [u.path for u in calls if "/items/" in u.path]
    assert item_paths == ["/api/v1/items/101"]
    search = next(u for u in calls if u.path.endswith("search_by_date"))
    assert search.params["tags"] == "story,author_whoishiring"


def test_months_reads_that_many_hiring_threads_newest_first() -> None:
    result = _harvest(2)

    assert [t.id for t in result.threads] == ["101", "91"]
    assert result.slugs["lever"] == {"known-co": 1, "Initech": 1}
    assert "hooli" not in result.slugs["ashby"]


def test_months_out_of_range_is_refused() -> None:
    with pytest.raises(ValueError):
        asyncio.run(hn.harvest(0))
    with pytest.raises(ValueError):
        asyncio.run(hn.harvest(hn.MAX_MONTHS + 1))
    with pytest.raises(SystemExit):
        hb.main(["--months", "7"])


def test_only_top_level_posts_are_read_and_counted_per_slug() -> None:
    result = _harvest(1)

    assert result.posts == 6
    assert result.slugs == {
        "greenhouse": {"globex": 1},
        "lever": {"known-co": 1},
        "ashby": {"acme": 2, "deadco": 1},
    }
    assert "replyco" not in result.slugs["lever"]


def test_case_variants_collapse_onto_first_spelling() -> None:
    result = hn.Harvest()
    hn._record(result, _esc("https://jobs.lever.co/Initech"))
    hn._record(result, _esc("https://jobs.lever.co/initech"))
    assert result.slugs["lever"] == {"Initech": 2}


def test_failed_search_is_reported_not_raised() -> None:
    result = _harvest(1, fail={"/api/v1/search_by_date"})

    assert result.threads == []
    assert len(result.errors) == 1
    assert "thread search failed" in result.errors[0]


def test_failed_thread_is_reported_and_the_rest_kept() -> None:
    result = _harvest(2, fail={"/api/v1/items/101"})

    assert [t.id for t in result.threads] == ["101", "91"]
    assert len(result.errors) == 1
    assert "(101)" in result.errors[0]
    assert result.slugs["lever"] == {"Initech": 1}
    assert result.posts == 1


# ---------------------------------------------------------------------------
# cli.hn_boards
# ---------------------------------------------------------------------------
ANSWERS = {
    ("ashby", "acme"): BoardProbe("ok", 200, 4, "Acme"),
    ("ashby", "deadco"): BoardProbe("not_found", 404, None, None),
    ("greenhouse", "globex"): BoardProbe("ok", 200, 0, "Globex"),
    ("lever", "Initech"): BoardProbe("ok", 200, 2, None),
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp data dir with ``known-co`` in the pool, a mocked API and probe."""
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(yaml.safe_dump({"lever": [{"slug": "Known-Co"}]}))
    (d / "unvetted.yaml").write_text("{}\n")
    (d / "blocklist.yaml").write_text(yaml.safe_dump({"blocked": []}))
    monkeypatch.setattr(tc, "DATA_DIR", d)

    probed: list[tuple[str, str]] = []

    async def fake_probe(platform: str, slug: str) -> BoardProbe:
        probed.append((platform, slug))
        return ANSWERS[(platform, slug)]

    monkeypatch.setattr(dork, "probe_board", fake_probe)

    async def mocked_harvest(months: int) -> hn.Harvest:
        async with _client() as client:
            return await hn.harvest(months, client)

    monkeypatch.setattr(hb, "harvest", mocked_harvest)
    return d, probed


def _snapshot(d) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(d.iterdir())}


def test_dry_run_writes_nothing_and_prints_outcomes(env, capsys) -> None:
    d, _ = env
    before = _snapshot(d)

    hb.main([])

    assert _snapshot(d) == before
    out = capsys.readouterr().out
    assert "thread 101: Ask HN: Who is hiring? (October 2026)" in out
    assert "6 top-level posts scanned" in out
    assert "already in pool (1): known-co" in out
    assert "+ acme: Acme [2 posts]" in out
    assert "✗ deadco: not_found" in out
    assert "✗ globex: empty" in out
    assert "1 boards to add, 2 rejected." in out
    assert "Dry run" in out


def test_write_appends_only_vetted_boards_with_names(env, capsys) -> None:
    d, _ = env

    hb.main(["--write", "--months", "2"])

    raw = yaml.safe_load((d / "unvetted.yaml").read_text())
    assert [(e["slug"], e.get("name")) for e in raw["ashby"]] == [("acme", "Acme")]
    assert [(e["slug"], e.get("name")) for e in raw["lever"]] == [("Initech", None)]
    assert "greenhouse" not in raw
    assert "Wrote 2 boards to unvetted.yaml." in capsys.readouterr().out


def test_slugs_already_in_pool_are_not_probed(env) -> None:
    _, probed = env

    hb.main([])

    assert ("lever", "known-co") not in probed
    assert sorted(probed) == [
        ("ashby", "acme"),
        ("ashby", "deadco"),
        ("greenhouse", "globex"),
    ]


def test_api_failure_is_printed(env, capsys, monkeypatch) -> None:
    async def failing_harvest(months: int) -> hn.Harvest:
        async with _client(fail={"/api/v1/search_by_date"}) as client:
            return await hn.harvest(months, client)

    monkeypatch.setattr(hb, "harvest", failing_harvest)

    hb.main([])

    out = capsys.readouterr().out
    assert "API failure, results are partial: thread search failed" in out
    assert "0 boards to add, 0 rejected." in out
