# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``tools.ats.workable``: the widget API's jobs mapped onto ``Job``.

Fixtures are shaped exactly like the public widget response (every key a
real job carries), with made-up companies. No real HTTP: every client is
forced onto an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from tools.ats import _http
from tools.ats.workable import BASE, build_location, fetch_workable_jobs

_REAL_CLIENT_INIT = httpx.AsyncClient.__init__


def _raw_job(shortcode: str = "1A2B3C4D5E", **over) -> dict:
    job = {
        "application_url": f"https://apply.workable.com/j/{shortcode}/apply",
        "city": "",
        "code": "",
        "country": "United States",
        "created_at": "2026-09-20",
        "department": "Engineering",
        "education": "",
        "employment_type": "Full-time",
        "experience": "Mid-Senior level",
        "function": "Engineering",
        "industry": "Computer Software",
        "locations": [
            {
                "country": "United States",
                "countryCode": "US",
                "city": "",
                "region": "",
                "hidden": False,
            }
        ],
        "published_on": "2026-09-21",
        "shortcode": shortcode,
        "shortlink": f"https://apply.workable.com/j/{shortcode}",
        "state": "",
        "telecommuting": True,
        "title": "Staff Software Engineer",
        "url": f"https://apply.workable.com/j/{shortcode}",
        "description": (
            "<p><strong>About Acme</strong></p>"
            "<ul><li>Build the platform</li><li>Ship weekly</li></ul>"
        ),
    }
    job.update(over)
    return job


def _board(*jobs: dict) -> dict:
    return {
        "name": "Acme Inc",
        "description": "<p>We build things.</p>",
        "jobs": list(jobs),
    }


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

    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        _REAL_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return seen


def _fetch(monkeypatch, body, slug: str = "acme", status: int = 200):
    seen = _route(monkeypatch, lambda r: httpx.Response(status, json=body))
    return asyncio.run(fetch_workable_jobs(slug, "u1")), seen


# ------------------------------------------------------------ the mapping


def test_a_job_maps_every_field(monkeypatch) -> None:
    (job,), seen = _fetch(monkeypatch, _board(_raw_job()))

    assert seen == [f"{BASE}/acme?details=true"]
    assert job.id == hashlib.sha256(b"workable:1A2B3C4D5E").hexdigest()[:16]
    assert job.user_id == "u1"
    assert job.source == "workable"
    assert job.source_id == "1A2B3C4D5E"
    assert job.company == "acme"
    assert job.title == "Staff Software Engineer"
    assert job.url == "https://apply.workable.com/j/1A2B3C4D5E"
    assert job.location == "Remote - United States"
    assert job.discovered_at.tzinfo is not None


def test_the_html_description_becomes_plain_jd_raw(monkeypatch) -> None:
    (job,), _ = _fetch(monkeypatch, _board(_raw_job()))

    assert "<" not in job.jd_raw
    assert "About Acme" in job.jd_raw
    assert "Build the platform" in job.jd_raw
    assert "Ship weekly" in job.jd_raw


def test_a_job_without_a_description_has_an_empty_jd(monkeypatch) -> None:
    raw = _raw_job()
    del raw["description"]
    (job,), _ = _fetch(monkeypatch, _board(raw))
    assert job.jd_raw == ""


def test_the_job_id_is_stable_across_fetches_users_and_slugs(monkeypatch) -> None:
    (first,), _ = _fetch(monkeypatch, _board(_raw_job()))
    (again,), _ = _fetch(monkeypatch, _board(_raw_job(title="Renamed")), slug="other")
    (other,), _ = _fetch(monkeypatch, _board(_raw_job("ZZZZZZZZZZ")))

    assert first.id == again.id
    assert first.id != other.id
    assert len(first.id) == 16


def test_url_falls_back_to_the_shortlink(monkeypatch) -> None:
    (job,), _ = _fetch(monkeypatch, _board(_raw_job(url="")))
    assert job.url == "https://apply.workable.com/j/1A2B3C4D5E"

    raw = _raw_job(url=None)
    del raw["shortlink"]
    (job,), _ = _fetch(monkeypatch, _board(raw))
    assert job.url == "https://apply.workable.com/j/1A2B3C4D5E"


# --------------------------------------------------------------- location


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"country": "United States", "telecommuting": True}, "Remote - United States"),
        (
            {"city": "Austin", "state": "Texas", "country": "United States"},
            "Austin, Texas, United States",
        ),
        (
            {"city": "", "state": "Texas", "country": "United States"},
            "Texas, United States",
        ),
        (
            {"city": "London", "country": "United Kingdom", "telecommuting": True},
            "Remote - London, United Kingdom",
        ),
        ({"telecommuting": True}, "Remote"),
        ({"city": "  ", "state": None, "country": ""}, None),
        ({"telecommuting": False}, None),
        ({}, None),
    ],
)
def test_build_location(fields, expected) -> None:
    raw = {"city": "", "state": "", "country": "", "telecommuting": False, **fields}
    assert build_location(raw) == expected


def test_an_onsite_job_has_no_remote_marker(monkeypatch) -> None:
    raw = _raw_job(city="Berlin", state="", country="Germany", telecommuting=False)
    (job,), _ = _fetch(monkeypatch, _board(raw))
    assert job.location == "Berlin, Germany"


# --------------------------------------------------------- malformed input


def test_malformed_jobs_are_skipped_and_the_rest_kept(monkeypatch) -> None:
    no_code = _raw_job()
    del no_code["shortcode"]
    body = _board(
        no_code,
        _raw_job(shortcode=""),
        "not a job",
        None,
        _raw_job("GOODGOOD01"),
    )

    jobs, _ = _fetch(monkeypatch, body)

    assert [j.source_id for j in jobs] == ["GOODGOOD01"]


@pytest.mark.parametrize("body", [{"name": "Acme Inc"}, {"jobs": None}, [], "junk"])
def test_a_body_without_a_jobs_list_is_empty(monkeypatch, body) -> None:
    jobs, _ = _fetch(monkeypatch, body)
    assert jobs == []


def test_a_missing_account_is_empty_and_classified_not_found(monkeypatch) -> None:
    seen = _route(monkeypatch, lambda r: httpx.Response(404, json={}))

    async def go():
        with _http.capture_outcome() as slot:
            jobs = await fetch_workable_jobs("nobody", "u1")
        return jobs, slot

    jobs, slot = asyncio.run(go())

    assert jobs == []
    assert (slot.outcome, slot.status) == (_http.NOT_FOUND, 404)
    assert len(seen) == 1, "a 404 is never retried"
