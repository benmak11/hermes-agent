# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Path A: deterministic Lever application submitter (Playwright).

Lever's hosted apply form (``jobs.lever.co/{org}/{posting}/apply``) is plain
server-rendered HTML — no hydration races, stable ``name`` attributes:
``name``, ``email``, ``phone``, ``org``, ``location``, ``urls[LinkedIn]``-style
link fields, a ``resume`` file input, and per-question "application-question"
cards. Confirmation lands on ``.../thanks``.

Safety mirrors the Greenhouse submitter:
- ``dry_run=True`` fills and screenshots but never clicks Submit.
- Unanswered required questions stop before submit and hand off to the user
  (``needs_input`` + the list of question labels).
- Only a *visible* CAPTCHA challenge stops the attempt (Lever occasionally
  ships hCaptcha); we never attempt to solve one.
"""

from __future__ import annotations

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
    profile_answers,
)
from .common import (
    emit_progress as _emit,
)

log = get_logger("tools.submitters.lever")

# Required controls on the plain-HTML form: the resume input, checkbox/radio
# groups (answered when any member is checked), and text-ish fields. Cards for
# custom questions carry the label; LABEL_TEXT_JS resolves it for the handoff.
_UNANSWERED_REQUIRED_JS = (
    "() => {"
    + LABEL_TEXT_JS
    + """
  const missing = [];
  const seen = new Set();
  const note = (el, fallback) => {
    const card = el.closest(".application-question");
    const cardLabel = card
      ? (card.querySelector(".application-label, label")?.textContent || "")
          .replace(/[*\\u2731]/g, "").replace(/\\s+/g, " ").trim()
      : "";
    const label = cardLabel || labelText(el) || fallback;
    if (label && !seen.has(label)) { seen.add(label); missing.push(label); }
  };
  const sel = 'input[required], textarea[required], select[required],'
    + ' [aria-required="true"]';
  document.querySelectorAll(sel).forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const fallback = el.getAttribute("name") || el.id || tag;
    if (tag === "input" && el.type === "file") {
      if (!el.files.length) note(el, fallback);
    } else if (tag === "input" && (el.type === "checkbox" || el.type === "radio")) {
      const group = el.name
        ? document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)
        : [el];
      if (![...group].some((g) => g.checked)) note(el, fallback);
    } else if (!el.value || !el.value.trim()) {
      note(el, fallback);
    }
  });
  return missing;
}"""
)


def apply_url(job: Job) -> str:
    """The posting's /apply page (job.url stores Lever's hostedUrl)."""
    base = job.url.split("?")[0].rstrip("/")
    return base if base.endswith("/apply") else f"{base}/apply"


async def submit_lever(
    job: Job,
    profile: MasterProfile,
    resume_path,
    *,
    dry_run: bool = False,
    headless: bool = True,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Submit (or, with dry_run, prepare) a Lever application.

    Same result contract as the Greenhouse submitter: ``success``, screenshots,
    ``needs_input`` + ``unanswered`` for the user handoff, ``error`` otherwise.
    """
    job_log = log.bind(job_id=job.id, company=job.company, url=job.url)
    job_log.info("submit.start", dry_run=dry_run, headless=headless)
    url = apply_url(job)
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

            name_field = page.locator('input[name="name"]').first
            file_input = page.locator('input[type="file"]').first
            try:
                await name_field.wait_for(state="attached", timeout=10000)
            except PlaywrightTimeout:
                pass  # handled by the standard-form check below

            if await detect_blocking_captcha(page):
                job_log.warning("submit.needs_input", reason="captcha")
                return {
                    "success": False,
                    "needs_input": True,
                    "unanswered": [CAPTCHA_HANDOFF],
                }

            if not (await name_field.count() and await file_input.count()):
                await page.screenshot(path=pre_path, full_page=True)
                job_log.warning("submit.bail", reason="nonstandard_form")
                return {
                    "success": False,
                    "error": (
                        "Not a standard Lever apply form — apply manually for now."
                    ),
                    "pre_submit_screenshot": pre_path,
                }

            await _emit(on_progress, "Filling standard fields", "submitting")
            await _fill_named(page, "name", profile.full_name)
            await _fill_named(page, "email", profile.email)
            if profile.phone:
                await _fill_named(page, "phone", profile.phone)
            current_org = next(
                (e.company for e in profile.experience if e.end is None), None
            )
            if current_org:
                await _fill_named(page, "org", current_org)
            location = (
                profile.residence.city if profile.residence else None
            ) or profile.location
            if location:
                await _fill_named(page, "location", location)

            # urls[LinkedIn] / urls[GitHub] / urls[Portfolio] named inputs, then
            # a generic label-matched pass for custom question cards.
            url_inputs = page.locator("input[name^='urls[']")
            url_names = [
                (await url_inputs.nth(i).get_attribute("name")) or ""
                for i in range(await url_inputs.count())
            ]
            for pattern, value in profile_answers(profile):
                for i, name_attr in enumerate(url_names):
                    field = url_inputs.nth(i)
                    if pattern.search(name_attr) and not await field.input_value():
                        await field.fill(value)
                        break
            await fill_labeled_answers(page, profile)

            await _emit(on_progress, "Attaching resume", "submitting")
            await file_input.set_input_files(str(resume_path), timeout=10000)
            # Lever parses the resume server-side and may autofill from it —
            # let that settle before checking required questions.
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
            submit_btn = page.locator('button#btn-submit, button[type="submit"]').first
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
                if "/thanks" in page.url:
                    confirmed = True
                    break
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


async def _fill_named(page: Page, name: str, value: str) -> None:
    """Fill input[name=...] if present; plain HTML, no hydration retry needed."""
    loc = page.locator(f'input[name="{name}"]').first
    if await loc.count():
        await loc.fill(value)
