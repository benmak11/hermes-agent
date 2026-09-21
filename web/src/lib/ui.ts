// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

export function initial(name: string): string {
  return (name.trim()[0] ?? "?").toUpperCase();
}

export type UserAvatar =
  | { kind: "initials"; text: string }
  | { kind: "email"; text: string }
  | { kind: "glyph" };

/**
 * Resolve a signed-in user's avatar: a name yields initials (first + last),
 * otherwise the email's first letter, otherwise a neutral person glyph.
 * Mirrors the onboarding "Avatar resolution" spec:
 *   initials(name) ?? email[0].toUpperCase() ?? glyph
 */
export function resolveUserAvatar(
  name?: string | null,
  email?: string | null,
): UserAvatar {
  const parts = (name ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 1) {
    return { kind: "initials", text: parts[0][0].toUpperCase() };
  }
  if (parts.length >= 2) {
    const first = parts[0][0];
    const last = parts[parts.length - 1][0];
    return { kind: "initials", text: (first + last).toUpperCase() };
  }
  const e = email?.trim();
  if (e) return { kind: "email", text: e[0].toUpperCase() };
  return { kind: "glyph" };
}

/** Colour (a CSS var) for the big numeric score, keyed by recommendation. */
export function scoreColor(rec: string): string {
  if (rec === "strong_apply") return "var(--sage)";
  if (rec === "apply") return "var(--terracotta)";
  if (rec === "maybe") return "var(--honey)";
  return "var(--ink-4)";
}

export type WarmPillTone = "accent" | "good" | "warn" | "muted";

/** Recommendation → `Pill` tone + label (design 07: "● STRONG MATCH"). */
export function recPill(rec: string): { tone: WarmPillTone; label: string } {
  switch (rec) {
    case "strong_apply":
      return { tone: "good", label: "STRONG MATCH" };
    case "apply":
      return { tone: "accent", label: "GOOD MATCH" };
    case "maybe":
      return { tone: "warn", label: "MAYBE" };
    default:
      return { tone: "muted", label: "WEAK MATCH" };
  }
}

/** Fill for a 0-100 breakdown bar. */
export function barColor(v: number): string {
  if (v >= 80) return "var(--sage)";
  if (v >= 60) return "var(--terracotta)";
  return "var(--honey)";
}
