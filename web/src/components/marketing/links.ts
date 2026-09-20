// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { APP_HOME } from "@/lib/nav";

/**
 * Every href on the marketing site. Hashes are `/#x`, not `#x`, so the same
 * nav and footer work from `/security`, `/contact` and `/terms`. The four
 * `?from=` values must stay in `SIGNUP_SOURCES` (`@/lib/nav`); `links.test.ts`
 * checks each one round-trips through `signupFrom`.
 */
export const LINKS = {
  top: "/#top",
  how: "/#how",
  journeys: "/#journeys",
  security: "/#security",
  start: "/#start",
  login: "/login",
  app: APP_HOME,
  signup: {
    nav: "/signup?from=nav",
    hero: "/signup?from=hero",
    closing: "/signup?from=closing",
    footer: "/signup?from=footer",
  },
  pages: { security: "/security", contact: "/contact", terms: "/terms" },
} as const;
