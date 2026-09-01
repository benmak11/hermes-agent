# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""What the Greenhouse submitter decides *before* it touches a form.

The bail reasons are not interchangeable labels — each one sends a different
fix at a different problem:

- ``fetch_blocked``  transient; the ATS refused us. Retry later.
- ``posting_gone``   terminal; the listing is down.
- ``custom_wrapper`` structural; this employer needs a different code path.
- ``captcha``        needs a human.

They were previously distinguishable only by accident. An error page has no
email field and no file input, so **every** non-200 response fell through to
the custom-wrapper branch and was reported as a permanent structural verdict.
A 50-posting measurement on 2026-09-01 tripped Greenhouse's bot detection and
produced 48 false ``custom_wrapper`` bails from HTTP 406s — a clean run, an
entirely wrong conclusion, and no test that would have caught it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.submitters.greenhouse as gh
from models.job import Job
from models.profile import MasterProfile

# --------------------------------------------------------------------------
# A Playwright stand-in: only the surface the submitter reaches before filling.
# --------------------------------------------------------------------------


class _Locator:
    def __init__(self, n: int = 0) -> None:
        self._n = n
        self.first = self

    async def count(self) -> int:
        return self._n

    async def wait_for(self, **_kw) -> None:
        if not self._n:
            raise gh.PlaywrightTimeout("not attached")

    async def click(self, **_kw) -> None:  # pragma: no cover - not reached here
        raise AssertionError("a bail must not click anything")


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status


class _Page:
    """Serves one status and one DOM shape; records the screenshot path."""

    def __init__(self, status: int | None, *, has_form: bool) -> None:
        self._status = status
        self._has_form = has_form
        self.shots: list[str] = []

    async def goto(self, _url, **_kw):
        return None if self._status is None else _Response(self._status)

    def locator(self, _sel: str) -> _Locator:
        return _Locator(1 if self._has_form else 0)

    async def screenshot(self, path: str, **_kw) -> None:
        self.shots.append(path)

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def inner_text(self, _sel: str) -> str:
        return ""

    async def evaluate(self, _js: str):  # pragma: no cover - not reached here
        return []


class _Browser:
    def __init__(self, page: _Page) -> None:
        self._page = page

    async def new_page(self, **_kw) -> _Page:
        return self._page

    async def close(self) -> None:
        return None


class _PW:
    def __init__(self, page: _Page) -> None:
        self.chromium = self
        self._page = page

    async def launch(self, **_kw) -> _Browser:
        return _Browser(self._page)

    async def __aenter__(self) -> _PW:
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


def _job() -> Job:
    from datetime import UTC, datetime

    return Job(
        id="j1",
        user_id="u1",
        source="greenhouse",
        source_id="1",
        company="acme",
        title="Staff Engineer",
        url="https://job-boards.greenhouse.io/acme/jobs/1",
        jd_raw="Build things.",
        discovered_at=datetime.now(UTC),
    )


def _profile() -> MasterProfile:
    return MasterProfile(
        user_id="u1",
        full_name="A Candidate",
        email="a@b.co",
        location="Austin, TX",
        objective_template="{role} at {company}",
        experience=[],
        education=[],
        skills={},
        preferences={
            "target_role_families": ["engineering"],
            "target_titles": ["Staff Software Engineer"],
            "target_seniorities": ["staff"],
        },
    )


async def _run(monkeypatch, tmp_path: Path, status: int | None, *, has_form: bool):
    page = _Page(status, has_form=has_form)
    monkeypatch.setattr(gh, "async_playwright", lambda: _PW(page))
    monkeypatch.setattr(gh, "_detect_captcha", _no_captcha)
    resume = tmp_path / "r.docx"
    resume.write_bytes(b"x")
    return await gh.submit_greenhouse(_job(), _profile(), resume, dry_run=True), page


async def _no_captcha(_page) -> bool:
    return False


# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 406, 429, 500, 503])
async def test_a_refused_fetch_is_not_a_custom_wrapper(monkeypatch, tmp_path, status):
    """The regression this file exists for. Each of these is the ATS refusing
    us, not the employer using a different form — and each is *transient*,
    where a custom wrapper is permanent."""
    result, _page = await _run(monkeypatch, tmp_path, status, has_form=False)

    assert result["success"] is False
    assert result["status"] == status
    assert "wrapper" not in result["error"].lower()
    assert "transient" in result["error"].lower()
    assert str(status) in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_a_dead_listing_says_so(monkeypatch, tmp_path, status):
    """404/410 is terminal and must read differently from a refusal: retrying
    a gone posting is pointless, retrying a 406 is the whole remedy."""
    result, _page = await _run(monkeypatch, tmp_path, status, has_form=False)

    assert result["status"] == status
    assert "gone" in result["error"].lower()
    assert "transient" not in result["error"].lower()


@pytest.mark.asyncio
async def test_a_200_without_a_form_is_still_a_wrapper(monkeypatch, tmp_path):
    """The branch keeps its original job — a page that really did load and
    really has no Greenhouse form."""
    result, page = await _run(monkeypatch, tmp_path, 200, has_form=False)

    assert result["success"] is False
    assert "no greenhouse application form" in result["error"].lower()
    assert page.shots, "a wrapper bail should leave a screenshot to judge by"


@pytest.mark.asyncio
async def test_a_captcha_wins_over_the_status_code(monkeypatch, tmp_path):
    """A challenge page is often served as 403 and *is* a captcha. The more
    specific, human-actionable reason has to win, or the user is told to wait
    for something that will never clear on its own."""

    async def _yes(_page) -> bool:
        return True

    page = _Page(403, has_form=False)
    monkeypatch.setattr(gh, "async_playwright", lambda: _PW(page))
    monkeypatch.setattr(gh, "_detect_captcha", _yes)
    resume = tmp_path / "r.docx"
    resume.write_bytes(b"x")

    result = await gh.submit_greenhouse(_job(), _profile(), resume, dry_run=True)

    assert "captcha" in result["error"].lower()


@pytest.mark.asyncio
async def test_a_missing_response_is_not_treated_as_failure(monkeypatch, tmp_path):
    """``goto`` returns None for a same-document navigation. Unknown is not the
    same as broken — falling through to the DOM checks is the right answer."""
    result, _page = await _run(monkeypatch, tmp_path, None, has_form=False)

    assert "no greenhouse application form" in result["error"].lower()
    assert result.get("status") is None
