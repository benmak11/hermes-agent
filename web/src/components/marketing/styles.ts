// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { CSSProperties } from "react";

/** Style constants shared by the marketing sections. No prose lives here. */

export const SANS = "var(--font-jakarta), -apple-system, BlinkMacSystemFont, sans-serif";
export const SERIF = "var(--font-instrument), Georgia, serif";

/** The 1140px section box used by How it works, Features, Security and the closing card. */
export const SECTION: CSSProperties = {
  maxWidth: 1140,
  margin: "0 auto",
  padding: "96px 28px 0",
};

/** Eyebrow above each section h2. */
export const EYEBROW: CSSProperties = {
  fontSize: 11.5,
  fontWeight: 800,
  letterSpacing: "0.16em",
  textTransform: "uppercase",
  color: "var(--terracotta-d)",
};

/** Section h2 (Instrument Serif). */
export const H2: CSSProperties = {
  margin: "16px 0 0",
  fontFamily: SERIF,
  fontWeight: 400,
  fontSize: "clamp(30px,4.2vw,46px)",
  lineHeight: 1.1,
  letterSpacing: "-0.015em",
  color: "var(--ink)",
};
