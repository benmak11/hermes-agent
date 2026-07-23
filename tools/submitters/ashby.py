# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Path A: deterministic Ashby application submitter (Playwright).

Ashby's hosted form (``jobs.ashbyhq.com/{org}/{posting}/application``) is a
React app whose standard fields use stable ``_systemfield_*`` names
(``_systemfield_name``, ``_systemfield_email``, ``_systemfield_phone``) plus a
resume file input and a "Submit Application" button. Ashby's official
application-submit API requires each org's own API key, so the browser path is
the only general one.

NOTE: the ``_systemfield_*`` selectors are from observed markup and must be
confirmed with live dry-runs before the first real submission; label-based
fallbacks cover drift.

Safety mirrors the Greenhouse submitter:
- ``dry_run=True`` fills and screenshots but never clicks Submit.
- Unanswered required questions stop before submit and hand off to the user
  (``needs_input`` + the list of question labels).
- Only a *visible* CAPTCHA challenge stops the attempt; we never solve one.
"""

from __future__ import annotations

import re

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from models.job import Job
from models.profile import MasterProfile
from obs.logging import get_logger

from .common import (
    CAPTCHA_HANDOFF,
    CONFIRMATION_PHRASES,
    LABEL_TEXT_JS,
    ProgressFn,
    detect_blocking_captcha,
    fill_labeled_answers,
)
from .common import (
    emit_progress as _emit,
)

log = get_logger("tools.submitters.ashby")

# React form: check DOM properties, not attributes (same rationale as the
# new-form Greenhouse scan). Ashby marks required controls with aria-required.
_UNANSWERED_REQUIRED_JS = (
    "() => {"
    + LABEL_TEXT_JS
    + """
  const missing = [];
  const seen = new Set();
  const note = (el, fallback) => {
    const label = labelText(el) || fallback;
    if (label && !seen.has(label)) { seen.add(label); missing.push(label); }
  };
  document.querySelectorAll('[aria-required="true"], [required]').forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const fallback = el.getAttribute("name") || el.id || tag;
    if (tag === "input" && el.type === "file") {
      if (!el.files.length) note(el, fallback);
    } else if (tag === "input" && (el.type === "checkbox" || el.type === "radio")) {
      const group = el.name
        ? document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)
        : [el];
      if (![...group].some((g) => g.checked)) note(el, fallback);
    } else if (tag === "input" || tag === "textarea" || tag === "select") {
      if (!el.value || !el.value.trim()) note(el, fallback);
    }
  });
  return missing;
}"""
)


def application_url(job: Job) -> str:
    """The posting's /application page (job.url stores Ashby's jobUrl)."""
    base = job.url.split("?")[0].rstrip("/")
    return base if base.endswith("/application") else f"{base}/application"


async def submit_ashby(
    job: Job,
    profile: MasterProfile,
    resume_path,
    *,
    dry_run: bool = False,
    headless: bool = True,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Submit (or, with dry_run, prepare) an Ashby application.

    Same result contract as the Greenhouse submitter: ``success``, screenshots,
    ``needs_input`` + ``unanswered`` for the user handoff, ``error`` otherwise.
    """
    job_log = log.bind(job_id=job.id, company=job.company, url=job.url)
    job_log.info("submit.start", dry_run=dry_run, headless=headless)
    url = application_url(job)
    first = profile.full_name.split()[0]
    pre_path = f"/tmp/pre_submit_{job.id}.png"
    confirm_path = f"/tmp/confirm_{job.id}.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 1600})
        try:
            await _emit(on_progress, f"Opening {url}", "submitting")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            name_field = page.locator('input[name="_systemfield_name"]').first
            try:
                await name_field.wait_for(state="attached", timeout=15000)
            except PlaywrightTimeout:
                pass  # fall through to the label fallback / form check

            if await detect_blocking_captcha(page):
                job_log.warning("submit.needs_input", reason="captcha")
                return {
                    "success": False,
                    "needs_input": True,
                    "unanswered": [CAPTCHA_HANDOFF],
                }

            file_input = page.locator('input[type="file"]').first
            has_name = await name_field.count()
            if not has_name:
                # Selector drift fallback: the labeled "Name" input.
                name_field = page.get_by_label(
                    re.compile(r"^(full\s+)?name", re.I)
                ).first
                has_name = await name_field.count()
            if not (has_name and await file_input.count()):
                await page.screenshot(path=pre_path, full_page=True)
                job_log.warning("submit.bail", reason="nonstandard_form")
                return {
                    "success": False,
                    "error": (
                        "Not a standard Ashby application form — apply manually "
                        "for now."
                    ),
                    "pre_submit_screenshot": pre_path,
                }

            await _emit(on_progress, "Filling standard fields", "submitting")
            await name_field.fill(profile.full_name)
            await _fill_systemfield(page, "email", profile.email, r"e-?mail")
            if profile.phone:
                await _fill_systemfield(page, "phone", profile.phone, r"phone")
            # Ashby asks "preferred name" on some forms.
            await _fill_systemfield(page, "preferred_name", first, r"preferred\s+name")
            await fill_labeled_answers(page, profile)

            await _emit(on_progress, "Attaching resume", "submitting")
            await file_input.set_input_files(str(resume_path), timeout=10000)
            # Ashby parses the resume and may autofill fields from it.
            await page.wait_for_timeout(3000)

            await page.screenshot(path=pre_path, full_page=True)

            unanswered: list[str] = await page.evaluate(_UNANSWERED_REQUIRED_JS)
            if unanswered:
                job_log.warning(
                    "submit.needs_input", reason="unanswered", fields=unanswered
                )
                return {
                    "success": False,
                    "needs_input": True,
                    "unanswered": unanswered,
                }

            if dry_run:
                await _emit(
                    on_progress, "Dry run — stopped before Submit", "ready_for_review"
                )
                job_log.info("submit.dry_run_ok")
                return {
                    "success": True,
                    "dry_run": True,
                    "pre_submit_screenshot": pre_path,
                }

            await _emit(on_progress, "Submitting application", "submitting")
            submit_btn = page.locator(
                'button:has-text("Submit Application"), button[type="submit"]'
            ).first
            await submit_btn.click()

            confirmed = False
            for _ in range(20):
                await page.wait_for_timeout(1000)
                if await detect_blocking_captcha(page):
                    job_log.warning("submit.needs_input", reason="captcha_challenge")
                    return {
                        "success": False,
                        "needs_input": True,
                        "unanswered": [CAPTCHA_HANDOFF],
                    }
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
                "Confirmation detected"
                if confirmed
                else "Submitted (no confirmation text found)",
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


async def _fill_systemfield(
    page: Page, field: str, value: str, label_pattern: str
) -> None:
    """Fill _systemfield_{field}, falling back to the visible label on drift."""
    loc = page.locator(f'input[name="_systemfield_{field}"]').first
    if not await loc.count():
        loc = page.get_by_label(re.compile(label_pattern, re.I)).first
        if not await loc.count():
            return
    try:
        await loc.fill(value)
    except PlaywrightError:
        pass
