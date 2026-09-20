// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { CSSProperties } from "react";

/** Style constants for the auth + onboarding screens. No prose lives here.
 *  `SANS`/`SERIF` duplicate marketing/styles.ts on purpose: auth files may
 *  not import from the marketing folder. */

export const SANS = "var(--font-jakarta), -apple-system, BlinkMacSystemFont, sans-serif";
export const SERIF = "var(--font-instrument), Georgia, serif";

/** The 24px raised card every 01–05 screen sits in. */
export const CARD: CSSProperties = {
  borderRadius: 24,
  border: "1px solid var(--border-warm-hair)",
  background: "var(--surface-warm)",
  boxShadow: "0 1px 2px rgba(94,63,39,0.05), 0 22px 50px rgba(94,63,39,0.12)",
};
