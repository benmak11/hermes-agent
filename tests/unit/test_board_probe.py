# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``tools.ats.probe``: the board outcome, its job count and the company name.

The name fixtures are shaped like the real public responses: Greenhouse's
board JSON, and the ``<title>`` of Lever's and Ashby's hosted board pages.
No real HTTP: every client is forced onto an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tools.ats import _http
from tools.ats import probe as pr

# ----------------------------------------------------------------- fixtures

GREENHOUSE_BOARD = '{"name":"Stripe","content":""}'
LEVER_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<title>Spotify</title></head><body>...</body></html>"
)
ASHBY_PAGE = (
    '<!DOCTYPE html><html lang="en"><head>'
    "<title>Linear Jobs</title><meta name='robots'></head><body></body></html>"
)


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    monkeypatch.setattr(_http, "_RETRY_INITIAL_WAIT", 0)
    monkeypatch.setattr(_http, "_RETRY_MAX_WAIT", 0)


def _route(monkeypatch, handler) -> list[str]:
    """Force every AsyncClient onto ``handler``; return the URLs requested."""
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return seen


def _probe(platform: str, slug: str = "acme") -> pr.BoardProbe:
    return asyncio.run(pr.probe_board(platform, slug))


# ------------------------------------------------------------ name parsing


def test_greenhouse_name_is_the_name_field() -> None:
    assert pr.parse_greenhouse_name(json.loads(GREENHOUSE_BOARD)) == "Stripe"


@pytest.mark.parametrize(
    "data",
    [{"content": ""}, {"name": ""}, {"name": None}, {"name": "  "}, [], "Stripe"],
)
def test_greenhouse_json_without_a_usable_name_is_none(data) -> None:
    assert pr.parse_greenhouse_name(data) is None


def test_lever_name_is_the_page_title() -> None:
    assert pr.parse_lever_name(LEVER_PAGE) == "Spotify"


def test_ashby_name_drops_the_jobs_suffix() -> None:
    assert pr.parse_ashby_name(ASHBY_PAGE) == "Linear"


@pytest.mark.parametrize(
    ("page", "lever", "ashby"),
    [
        ("<title>Acme &amp; Sons</title>", "Acme & Sons", "Acme & Sons"),
        ("<title>Acme &amp; Sons Jobs</title>", "Acme & Sons Jobs", "Acme & Sons"),
        ("<TITLE>\n   Globex\n   Corp  </TITLE>", "Globex Corp", "Globex Corp"),
        (
            "<title data-rh='true'>Globex Corp Jobs</title>",
            "Globex Corp Jobs",
            "Globex Corp",
        ),
        ("<title>Initech&#39;s Jobs</title>", "Initech's Jobs", "Initech's"),
    ],
)
def test_titles_are_unescaped_and_whitespace_collapsed(page, lever, ashby) -> None:
    assert pr.parse_lever_name(page) == lever
    assert pr.parse_ashby_name(page) == ashby


@pytest.mark.parametrize(
    "page",
    [
        "<html><head></head></html>",
        "",
        "<title></title>",
        "<title>   </title>",
        "<title>Jobs</title>",
        "<title>Lever</title>",
        "<title>Ashby</title>",
        "<title>Careers</title>",
        "<title> Jobs </title>",
    ],
)
def test_missing_or_generic_titles_are_no_name(page) -> None:
    assert pr.parse_lever_name(page) is None
    assert pr.parse_ashby_name(page) is None


def test_an_ashby_title_that_is_only_a_generic_word_plus_jobs_is_no_name() -> None:
    assert pr.parse_ashby_name("<title>Ashby Jobs</title>") is None


# ----------------------------------------------------------- probe outcomes


def _board(platform: str, jobs_response, name_response):
    """A handler answering the jobs API and the name source differently."""
    jobs_hosts = {
        "greenhouse": "/jobs",
        "lever": "api.lever.co",
        "ashby": "api.ashbyhq.com",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if jobs_hosts[platform] in url:
            return jobs_response(request)
        return name_response(request)

    return handler


@pytest.mark.parametrize(
    ("platform", "jobs", "name_page", "count", "name"),
    [
        (
            "greenhouse",
            {"jobs": [{"id": 1}, {"id": 2}]},
            httpx.Response(200, text=GREENHOUSE_BOARD),
            2,
            "Stripe",
        ),
        ("lever", [{"id": "a"}], httpx.Response(200, text=LEVER_PAGE), 1, "Spotify"),
        (
            "ashby",
            {"jobs": [{"id": "x"}] * 3, "apiVersion": "1"},
            httpx.Response(200, text=ASHBY_PAGE),
            3,
            "Linear",
        ),
    ],
)
def test_an_ok_board_with_jobs(monkeypatch, platform, jobs, name_page, count, name):
    seen = _route(
        monkeypatch,
        _board(platform, lambda r: httpx.Response(200, json=jobs), lambda r: name_page),
    )

    assert _probe(platform) == pr.BoardProbe(_http.OK, 200, count, name)
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("platform", "jobs", "expected"),
    [
        ("greenhouse", {"jobs": []}, "https://boards-api.greenhouse.io/v1/boards/acme"),
        ("lever", [], "https://jobs.lever.co/acme"),
        ("ashby", {"jobs": []}, "https://jobs.ashbyhq.com/acme"),
    ],
)
def test_the_name_sources_are_the_documented_urls(
    monkeypatch, platform, jobs, expected
) -> None:
    seen = _route(
        monkeypatch,
        _board(
            platform,
            lambda r: httpx.Response(200, json=jobs),
            lambda r: httpx.Response(200, text="{}"),
        ),
    )

    _probe(platform)

    assert seen[-1] == expected


