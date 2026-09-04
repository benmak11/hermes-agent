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

Checking the status closed that hole for error *pages*. It did not close it for
expired postings, which Greenhouse serves as a 302 to the board index that
Playwright follows to a clean 200: the 2026-09-04 rerun reported 12
``custom_wrapper`` bails, all 12 of them redirects to
``.../{board}?error=true`` and none of them a wrapper. So the final URL is
checked too, and the table test below pins exactly which redirects mean "gone"
and which are business as usual.
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

    async def fill(self, _value: str, **_kw) -> None:
        """Reached only once the standard-form check has passed."""
        return None


class _Response:
    def __init__(self, status: int, url: str) -> None:
        self.status = status
        # Playwright reports the *final* URL after any redirect chain it
        # followed, which is the only signal an expired posting leaves behind.
        self.url = url


class _Page:
    """Serves one status and one DOM shape; records the screenshot path."""

    def __init__(
        self,
        status: int | None,
        *,
        has_form: bool,
        final_url: str | None = None,
    ) -> None:
        self._status = status
        self._has_form = has_form
        self._final_url = final_url
        self.shots: list[str] = []

    async def goto(self, url, **_kw):
        # Capture the requested URL: with no redirect, the response URL *is*
        # the requested one, and a test that compared against "" instead would
        # pass for the wrong reason.
        if self._status is None:
            return None
        return _Response(self._status, self._final_url or url)

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


def _job(url: str = "https://job-boards.greenhouse.io/acme/jobs/1") -> Job:
    from datetime import UTC, datetime

    return Job(
        id="j1",
        user_id="u1",
        source="greenhouse",
        source_id="1",
        company="acme",
        title="Staff Engineer",
        url=url,
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


async def _run(
    monkeypatch,
    tmp_path: Path,
    status: int | None,
    *,
    has_form: bool,
    final_url: str | None = None,
    job_url: str = "https://job-boards.greenhouse.io/acme/jobs/1",
):
    page = _Page(status, has_form=has_form, final_url=final_url)
    monkeypatch.setattr(gh, "async_playwright", lambda: _PW(page))
    monkeypatch.setattr(gh, "_detect_captcha", _no_captcha)
    resume = tmp_path / "r.docx"
    resume.write_bytes(b"x")
    return (
        await gh.submit_greenhouse(_job(job_url), _profile(), resume, dry_run=True),
        page,
    )


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


# --------------------------------------------------------------------------
# Expired postings: a 302 to the board index that Playwright follows to a 200.
# --------------------------------------------------------------------------

_POSTING = "https://job-boards.greenhouse.io/acme/jobs/1"


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        # Landed somewhere that no longer names a posting -> gone.
        ("https://job-boards.greenhouse.io/acme?error=true", "error=true"),
        ("https://job-boards.greenhouse.io/acme", "board index"),
        ("https://boards.eu.greenhouse.io/acme?error=true", "error=true"),
        ("https://job-boards.greenhouse.io/acme/?error=true", "error=true"),
        ("https://job-boards.greenhouse.io/", "board index"),
        # Still names a posting. The boards. -> job-boards. migration is a real
        # past incident, and locale prefixes and trailing slashes are routine:
        # calling any of these "gone" would tombstone live postings.
        ("https://job-boards.greenhouse.io/acme/jobs/1", None),
        ("https://job-boards.greenhouse.io/acme/jobs/1/", None),
        ("https://job-boards.greenhouse.io/acme/jobs/1?gh_src=abc", None),
        ("https://job-boards.greenhouse.io/en/acme/jobs/1", None),
        ("https://boards.eu.greenhouse.io/acme/jobs/1", None),
        # The case that makes the `jobs` guard load-bearing, and the only one
        # that does. Every other "still a posting" row above is already excluded
        # by the segment count, so deleting the guard leaves them passing — this
        # is the row that turns a *live* posting into a tombstone without it.
        ("https://job-boards.greenhouse.io/acme/jobs/1?error=true", None),
        # Two segments and a real form — not a board index.
        ("https://job-boards.greenhouse.io/embed/job_app?token=1", None),
        # Off Greenhouse entirely: that is a wrapper, and custom_wrapper is the
        # honest verdict even with ?error=true glued on.
        ("https://careers.acme.com/careers", None),
        ("https://careers.acme.com/careers?error=true", None),
        # Not-greenhouse.io suffix confusion.
        ("https://notgreenhouse.io/acme", None),
        # No redirect at all.
        (_POSTING, None),
        ("", None),
    ],
)
def test_redirected_off_posting(final, expected):
    assert gh._redirected_off_posting(_POSTING, final) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "final",
    [
        "https://job-boards.greenhouse.io/acme?error=true",
        "https://job-boards.greenhouse.io/acme",
    ],
)
async def test_a_200_redirect_to_the_board_index_is_gone(monkeypatch, tmp_path, final):
    """The regression measured on 2026-09-04. Greenhouse expires postings with
    a 302 to the board index, so the status is 200 and the page has no form —
    which used to read as ``custom_wrapper``, a permanent structural verdict
    about a posting that merely closed."""
    result, page = await _run(
        monkeypatch, tmp_path, 200, has_form=False, final_url=final
    )

    assert result["success"] is False
    assert "gone" in result["error"].lower()
    assert "wrapper" not in result["error"].lower()
    assert "transient" not in result["error"].lower()
    # 200 is the honest status of the *final* response; final_url is what lets
    # a measurement tell this mechanism apart from a 404.
    assert result["status"] == 200
    assert result["final_url"] == final
    assert page.shots, "the screenshot is still the evidence of record"


