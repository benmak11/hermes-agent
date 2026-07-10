# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the posting-liveness check (tools/ats/validate.py).

The load-bearing property is the fail-open contract: only a definitive
404/410 (or verified absence from a fetched Ashby board) may return
``removed`` — transient failures must come back ``unknown``, never dismissing
an application over a flaky board.
"""

from datetime import UTC, datetime

import httpx
import pytest

from models.job import Job
from tools.ats.validate import check_posting


def _job(source: str) -> Job:
    return Job(
        id="t",
        user_id="me",
        source=source,
        source_id="1",
        company="acme",
        title="Engineer",
        url="https://example.com/job",
        jd_raw="jd",
        discovered_at=datetime.now(UTC),
    )


def _transport(
    status: int, json_body: object = None, seen: list[str] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, json=json_body if json_body is not None else {})

    return httpx.MockTransport(handler)


def _timeout_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected_url"),
    [
        ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"),
        ("lever", "https://api.lever.co/v0/postings/acme/1"),
        ("manual", "https://example.com/job"),
        # meta_jobs relies on the plain URL probe: dead postings 301 to a
        # real 404 page, so no special casing is needed (or wanted).
        ("meta_jobs", "https://example.com/job"),
    ],
)
async def test_live_posting_and_probe_url(source: str, expected_url: str) -> None:
    seen: list[str] = []
    res = await check_posting(_job(source), transport=_transport(200, seen=seen))
    assert res == "live"
    assert seen == [expected_url]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["greenhouse", "lever", "manual", "meta_jobs"])
@pytest.mark.parametrize("status", [404, 410])
async def test_gone_status_means_removed(source: str, status: int) -> None:
    res = await check_posting(_job(source), transport=_transport(status))
    assert res == "removed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["greenhouse", "lever", "ashby", "manual", "meta_jobs"]
)
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_errors_fail_open(source: str, status: int) -> None:
    res = await check_posting(_job(source), transport=_transport(status))
    assert res == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["greenhouse", "ashby"])
async def test_transport_errors_fail_open(source: str) -> None:
    res = await check_posting(_job(source), transport=_timeout_transport())
    assert res == "unknown"


@pytest.mark.asyncio
async def test_ashby_posting_still_on_board() -> None:
    board = {"jobs": [{"id": "1"}, {"id": "2"}]}
    res = await check_posting(_job("ashby"), transport=_transport(200, board))
    assert res == "live"


@pytest.mark.asyncio
async def test_ashby_posting_absent_from_board() -> None:
    board = {"jobs": [{"id": "2"}]}
    res = await check_posting(_job("ashby"), transport=_transport(200, board))
    assert res == "removed"


@pytest.mark.asyncio
async def test_ashby_board_gone_means_removed() -> None:
    res = await check_posting(_job("ashby"), transport=_transport(404))
    assert res == "removed"


@pytest.mark.asyncio
async def test_ashby_unparseable_board_fails_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    res = await check_posting(_job("ashby"), transport=httpx.MockTransport(handler))
    assert res == "unknown"