def test_an_ok_empty_board_still_gets_its_name(monkeypatch) -> None:
    _route(
        monkeypatch,
        _board(
            "greenhouse",
            lambda r: httpx.Response(200, json={"jobs": []}),
            lambda r: httpx.Response(200, text=GREENHOUSE_BOARD),
        ),
    )

    assert _probe("greenhouse") == pr.BoardProbe(_http.OK, 200, 0, "Stripe")


def test_a_404_board_has_no_count_and_no_name_fetch(monkeypatch) -> None:
    seen = _route(monkeypatch, lambda r: httpx.Response(404))

    assert _probe("lever") == pr.BoardProbe(_http.NOT_FOUND, 404, None, None)
    assert len(seen) == 1


def test_a_429_that_never_recovers_is_rate_limited(monkeypatch) -> None:
    seen = _route(monkeypatch, lambda r: httpx.Response(429))

    assert _probe("ashby") == pr.BoardProbe(_http.RATE_LIMITED, 429, None, None)
    assert len(seen) == _http._RETRY_ATTEMPTS


def test_a_timeout_is_a_timeout_not_an_exception(monkeypatch) -> None:
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    _route(monkeypatch, handler)

    assert _probe("greenhouse") == pr.BoardProbe(_http.TIMEOUT, None, None, None)


def test_a_body_that_is_not_json_is_an_error(monkeypatch) -> None:
    _route(monkeypatch, lambda r: httpx.Response(200, text="<html>nope</html>"))

    assert _probe("lever") == pr.BoardProbe(_http.ERROR, None, None, None)


def test_an_unexpected_jobs_shape_has_no_count(monkeypatch) -> None:
    _route(
        monkeypatch,
        _board(
            "lever",
            lambda r: httpx.Response(200, json={"error": "x"}),
            lambda r: httpx.Response(200, text=LEVER_PAGE),
        ),
    )

    assert _probe("lever").job_count is None


@pytest.mark.parametrize(
    "name_response",
    [
        lambda r: httpx.Response(404),
        lambda r: httpx.Response(200, text="not json"),
    ],
)
def test_a_failed_name_fetch_keeps_the_ok_outcome(monkeypatch, name_response) -> None:
    _route(
        monkeypatch,
        _board(
            "greenhouse",
            lambda r: httpx.Response(200, json={"jobs": [{}]}),
            name_response,
        ),
    )

    assert _probe("greenhouse") == pr.BoardProbe(_http.OK, 200, 1, None)


def test_a_name_fetch_timeout_keeps_the_ok_outcome(monkeypatch) -> None:
    def name(request):
        raise httpx.ConnectTimeout("slow", request=request)

    _route(
        monkeypatch,
        _board("ashby", lambda r: httpx.Response(200, json={"jobs": [{}]}), name),
    )

    assert _probe("ashby") == pr.BoardProbe(_http.OK, 200, 1, None)


@pytest.mark.parametrize("platform", ["google_jobs", "meta_jobs", "workday", ""])
def test_other_platforms_are_refused(platform) -> None:
    with pytest.raises(ValueError):
        _probe(platform)


def test_a_scope_lends_both_gets_one_client(monkeypatch) -> None:
    built = [0]
    transport = httpx.MockTransport(
        _board(
            "greenhouse",
            lambda r: httpx.Response(200, json={"jobs": [{}]}),
            lambda r: httpx.Response(200, text=GREENHOUSE_BOARD),
        )
    )
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        built[0] += 1
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)

    async def main():
        async with _http.board_client():
            return [await pr.probe_board("greenhouse", s) for s in ("acme", "globex")]

    assert [p.name for p in asyncio.run(main())] == ["Stripe", "Stripe"]
    assert built[0] == 1