@pytest.mark.asyncio
async def test_a_redirect_bail_clicks_nothing(monkeypatch, tmp_path):
    """Pins the early placement. On a board index ``a:has-text("Apply")`` can
    match a link to a *different* posting; the Apply fallback would click it
    and we would fill a form for a job nobody asked to apply to. Bailing before
    the email-field wait forecloses that (``_Locator.click`` raises)."""
    result, _page = await _run(
        monkeypatch,
        tmp_path,
        200,
        has_form=False,
        final_url="https://job-boards.greenhouse.io/acme?error=true",
    )

    assert result["final_url"].endswith("?error=true")


@pytest.mark.asyncio
async def test_a_wrapper_on_the_employers_own_domain_stays_a_wrapper(
    monkeypatch, tmp_path
):
    """Host scope. A redirect off Greenhouse is what a genuine custom careers
    wrapper looks like, and it needs the Computer Use path, not a tombstone."""
    result, _page = await _run(
        monkeypatch,
        tmp_path,
        200,
        has_form=False,
        final_url="https://careers.acme.com/careers",
    )

    assert "no greenhouse application form" in result["error"].lower()
    assert "final_url" not in result


@pytest.mark.asyncio
async def test_a_board_migration_redirect_still_applies(monkeypatch, tmp_path):
    """boards. -> job-boards. is a redirect Greenhouse really does, to a URL
    that still names the posting. Treating it as gone would break every legacy
    board link we hold."""
    result, _page = await _run(
        monkeypatch,
        tmp_path,
        200,
        has_form=True,
        job_url="https://boards.greenhouse.io/acme/jobs/1",
        final_url="https://job-boards.greenhouse.io/acme/jobs/1",
    )

    # Reaching the fill loop at all proves the standard-form check passed, so
    # neither the redirect branch nor the wrapper branch fired.
    assert "kept resetting" in result["error"].lower()
    assert "final_url" not in result


@pytest.mark.asyncio
async def test_a_job_app_embed_is_not_a_board_index(monkeypatch, tmp_path):
    """Two path segments, and a real application form. The ``<= 1 segment``
    rule is what keeps /embed/job_app out of the gone branch."""
    result, _page = await _run(
        monkeypatch,
        tmp_path,
        200,
        has_form=True,
        final_url="https://job-boards.greenhouse.io/embed/job_app?token=1",
    )

    assert "kept resetting" in result["error"].lower()
    assert "final_url" not in result
