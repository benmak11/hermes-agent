# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared helpers for the per-ATS submitters (Playwright fill/bail policies).

Two policies live here so every submitter applies them identically:

- **CAPTCHA**: bail only on a *visible, interactive* challenge. Many boards
  (notably new-form Greenhouse) ship an invisible reCAPTCHA badge that never
  challenges a normal submit — treating its mere presence as blocking made
  auto-apply fail on boards a human sails through. We still never attempt to
  solve one.
- **Handoff fill**: answer every question that maps 1:1 to profile data
  (links, location) so that when required questions *do* remain, the
  ``needs_input`` handoff asks the user for as little as possible.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from models.profile import MasterProfile

# Progress sink: called with (message, status) as the submission advances.
ProgressFn = Callable[[str, str], Awaitable[None] | None]

# Confirmation-page phrases shared across hosted ATS forms.
CONFIRMATION_PHRASES = (
    "thank you for applying",
    "application received",
    "we've received",
    "we have received",
    "your application has been submitted",
)

# The unanswered-questions entry used when a visible challenge blocks us.
CAPTCHA_HANDOFF = "CAPTCHA challenge — complete it on the application page"


async def emit_progress(
    on_progress: ProgressFn | None, message: str, status: str
) -> None:
    if on_progress is None:
        return
    result = on_progress(message, status)
    if inspect.isawaitable(result):
        await result


# Anchor/challenge iframes across reCAPTCHA v2/v3 and hCaptcha.
_CAPTCHA_IFRAMES = (
    'iframe[src*="recaptcha"][src*="anchor"]',
    'iframe[src*="hcaptcha"]',
    'iframe[title*="captcha" i]',
)


async def detect_blocking_captcha(page: Page) -> bool:
    """True only when a captcha the user would have to interact with is shown.

    Invisible badges (``size=invisible`` anchors, zero-size frames) don't
    block a normal submit and must not abort the attempt.
    """
    for sel in _CAPTCHA_IFRAMES:
        loc = page.locator(sel)
        for i in range(await loc.count()):
            frame = loc.nth(i)
            try:
                src = (await frame.get_attribute("src")) or ""
                if "size=invisible" in src:
                    continue
                box = await frame.bounding_box()
            except PlaywrightError:
                continue
            if box and box["width"] > 0 and box["height"] > 0:
                return True
    return False


def profile_answers(profile: MasterProfile) -> list[tuple[re.Pattern[str], str]]:
    """(label pattern → answer) pairs for questions derivable from the profile.

    Ordered most-specific first; used by ``fill_labeled_answers`` and by the
    Lever ``urls[...]`` named fields.
    """
    links = {k.lower(): v for k, v in (profile.links or {}).items() if v}
    location = (
        profile.residence.country if profile.residence else None
    ) or profile.location
    pairs: list[tuple[re.Pattern[str], str]] = []
    if links.get("linkedin"):
        pairs.append((re.compile(r"linked\s*in", re.I), links["linkedin"]))
    if links.get("github"):
        pairs.append((re.compile(r"git\s*hub", re.I), links["github"]))
    portfolio = links.get("portfolio") or links.get("website")
    if portfolio:
        pairs.append((re.compile(r"portfolio|website", re.I), portfolio))
    if location:
        pairs.append(
            (re.compile(r"^(current\s+)?(location|city|country)\b", re.I), location)
        )
    return pairs


async def fill_labeled_answers(page: Page, profile: MasterProfile) -> None:
    """Best-effort: fill empty text inputs whose label matches profile data.

    Anything that doesn't take (comboboxes that need option selection, fields
    that reject the value) is caught by the caller's required-question check,
    so this can never cause a bad submission — it only shrinks the handoff.
    """
    for pattern, value in profile_answers(profile):
        try:
            loc = page.get_by_label(pattern).first
            if not await loc.count():
                continue
            fillable_and_empty = await loc.evaluate(
                """(el) => (el.tagName === "INPUT" || el.tagName === "TEXTAREA")
                       && !el.value"""
            )
            if fillable_and_empty:
                await loc.fill(value)
        except PlaywrightError:
            continue


# Resolve a human-readable label for a form control — the needs_input handoff
# shows these strings to the user, so element ids are a last resort only.
# Embedded into each submitter's required-question scan via string formatting.
LABEL_TEXT_JS = """
  const labelText = (el) => {
    let holder = null;
    if (el.id) {
      holder = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    }
    if (!holder) {
      const byId = (el.getAttribute("aria-labelledby") || "").split(/\\s+/)[0];
      if (byId) holder = document.getElementById(byId);
    }
    if (!holder) holder = el.closest("label");
    let t = holder ? holder.textContent : (el.getAttribute("aria-label") || "");
    t = (t || "").replace(/[*\\u2731\\u2726]/g, "").replace(/\\s+/g, " ").trim();
    return t || null;
  };
"""
