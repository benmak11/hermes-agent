// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { CSSProperties } from "react";

export type PillTone = "accent" | "good" | "warn" | "muted";

const TONES: Record<PillTone, CSSProperties> = {
  // `--terracotta-d`, not `--terracotta`: the seam doc's contrast rule says
  // terracotta below 13px must be the dark shade (11.5px on the tint is
  // 4.45:1 with the base, 5.35:1 with this).
  accent: {
    color: "var(--terracotta-d)",
    background: "var(--terracotta-tint)",
    border: "1px solid var(--border-warm)",
  },
  good: { color: "var(--sage)", background: "var(--sage-tint)" },
  warn: { color: "#9a6216", background: "var(--honey-tint)" },
  muted: {
    color: "var(--ink-4)",
    background: "var(--surface-warm)",
    border: "1px solid var(--border-warm-hair)",
  },
};

/** Rounded status pill ("Hermes applied for you", "Not booked"). */
export function Pill({
  tone,
  children,
}: {
  tone: PillTone;
  children: React.ReactNode;
}) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "4px 11px",
        borderRadius: 999,
        fontSize: 11.5,
        fontWeight: 700,
        lineHeight: 1.3,
        whiteSpace: "nowrap",
        ...TONES[tone],
      }}
    >
      {children}
    </span>
  );
}
