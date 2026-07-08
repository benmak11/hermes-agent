# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the Google/Meta careers-site fetchers and Google liveness.

These sites have no clean JSON API: Google embeds job data in an
``AF_initDataCallback`` blob and soft-404s dead postings; Meta is GraphQL
behind an LSD page token. The tests pin the blob extraction, the field
mapping, and the live/removed discrimination those quirks force.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from models.job import Job
from tools.ats.google_jobs import extract_ds, fetch_google_jobs
from tools.ats.meta_jobs import _jd_text, fetch_meta_jobs, job_url
from tools.ats.validate import check_posting


def _google_entry(source_id: str, title: str) -> list:
    """A ds:1 listing entry with only the offsets the fetcher reads."""
    entry = [None] * 21
    entry[0] = source_id
    entry[1] = title
    entry[3] = [None, "<ul><li>Build things.</li></ul>"]
    entry[4] = [None, "<h3>Minimum qualifications:</h3><ul><li>BS degree</li></ul>"]
    entry[7] = "Google"
    entry[9] = [["Austin, TX, USA", ["Austin, TX, USA"], "Austin", None, "TX", "US"]]
    entry[10] = [None, "<p>We write software.</p>"]
    return entry


def _af_page(key: str, data: object) -> str:
    return (
        "<html><script>"
        f"AF_initDataCallback({{key: '{key}', hash: '1', data:{json.dumps(data)}"
        ", sideChannel: {}});</script></html>"
    )


def test_extract_ds_balanced_and_missing() -> None:
    page = _af_page("ds:1", [["a[b]c", '"quoted \\" ]']], )
    assert extract_ds(page, "ds:1") == [["a[b]c", '"quoted \\" ]']]
    assert extract_ds(page, "ds:0") is None


@pytest.mark.asyncio
async def test_fetch_google_jobs_maps_fields(monkeypatch) -> None:
    page = _af_page("ds:1", [[_google_entry("42", "Engineer")], None, "1", "20"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["location"] == "United States"
        return httpx.Response(200, text=page)

    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        _patched_client_init(httpx.MockTransport(handler)),
    )
    jobs = await fetch_google_jobs("software engineer", "me")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "google_jobs"
    assert job.source_id == "42"
    assert job.company == "Google"
    assert job.location == "Austin, TX, USA"
    assert "We write software." in job.jd_raw
    assert "BS degree" in job.jd_raw
    assert job.url.endswith("/jobs/results/42")


@pytest.mark.asyncio
async def test_fetch_google_jobs_fetch_failure_returns_empty(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        _patched_client_init(httpx.MockTransport(handler)),
    )
    assert await fetch_google_jobs("software engineer", "me") == []


def _job(source: str, source_id: str = "1") -> Job:
    return Job(
        id="t",
        user_id="me",
        source=source,
        source_id=source_id,
        company="acme",
        title="Engineer",
        url="https://example.com/job",
        jd_raw="jd",
        discovered_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_google_liveness_live_when_ds0_matches() -> None:
    page = _af_page("ds:0", [["42", "Engineer", "apply-url"]])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=page))
    res = await check_posting(_job("google_jobs", "42"), transport=transport)
    assert res == "live"


@pytest.mark.asyncio
async def test_google_liveness_removed_on_error_blob() -> None:
    # Dead postings still render 200, but ds:0 holds an error structure.
    page = _af_page("ds:0", [5, None, [["ErrorDetails", ["cp.v1.e", 30]]]])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=page))
    res = await check_posting(_job("google_jobs", "42"), transport=transport)
    assert res == "removed"


@pytest.mark.asyncio
async def test_google_liveness_fails_open_without_blob() -> None:
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, text="<html>no blob</html>")
    )
    res = await check_posting(_job("google_jobs", "42"), transport=transport)
    assert res == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_google_liveness_fails_open_on_server_error(status: int) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(status))
    res = await check_posting(_job("google_jobs", "42"), transport=transport)
    assert res == "unknown"


def test_meta_jd_text_handles_html_wrappers_and_strings() -> None:
    # Real shapes: description is a JSON-encoded string, list items are
    # {"item": ...} dicts; {"__html": ...} dicts and bare strings also occur.
    detail = {
        "description": '{"__html":"<p>Build infra.</p>"}',
        "responsibilities": [{"item": "Ship code"}, "Review code"],
        "minimum_qualifications": [{"__html": "5 years experience"}],
        "preferred_qualifications": [],
        "public_compensation": None,
    }
    text = _jd_text(detail)
    assert "Build infra." in text
    assert "- Ship code" in text
    assert "- Review code" in text
    assert "- 5 years experience" in text
    assert "Minimum Qualifications:" in text
    assert "Preferred" not in text


@pytest.mark.asyncio
async def test_fetch_meta_jobs_end_to_end(monkeypatch) -> None:
    search_payload = {
        "data": {
            "job_search_with_featured_jobs": {
                "all_jobs": [
                    {"id": "111", "title": "SWE", "locations": ["Austin, TX"]}
                ]
            }
        }
    }
    detail_payload = {
        "data": {
            "xcp_requisition_job_description": {
                "id": "111",
                "title": "Software Engineer",
                "locations": ["Austin, TX", "Remote, US"],
                "description": {"__html": "<p>Do things.</p>"},
                "responsibilities": [{"__html": "Ship"}],
                "minimum_qualifications": [{"__html": "Code"}],
                "preferred_qualifications": [],
                "public_compensation": [],
            }
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # the /jobsearch/ page with the LSD token
            return httpx.Response(
                200, text='... "LSD",[],{"token":"tok123"} ...'
            )
        body = request.content.decode()
        assert "lsd=tok123" in body
        if "27506805582236862" in body:
            return httpx.Response(200, json=search_payload)
        return httpx.Response(200, json=detail_payload)

    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        _patched_client_init(httpx.MockTransport(handler)),
    )
    jobs = await fetch_meta_jobs("software engineer", "me")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "meta_jobs"
    assert job.source_id == "111"
    assert job.company == "Meta"
    assert job.title == "Software Engineer"
    assert job.location == "Austin, TX; Remote, US"
    assert "Do things." in job.jd_raw
    assert job.url == job_url("111")


@pytest.mark.asyncio
async def test_fetch_meta_jobs_search_failure_returns_empty(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        _patched_client_init(httpx.MockTransport(handler)),
    )
    assert await fetch_meta_jobs("software engineer", "me") == []


def _patched_client_init(transport: httpx.MockTransport):
    """Force every AsyncClient in the module under test onto a mock transport."""
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    return patched
