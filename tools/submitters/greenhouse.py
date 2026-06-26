# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Path A: deterministic Greenhouse application submitter (Playwright).

Greenhouse-hosted forms have a consistent DOM (text inputs id'd ``first_name``,
``last_name``, ``email``, ``phone``; a typed file input for the resume; a Submit
button). We fill the standard fields, attach the resume, screenshot before and
after, and only click Submit when every required field is satisfied.

Safety:
- ``dry_run=True`` fills the form and screenshots but never clicks Submit — used
  to validate against real postings without applying.
- Unanswered required custom questions abort before submit (escalate to the user).
- CAPTCHAs are detected and abort; we never attempt to bypass them.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path

from playwright.async_api import Page, async_playwright

from models.job import Job
from models.profile import MasterProfile
from obs.logging import get_logger

log = get_logger("tools.submitters.greenhouse")

# Progress sink: called with (message, status) as the submission advances.
ProgressFn = Callable[[str, str], Awaitable[None] | None]

CONFIRMATION_PHRASES = (
    "thank you for applying",
    "application received",
    "we've received",
    "we have received",
    "your application has been submitted",
)


async def _emit(on_progress: ProgressFn | None, message: str, status: str) -> None:
    if on_progress is None:
        return
    result = on_progress(message, status)
    if inspect.isawaitable(result):
        await result


async def _detect_captcha(page: Page) -> bool:
    for sel in (
        'iframe[src*="recaptcha"]',
        'iframe[title*="captcha" i]',
        'div.g-recaptcha',
        'iframe[src*="hcaptcha"]',
    ):
        if await page.locator(sel).count():
            return True
    return False


async def submit_greenhouse(
    job: Job,
    profile: MasterProfile,
    resume_path: Path,
    *,
    dry_run: bool = False,
    headless: bool = True,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Submit (or, with dry_run, prepare) a Greenhouse application.

    Returns a dict with ``success`` plus, on success, screenshot paths and any
    detected confirmation; on failure, ``error`` and the pre-submit screenshot.
    """
    job_log = log.bind(job_id=job.id, company=job.company, url=job.url)
    job_log.info("submit.start", dry_run=dry_run, headless=headless)
    first = profile.full_name.split()[0]
    last = " ".join(profile.full_name.split()[1:])
    pre_path = f"/tmp/pre_submit_{job.id}.png"
    confirm_path = f"/tmp/confirm_{job.id}.png"

    async with async_playwright() as p:
        # --no-sandbox / --disable-dev-shm-usage are required to run Chromium as
        # root inside the Cloud Run container.
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 1600})
        try:
            await _emit(on_progress, f"Opening {job.url}", "submitting")
            await page.goto(job.url, wait_until="networkidle", timeout=30000)

            # The form may live behind an "Apply" button.
            apply_btn = page.locator(
                'a:has-text("Apply"), button:has-text("Apply")'
            ).first
            if await apply_btn.count():
                await apply_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)

            if await _detect_captcha(page):
                await page.screenshot(path=pre_path, full_page=True)
                job_log.warning("submit.bail", reason="captcha")
                return {
                    "success": False,
                    "error": "CAPTCHA present — solve it in a browser, then retry.",
                    "pre_submit_screenshot": pre_path,
                }

            # Confirm this is a standard Greenhouse-hosted form before touching it.
            # Custom careers wrappers (careers.airbnb.com, etc.) lack these and need
            # the Computer Use path (deferred) — bail cleanly instead of hanging.
            email_field = page.locator('input#email, input[name="email"]').first
            file_input = page.locator('input[type="file"]').first
            if not (await email_field.count() and await file_input.count()):
                await page.screenshot(path=pre_path, full_page=True)
                job_log.warning("submit.bail", reason="custom_wrapper")
                return {
                    "success": False,
                    "error": (
                        "Not a standard Greenhouse form (custom careers wrapper) — "
                        "needs the Computer Use path, which is deferred. Apply manually."
                    ),
                    "pre_submit_screenshot": pre_path,
                }

            await _emit(on_progress, "Filling standard fields", "submitting")
            await _fill_first(page, ['input#first_name', 'input[name="first_name"]'], first)
            await _fill_first(page, ['input#last_name', 'input[name="last_name"]'], last)
            await _fill_first(page, ['input#email', 'input[name="email"]'], profile.email)
            if profile.phone:
                await _fill_first(
                    page, ['input#phone', 'input[name="phone"]'], profile.phone
                )

            await _emit(on_progress, "Attaching resume", "submitting")
            await file_input.set_input_files(str(resume_path), timeout=10000)
            await page.wait_for_timeout(3000)  # let the upload settle

            await page.screenshot(path=pre_path, full_page=True)

            # Abort if required custom questions are left blank.
            unanswered = await page.locator(
                '[aria-required="true"]:not(:has(input:not([value=""])))'
            ).count()
            if unanswered > 0:
                job_log.warning("submit.bail", reason="unanswered", count=unanswered)
                return {
                    "success": False,
                    "error": f"{unanswered} required custom question(s) need answers.",
                    "pre_submit_screenshot": pre_path,
                }

            if dry_run:
                await _emit(on_progress, "Dry run — stopped before Submit", "ready_for_review")
                job_log.info("submit.dry_run_ok")
                return {
                    "success": True,
                    "dry_run": True,
                    "pre_submit_screenshot": pre_path,
                }

            await _emit(on_progress, "Submitting application", "submitting")
            submit_btn = page.locator(
                'button[type="submit"]:has-text("Submit"), '
                'button:has-text("Submit Application")'
            ).first
            await submit_btn.click()
            await page.wait_for_load_state("networkidle", timeout=20000)

            await page.screenshot(path=confirm_path, full_page=True)
            body = (await page.locator("body").inner_text()).lower()
            confirmed = any(phrase in body for phrase in CONFIRMATION_PHRASES)
            await _emit(
                on_progress,
                "Confirmation detected" if confirmed else "Submitted (no confirmation text found)",
                "submitted" if confirmed else "submitting",
            )
            job_log.info("submit.complete", confirmed=confirmed)
            return {
                "success": confirmed,
                "pre_submit_screenshot": pre_path,
                "confirmation_screenshot": confirm_path,
                **({} if confirmed else {"error": "No confirmation text detected."}),
            }
        except Exception:
            job_log.exception("submit.error")
            raise
        finally:
            await browser.close()


async def _fill_first(page: Page, selectors: list[str], value: str) -> None:
    """Fill the first matching selector; ignore if none present."""
    for sel in selectors:
        loc = page.locator(sel).first
        if await loc.count():
            await loc.fill(value)
            return
