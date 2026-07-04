# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Path A: deterministic Greenhouse application submitter (Playwright).

Handles both generations of Greenhouse-hosted forms:

- legacy ``boards.greenhouse.io`` pages
- the newer ``job-boards.greenhouse.io`` React app (Greenhouse has been
  migrating hosted boards there; the Job Board API's ``absolute_url`` already
  points at it for migrated companies, e.g. gitlab)

Both render the same core field ids (``first_name``, ``last_name``, ``email``,
``phone``, a file input for the resume, a Submit button), so one fill path
covers both. The new app differs in ways this module must work around: the
``value`` attribute never reflects user input (React state only), dropdown
questions are react-select comboboxes, and persistent analytics connections
mean ``networkidle`` never reliably settles.

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

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

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

# Required fields must be checked through DOM properties (el.value), never the
# value *attribute*: the new job-boards React app renders value="" once at SSR
# and never syncs it, so an attribute check flags every field — filled or not.
# Returns labels of unanswered required fields; empty list means safe to submit.
_UNANSWERED_REQUIRED_JS = """
() => {
  const missing = [];
  const seen = new Set();
  const note = (label) => {
    if (label && !seen.has(label)) { seen.add(label); missing.push(label); }
  };
  document.querySelectorAll('[aria-required="true"], [required]').forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const label = el.id || el.getAttribute("name")
      || el.getAttribute("aria-labelledby") || el.getAttribute("aria-label")
      || tag;
    // react-select structure: the chosen option renders as a *single-value
    // node inside the *value-container — a SIBLING of the input's own
    // container, so anchor the answered check there (closest '[class*=select]'
    // would match the inner input-container and never see it).
    const valueRoot = el.closest('[class*="value-container"]');
    const shell = el.closest('[class*="select-shell"], [class*="select"]');
    if (el.getAttribute("role") === "group") {
      // New-form file upload wrapper. Answered states: file still in the
      // input, uploaded-file chip rendered (React removes the input after
      // upload), or the "Enter manually" textarea filled.
      const file = el.querySelector('input[type="file"]');
      if (file && file.files.length) return;
      if (el.querySelector('[class*="__filename"]')) return;
      const manual = el.querySelector("textarea");
      if (manual && manual.value.trim()) return;
      note(label);
    } else if (el.getAttribute("role") === "combobox" || (tag === "input" && shell)) {
      // react-select keeps its inputs permanently empty even when answered.
      // Label anonymous inner inputs from the react-select-* ids so the two
      // inputs of one dropdown dedupe to a single entry.
      const root = valueRoot || shell;
      if (root && root.querySelector('[class*="single-value"]')) return;
      let l = label;
      if (l === tag && shell) {
        const rid = shell.querySelector('[id^="react-select-"]');
        if (rid) l = rid.id.replace("react-select-", "")
          .replace(/-(placeholder|input|live-region)$/, "");
      }
      note(l);
    } else if (tag === "input" && el.type === "file") {
      if (!el.files.length) note(label);
    } else if (tag === "input" || tag === "textarea" || tag === "select") {
      if (!el.value || !el.value.trim()) note(label);
    }
  });
  return missing;
}
"""


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
            await page.goto(job.url, wait_until="domcontentloaded", timeout=30000)

            # Wait for the form itself, not networkidle (which never settles on
            # the new job-boards app). The form may live behind an "Apply"
            # button, so click through and retry once before giving up.
            email_field = page.locator('input#email, input[name="email"]').first
            try:
                await email_field.wait_for(state="attached", timeout=10000)
            except PlaywrightTimeout:
                apply_btn = page.locator(
                    'a:has-text("Apply"), button:has-text("Apply")'
                ).first
                if await apply_btn.count():
                    await apply_btn.click()
                    try:
                        await email_field.wait_for(state="attached", timeout=10000)
                    except PlaywrightTimeout:
                        pass  # handled by the standard-form check below

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
            # The file input is visually-hidden on the new form; count() still
            # sees it and set_input_files works on hidden file inputs.
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
            # React hydration on the new job-boards app recreates the SSR'd
            # inputs, silently wiping anything filled too early — fill, verify
            # the value actually stuck, and refill until hydration settles.
            fields_stuck = False
            for _ in range(5):
                await _fill_first(
                    page, ['input#first_name', 'input[name="first_name"]'], first
                )
                await _fill_first(
                    page, ['input#last_name', 'input[name="last_name"]'], last
                )
                await _fill_first(
                    page, ['input#email', 'input[name="email"]'], profile.email
                )
                if profile.phone:
                    await _fill_first(
                        page, ['input#phone', 'input[name="phone"]'], profile.phone
                    )
                # Semi-standard field on some boards; harmless when absent.
                await _fill_first(
                    page,
                    ['input#preferred_name', 'input[name="preferred_name"]'],
                    first,
                )
                await page.wait_for_timeout(1500)
                fields_stuck = await page.evaluate(
                    """() => {
                      const el = document.querySelector('input#email, input[name="email"]');
                      return !!(el && el.value);
                    }"""
                )
                if fields_stuck:
                    break
            if not fields_stuck:
                await page.screenshot(path=pre_path, full_page=True)
                job_log.warning("submit.bail", reason="fields_not_sticking")
                return {
                    "success": False,
                    "error": "Form kept resetting filled fields — apply manually.",
                    "pre_submit_screenshot": pre_path,
                }

            # Country is a standard (often required) react-select on new-form
            # postings — fill it from the profile. Best-effort: if the option
            # doesn't take, the required-field check below still bails.
            country = (
                profile.residence.country if profile.residence else None
            ) or profile.location
            if country:
                await _fill_combobox(page, "input#country", country)

            # Attach the resume only after hydration is stable so the file
            # input isn't recreated out from under us.
            await _emit(on_progress, "Attaching resume", "submitting")
            await file_input.set_input_files(str(resume_path), timeout=10000)
            await page.wait_for_timeout(3000)  # let the upload settle

            await page.screenshot(path=pre_path, full_page=True)

            # Abort if required questions are left blank (typically custom
            # dropdowns Path A can't answer deterministically).
            unanswered: list[str] = await page.evaluate(_UNANSWERED_REQUIRED_JS)
            if unanswered:
                job_log.warning("submit.bail", reason="unanswered", fields=unanswered)
                return {
                    "success": False,
                    "error": (
                        f"{len(unanswered)} required question(s) need answers: "
                        + ", ".join(unanswered)
                    ),
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

            # Poll for confirmation text instead of waiting for networkidle: a
            # wait timeout after the click must not report failure for an
            # application that actually went through (failed status permits
            # retry, which would double-submit).
            confirmed = False
            for _ in range(20):
                await page.wait_for_timeout(1000)
                try:
                    body = (await page.locator("body").inner_text()).lower()
                except PlaywrightError:
                    continue  # mid-navigation; try again
                if any(phrase in body for phrase in CONFIRMATION_PHRASES):
                    confirmed = True
                    break

            await page.screenshot(path=confirm_path, full_page=True)
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


async def _fill_combobox(page: Page, selector: str, value: str) -> None:
    """Best-effort react-select fill: type the value, take the top option.

    Any failure just leaves the field unanswered — the required-field check
    catches it before submit, so this can never cause a bad submission.
    """
    combo = page.locator(selector).first
    if not await combo.count():
        return
    try:
        await combo.click()
        # Real keystrokes, not fill(): react-select filters on key events.
        await combo.press_sequentially(value, delay=30)
        await page.wait_for_timeout(800)  # options load async
        await combo.press("Enter")
    except PlaywrightError:
        return
